"""OAuth and PKCE auth for the Fastn MCP server.

Two concerns live here:

1. **PKCE helpers** — used by the ``setup`` tool to do the manual
   Authorization-Code + PKCE flow against Fastn's Keycloak instance.
   These are low-level utilities and remain unchanged.

2. **FastnOAuthProvider** — implements the MCP SDK's
   ``OAuthAuthorizationServerProvider`` protocol so that MCP clients
   (Lovable, MCP Inspector, etc.) can authenticate via the standard
   MCP OAuth flow.  This provider proxies to Fastn's Keycloak for the
   actual authentication then issues its own opaque tokens.

Flow (MCP OAuth ↔ Keycloak):

    MCP Client           MCP Server              Keycloak
    ─────────           ──────────              ────────
    1. GET /authorize ──▶ provider.authorize()
                          builds Keycloak URL ──▶ /auth
    2.                    /callback?code=KC ◀── redirect
                          exchange KC code   ──▶ /token
                          store tokens
                          redirect client
       ◀── redirect with MCP code
    3. POST /token ──────▶ exchange_authorization_code()
                          return MCP access_token
       ◀── {access_token}
    4. Bearer <token> ──▶ load_access_token()
                          validate, return AccessToken
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import pathlib
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
import jwt as pyjwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientError
from pydantic import AnyUrl

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    OAuthToken,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull

logger = logging.getLogger("fastn-mcp.auth")

# ---------------------------------------------------------------------------
# Keycloak endpoints
# ---------------------------------------------------------------------------

KEYCLOAK_BASE = "https://live.fastn.ai/auth/realms/fastn"
KEYCLOAK_AUTHORIZE_URL = f"{KEYCLOAK_BASE}/protocol/openid-connect/auth"
KEYCLOAK_TOKEN_URL = f"{KEYCLOAK_BASE}/protocol/openid-connect/token"
KEYCLOAK_CLIENT_ID = "fastn-oauth"
KEYCLOAK_SDK_CLIENT_ID = "fastn-sdk"
KEYCLOAK_JWKS_URL = f"{KEYCLOAK_BASE}/protocol/openid-connect/certs"
KEYCLOAK_ISSUER = KEYCLOAK_BASE  # "https://live.fastn.ai/auth/realms/fastn"

# Fastn Connect endpoint
FASTN_CONNECT_URL = "https://app.fastn.ai/connect"

# ---------------------------------------------------------------------------
# Redirect URI allowlist — only these domains may register as MCP clients.
# Localhost is allowed for desktop MCP clients (Claude Desktop, Cursor, etc.)
# which bind to random ports each session.
# ---------------------------------------------------------------------------

ALLOWED_REDIRECT_DOMAINS: set[str] = {
    # Desktop MCP clients (random localhost ports)
    "localhost",
    "127.0.0.1",
    # Vibe coding platforms
    "lovable.dev",
    "bolt.new",
    "v0.dev",
    "replit.com",
    "cursor.sh",
    "windsurf.ai",
    # AI assistants
    "claude.ai",
    # Fastn own domains
    "app.fastn.ai",
    "mcp.live.fastn.ai",
    # Development tunnels
    "ngrok-free.dev",
}

# Supported MCP scopes — clients may request any of these.
# Set on registration if the client doesn't specify a scope.
SUPPORTED_SCOPES = "read write"

# Maximum number of registered clients (prevent memory exhaustion)
MAX_REGISTERED_CLIENTS = 500

# Rate limiting: max registrations per IP within window
REGISTER_RATE_LIMIT = 10  # max registrations
REGISTER_RATE_WINDOW = 60  # seconds

# Token lifetimes
ACCESS_TOKEN_LIFETIME = 3600  # 1 hour
REFRESH_TOKEN_LIFETIME = 86400  # 24 hours
AUTH_CODE_LIFETIME = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Redirect URI validation
# ---------------------------------------------------------------------------


def _validate_redirect_uri(uri: str) -> bool:
    """Check that a redirect URI belongs to an allowed domain.

    Rules (per MCP spec + OAuth 2.1 best practices):
    - localhost / 127.0.0.1: allow http with any port (desktop clients)
    - All other domains: require https, must be in ALLOWED_REDIRECT_DOMAINS
    - No wildcards, no fragments, no user-info
    """
    try:
        parsed = urlparse(str(uri))
    except Exception:
        return False

    # Must have scheme and host
    if not parsed.scheme or not parsed.hostname:
        return False

    # No fragments allowed (OAuth 2.1 §2.3.1)
    if parsed.fragment:
        return False

    # No user-info (prevents credential leaking)
    if parsed.username or parsed.password:
        return False

    hostname = parsed.hostname.lower()

    # Localhost: allow http, any port
    if hostname in ("localhost", "127.0.0.1", "::1"):
        return parsed.scheme in ("http", "https")

    # Custom schemes for native apps (e.g. vscode://mcp-auth/callback)
    if parsed.scheme not in ("http", "https"):
        return False

    # All non-localhost must be HTTPS
    if parsed.scheme != "https":
        return False

    # Check domain against allowlist (exact match or subdomain)
    for allowed in ALLOWED_REDIRECT_DOMAINS:
        if hostname == allowed or hostname.endswith(f".{allowed}"):
            return True

    return False


class RegistrationError(Exception):
    """Raised when client registration is rejected."""
    pass


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


@dataclass
class PKCEChallenge:
    """PKCE code verifier and challenge pair."""

    code_verifier: str
    code_challenge: str


@dataclass
class TokenResponse:
    """OAuth token response."""

    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = "Bearer"


def generate_pkce_challenge() -> PKCEChallenge:
    """Generate a PKCE code_verifier and code_challenge (S256)."""
    code_verifier = (
        base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    )
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = (
        base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    )
    return PKCEChallenge(
        code_verifier=code_verifier,
        code_challenge=code_challenge,
    )


def build_authorize_url(
    code_challenge: str,
    redirect_uri: str,
    state: Optional[str] = None,
) -> str:
    """Build the Keycloak authorization URL with PKCE challenge."""
    params = {
        "client_id": KEYCLOAK_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scope": "openid profile email",
    }
    if state:
        params["state"] = state
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{KEYCLOAK_AUTHORIZE_URL}?{query}"


async def exchange_code_for_tokens(
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> TokenResponse:
    """Exchange an authorization code for tokens using PKCE."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            KEYCLOAK_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": KEYCLOAK_CLIENT_ID,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        response.raise_for_status()
        data = response.json()

    return TokenResponse(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_in=data.get("expires_in", 300),
        token_type=data.get("token_type", "Bearer"),
    )


async def refresh_tokens(refresh_token: str, client_id: str = KEYCLOAK_CLIENT_ID) -> TokenResponse:
    """Refresh an access token using a refresh token."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            KEYCLOAK_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
            },
        )
        response.raise_for_status()
        data = response.json()

    return TokenResponse(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_in=data.get("expires_in", 300),
        token_type=data.get("token_type", "Bearer"),
    )


def build_connect_url(
    access_token: str,
    redirect_uri: str,
) -> str:
    """Build the Fastn Connect URL for workspace selection."""
    return (
        f"{FASTN_CONNECT_URL}"
        f"?access_token={access_token}"
        f"&redirect_uri={redirect_uri}"
    )


# ---------------------------------------------------------------------------
# Helper: generate opaque tokens with ≥160 bits of entropy (per RFC 6749)
# ---------------------------------------------------------------------------


def _generate_token() -> str:
    """Generate a URL-safe opaque token with 256 bits of entropy."""
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Keycloak JWT validator (for direct Bearer token mode)
# ---------------------------------------------------------------------------


class _KeycloakJWTValidator:
    """Validates Keycloak JWTs via JWKS public key verification (RS256)."""

    def __init__(self, jwks_url=KEYCLOAK_JWKS_URL, issuer=KEYCLOAK_ISSUER):
        self._issuer = issuer
        self._jwk_client = PyJWKClient(uri=jwks_url, cache_jwk_set=True, lifespan=3600)

    def validate(self, token: str) -> dict | None:
        """Validate JWT signature + expiry + issuer.  Synchronous (PyJWKClient uses urllib)."""
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            return pyjwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                options={"verify_exp": True, "verify_iss": True, "verify_aud": False},
            )
        except (pyjwt.InvalidTokenError, PyJWKClientError, Exception):
            return None


# ---------------------------------------------------------------------------
# FastnOAuthProvider — MCP OAuth ↔ Keycloak bridge
# ---------------------------------------------------------------------------


@dataclass
class _PendingAuth:
    """State stored while the user authenticates with Keycloak."""

    pkce: PKCEChallenge
    keycloak_redirect_uri: str  # our callback URL
    client_redirect_uri: str  # MCP client's redirect_uri
    client_state: str | None
    client_id: str
    scopes: list[str]
    code_challenge: str  # client's PKCE challenge
    redirect_uri_provided_explicitly: bool
    resource: str | None = None


class FastnOAuthProvider:
    """MCP OAuth Authorization Server that proxies to Fastn's Keycloak.

    Implements ``OAuthAuthorizationServerProvider`` so the MCP SDK handles
    all the HTTP endpoints (/authorize, /token, /register, /revoke, metadata).

    Keycloak tokens are used directly as MCP tokens — no secondary token store.
    The MCP access_token IS the Keycloak JWT; the MCP refresh_token IS the
    Keycloak refresh token.  Validation is stateless (JWKS for JWTs, direct
    Keycloak exchange for refreshes).

    Only state kept:
    - _clients:       registered MCP clients (required by MCP spec)
    - _pending_auths: in-flight OAuth dance state (keyed by Keycloak state param)
    - _auth_codes:    short-lived codes → Keycloak token pairs (5-min lifetime)
    """

    def __init__(
        self,
        server_url: str = "http://localhost:8000",
        clients_file: str | None = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self._clients_file = pathlib.Path(clients_file) if clients_file else None
        self._clients: Dict[str, OAuthClientInformationFull] = {}
        self._pending_auths: Dict[str, _PendingAuth] = {}
        self._auth_codes: Dict[str, AuthorizationCode] = {}
        self._auth_code_kc_tokens: Dict[str, TokenResponse] = {}  # code → Keycloak tokens
        self._kc_jwt_validator = _KeycloakJWTValidator()
        self._register_attempts: list[float] = []
        self._load_clients()

    # -- client persistence ----------------------------------------------------

    def _load_clients(self) -> None:
        """Load registered clients from disk (if clients_file is set)."""
        if not self._clients_file or not self._clients_file.exists():
            return
        try:
            data = json.loads(self._clients_file.read_text())
            for client_data in data.values():
                client = OAuthClientInformationFull.model_validate(client_data)
                self._clients[client.client_id] = client
            logger.info("Loaded %d OAuth clients from %s", len(self._clients), self._clients_file)
        except Exception as exc:
            logger.warning("Failed to load clients from %s: %s", self._clients_file, exc)

    def _save_clients(self) -> None:
        """Persist registered clients to disk (if clients_file is set)."""
        if not self._clients_file:
            return
        try:
            self._clients_file.parent.mkdir(parents=True, exist_ok=True)
            data = {cid: c.model_dump(mode="json") for cid, c in self._clients.items()}
            self._clients_file.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            logger.warning("Failed to save clients to %s: %s", self._clients_file, exc)

    # -- helpers ---------------------------------------------------------------

    def get_keycloak_token(self, mcp_token: str) -> str | None:
        """The MCP token IS the Keycloak token — return as-is."""
        return mcp_token

    def cleanup_expired(self) -> int:
        """Remove expired auth codes and stale pending auths.

        Returns the total number of entries removed. Call periodically
        (e.g. every 5 minutes) to bound memory usage.
        """
        now = time.time()
        removed = 0

        # Expired auth codes (5-min lifetime)
        for code_str in list(self._auth_codes):
            if now > self._auth_codes[code_str].expires_at:
                self._auth_codes.pop(code_str, None)
                self._auth_code_kc_tokens.pop(code_str, None)
                removed += 1

        # Stale pending auths — no timestamp available, trim when > 100
        if len(self._pending_auths) > 100:
            keys = list(self._pending_auths.keys())
            for k in keys[: len(keys) // 2]:
                self._pending_auths.pop(k, None)
                removed += 1

        # Stale rate-limit entries
        self._register_attempts = [
            t for t in self._register_attempts if now - t < REGISTER_RATE_WINDOW
        ]

        if removed:
            logger.info("Cleanup: removed %d expired entries", removed)
        return removed

    def _check_register_rate_limit(self) -> None:
        """Enforce global rate limiting on client registration."""
        now = time.time()
        # Prune old attempts outside the window
        self._register_attempts = [
            t for t in self._register_attempts if now - t < REGISTER_RATE_WINDOW
        ]
        if len(self._register_attempts) >= REGISTER_RATE_LIMIT:
            logger.warning("Registration rate limit exceeded (%d in %ds)",
                           REGISTER_RATE_LIMIT, REGISTER_RATE_WINDOW)
            raise RegistrationError(
                "Too many registration attempts. Try again later."
            )
        self._register_attempts.append(now)

    # -- OAuthAuthorizationServerProvider protocol -----------------------------

    async def get_client(
        self, client_id: str
    ) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(
        self, client_info: OAuthClientInformationFull
    ) -> None:
        """Register an MCP client with redirect_uri validation.

        Validates:
        1. Rate limiting — max REGISTER_RATE_LIMIT per REGISTER_RATE_WINDOW
        2. Client cap — max MAX_REGISTERED_CLIENTS total
        3. Redirect URIs — every URI must belong to ALLOWED_REDIRECT_DOMAINS
        """
        client_id = client_info.client_id or "unknown"

        # 1. Rate limiting
        self._check_register_rate_limit()

        # 2. Client cap
        if len(self._clients) >= MAX_REGISTERED_CLIENTS:
            logger.warning("Max registered clients reached (%d)", MAX_REGISTERED_CLIENTS)
            raise RegistrationError("Server has reached maximum client capacity.")

        # 3. Validate every redirect_uri
        redirect_uris = client_info.redirect_uris or []
        if not redirect_uris:
            logger.warning("Registration rejected: no redirect_uris, client_id=%s", client_id)
            raise RegistrationError("At least one redirect_uri is required.")

        for uri in redirect_uris:
            uri_str = str(uri)
            if not _validate_redirect_uri(uri_str):
                logger.warning(
                    "Registration rejected: invalid redirect_uri=%s, client_id=%s",
                    uri_str, client_id,
                )
                raise RegistrationError(
                    f"Redirect URI not allowed: {uri_str}. "
                    f"Only localhost and approved HTTPS domains are accepted."
                )

        # 4. Set default scope if not provided — MCP clients (like Lovable)
        # request "read write" in /authorize but may not include scope in
        # /register. Without this, validate_scope() rejects the request.
        if not client_info.scope:
            client_info.scope = SUPPORTED_SCOPES

        self._clients[client_info.client_id] = client_info
        self._save_clients()
        logger.info(
            "Registered MCP client: id=%s, name=%s, redirect_uris=%s, scope=%s",
            client_id,
            client_info.client_name or "(none)",
            [str(u) for u in redirect_uris],
            client_info.scope,
        )

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Start the OAuth flow by redirecting to Keycloak.

        NOTE: The MCP SDK validates redirect_uri BEFORE calling this method
        (in validate_redirect_uri). If you see "wasn't registered" errors,
        the redirect_uri in /authorize doesn't exactly match what was
        stored during /register.

        The MCP SDK calls this from the ``/authorize`` endpoint.  We:
        1. Generate our own PKCE pair for Keycloak
        2. Store pending auth state (keyed by a random ``state``)
        3. Return the Keycloak authorize URL — the MCP SDK redirects the
           user's browser there
        """
        # Generate PKCE for our connection to Keycloak
        pkce = generate_pkce_challenge()

        # Random state that ties the Keycloak callback to this auth request
        keycloak_state = secrets.token_urlsafe(32)

        # Our callback URL where Keycloak will redirect
        callback_url = f"{self.server_url}/callback"

        # Store everything we need when the callback comes back
        self._pending_auths[keycloak_state] = _PendingAuth(
            pkce=pkce,
            keycloak_redirect_uri=callback_url,
            client_redirect_uri=str(params.redirect_uri),
            client_state=params.state,
            client_id=client.client_id,
            scopes=params.scopes or [],
            code_challenge=params.code_challenge,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )

        # Build Keycloak authorize URL
        keycloak_url = build_authorize_url(
            code_challenge=pkce.code_challenge,
            redirect_uri=callback_url,
            state=keycloak_state,
        )

        return keycloak_url

    async def handle_keycloak_callback(
        self, keycloak_state: str, keycloak_code: str
    ) -> str:
        """Handle the Keycloak callback — called from the /callback route.

        This is NOT part of the OAuthAuthorizationServerProvider protocol.
        It's a custom method called by a Starlette route handler.

        Returns the redirect URL back to the MCP client with our own auth code.
        """
        pending = self._pending_auths.pop(keycloak_state, None)
        if pending is None:
            raise ValueError("Invalid or expired auth state")

        # Exchange the Keycloak code for Keycloak tokens
        kc_tokens = await exchange_code_for_tokens(
            code=keycloak_code,
            code_verifier=pending.pkce.code_verifier,
            redirect_uri=pending.keycloak_redirect_uri,
        )

        # Generate our own authorization code
        our_code = _generate_token()
        now = time.time()

        self._auth_codes[our_code] = AuthorizationCode(
            code=our_code,
            scopes=pending.scopes,
            expires_at=now + AUTH_CODE_LIFETIME,
            client_id=pending.client_id,
            code_challenge=pending.code_challenge,
            redirect_uri=AnyUrl(pending.client_redirect_uri),
            redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
            resource=pending.resource,
        )

        # Store Keycloak tokens alongside the auth code — returned directly in exchange_authorization_code
        self._auth_code_kc_tokens[our_code] = kc_tokens

        # Redirect to the MCP client's redirect_uri with our code
        return construct_redirect_uri(
            pending.client_redirect_uri,
            code=our_code,
            state=pending.client_state,
        )

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        auth_code = self._auth_codes.get(authorization_code)
        if auth_code is None:
            return None
        # Check expiry
        if time.time() > auth_code.expires_at:
            self._auth_codes.pop(authorization_code, None)
            return None
        # Check client
        if auth_code.client_id != client.client_id:
            return None
        return auth_code

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        """Return Keycloak tokens directly as MCP tokens — no secondary token generated."""
        self._auth_codes.pop(authorization_code.code, None)
        kc_tokens = self._auth_code_kc_tokens.pop(authorization_code.code, None)
        if kc_tokens is None:
            raise ValueError("Keycloak tokens not found for auth code")

        return OAuthToken(
            access_token=kc_tokens.access_token,
            token_type="Bearer",
            expires_in=kc_tokens.expires_in,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
            refresh_token=kc_tokens.refresh_token,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        """The refresh token IS the Keycloak refresh token — wrap without state lookup.

        Keycloak validates it when exchange_refresh_token calls the token endpoint.
        """
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=[],
            expires_at=int(time.time()) + REFRESH_TOKEN_LIFETIME,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Exchange Keycloak refresh token directly — returns fresh Keycloak tokens."""
        kc_response = None
        for kc_client_id in (KEYCLOAK_CLIENT_ID, KEYCLOAK_SDK_CLIENT_ID):
            try:
                kc_response = await refresh_tokens(refresh_token.token, client_id=kc_client_id)
                break
            except httpx.HTTPStatusError:
                continue
            except Exception as exc:
                logger.warning("Refresh token exchange error: %s", exc)
                raise

        if kc_response is None:
            raise ValueError("Refresh token rejected by Keycloak")

        new_scopes = scopes if scopes else refresh_token.scopes
        return OAuthToken(
            access_token=kc_response.access_token,
            token_type="Bearer",
            expires_in=kc_response.expires_in,
            scope=" ".join(new_scopes) if new_scopes else None,
            refresh_token=kc_response.refresh_token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Validate an access token. Called on every request.

        Two paths — both stateless:
        1. Keycloak JWT — JWKS signature + expiry validation
        2. Fastn API key — hex string accepted provisionally (backend validates)
        """
        logger.debug("load_access_token: token=%.8s..., len=%d", token, len(token))

        # Path 1: Keycloak JWT (JWKS validation — stateless)
        claims = await asyncio.to_thread(self._kc_jwt_validator.validate, token)
        if claims is not None:
            logger.debug("load_access_token: Path 1 JWT valid, azp=%s", claims.get("azp"))
            return AccessToken(
                token=token,
                client_id=claims.get("azp", claims.get("sub", "keycloak-direct")),
                scopes=claims.get("scope", "").split() if claims.get("scope") else [],
                expires_at=int(claims["exp"]) if claims.get("exp") else int(time.time()) + ACCESS_TOKEN_LIFETIME,
            )

        # Path 2: Fastn API key — hex string, not a JWT (no dots)
        # Accept provisionally; the Fastn backend validates when the SDK calls it.
        if "." not in token and all(c in "0123456789abcdef" for c in token.lower()):
            logger.debug("load_access_token: Path 2 API key accepted, token=%.8s...", token)
            return AccessToken(
                token=token,
                client_id="api-key",
                scopes=["read", "write"],
                expires_at=int(time.time()) + ACCESS_TOKEN_LIFETIME,
            )

        logger.debug("load_access_token: rejected, token=%.8s...", token)
        return None

    async def revoke_token(
        self, token: AccessToken | RefreshToken
    ) -> None:
        """No local state to revoke — tokens live in Keycloak."""
        pass
