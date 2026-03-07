"""OAuth and PKCE auth for the Fastn MCP server.

Two concerns live here:

1. **PKCE helpers** — used by the ``setup`` tool to do the manual
   Authorization-Code + PKCE flow against Fastn's Keycloak instance.
   These are low-level utilities and remain unchanged.

2. **FastnOAuthProvider** — implements the MCP SDK's
   ``OAuthAuthorizationServerProvider`` protocol so that MCP clients
   (Lovable, MCP Inspector, etc.) can authenticate via the standard
   MCP OAuth flow.  This provider proxies to Fastn's Keycloak and
   returns Keycloak tokens directly to the client — no opaque mapping.

Flow (MCP OAuth ↔ Keycloak):

    MCP Client           MCP Server              Keycloak
    ─────────           ──────────              ────────
    1. GET /authorize ──▶ provider.authorize()
                          builds Keycloak URL ──▶ /auth
    2.                    /callback?code=KC ◀── redirect
                          exchange KC code   ──▶ /token
                          store KC tokens temporarily
                          redirect client
       ◀── redirect with MCP code
    3. POST /token ──────▶ exchange_authorization_code()
                          return KC access_token + refresh_token
       ◀── {access_token, refresh_token}
    4. Bearer <kc_jwt> ──▶ load_access_token()
                           validate via JWKS (stateless)
    5. POST /token ──────▶ exchange_refresh_token()
       (refresh_token)     proxy to Keycloak /token
       ◀── {new access_token, new refresh_token}

Token lifetime is controlled entirely by Keycloak realm/client settings.
Set Access Token Lifespan on the fastn-oauth client in Keycloak admin.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
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
    TokenError,
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
ACCESS_TOKEN_LIFETIME = 86400 * 7   # 7 days — MCP clients rarely use refresh tokens
REFRESH_TOKEN_LIFETIME = 86400 * 30  # 30 days
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
    refresh_expires_in: int = 86400 * 7  # fallback if Keycloak doesn't send it
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
        refresh_expires_in=data.get("refresh_expires_in", 86400 * 7),
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
        refresh_expires_in=data.get("refresh_expires_in", 86400 * 7),
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
    This class only implements the business logic.

    All state is in-memory.  When the server restarts, clients re-authenticate
    (which is fine — they just redo the OAuth dance).
    """

    def __init__(self, server_url: str = "http://localhost:8000") -> None:
        self.server_url = server_url.rstrip("/")

        # In-memory stores
        self._clients: Dict[str, OAuthClientInformationFull] = {}
        self._pending_auths: Dict[str, _PendingAuth] = {}  # keyed by state
        self._auth_codes: Dict[str, AuthorizationCode] = {}
        # JWT validation cache: token → AccessToken (evicted on expiry)
        self._access_tokens: Dict[str, AccessToken] = {}
        # Temporary KC token storage during the OAuth dance (code: / code_ttl: / code_kc_refresh:)
        # and identity mapping for KC JWTs (token → token)
        self._keycloak_tokens: Dict[str, str] = {}
        # Direct Keycloak JWT validator (for Bearer token mode)
        self._kc_jwt_validator = _KeycloakJWTValidator()
        # Refresh token exchange state (for Bearer token mode with opaque refresh tokens)
        self._refresh_token_state: Dict[str, str] = {}  # bearer_key -> latest keycloak refresh token
        self._failed_refresh_tokens: Dict[str, float] = {}  # token -> failure timestamp (negative cache)
        self._refresh_client_ids: Dict[str, str] = {}  # bearer_key -> azp (client that issued the token)
        # Rate limiting for client registration (global, not per-client)
        self._register_attempts: list[float] = []

    # -- helpers ---------------------------------------------------------------

    def get_keycloak_token(self, mcp_token: str) -> str | None:
        """Return the Keycloak access token for a given MCP token.

        Since we now issue Keycloak tokens directly as MCP tokens, this is
        mostly an identity function — the MCP token IS the KC token.
        The dict lookup handles legacy opaque tokens and API keys.
        """
        cached = self._keycloak_tokens.get(mcp_token)
        if cached is not None:
            return cached
        # KC JWT passed directly: token IS the KC access token
        if "." in mcp_token:
            return mcp_token
        return None

    def cleanup_expired(self) -> int:
        """Remove expired tokens, auth codes, and stale pending auths.

        Returns the total number of entries removed. Call periodically
        (e.g. every 5 minutes) to bound memory usage.
        """
        now = time.time()
        removed = 0

        # Expired cached JWT access tokens (Path 2 cache in load_access_token)
        for token_str in list(self._access_tokens):
            at = self._access_tokens[token_str]
            if at.expires_at and now > at.expires_at:
                self._access_tokens.pop(token_str, None)
                self._keycloak_tokens.pop(token_str, None)
                self._refresh_token_state.pop(token_str, None)
                self._refresh_client_ids.pop(token_str, None)
                removed += 1

        # Expired auth codes
        for code_str in list(self._auth_codes):
            ac = self._auth_codes[code_str]
            if now > ac.expires_at:
                self._auth_codes.pop(code_str, None)
                self._keycloak_tokens.pop(f"code:{code_str}", None)
                self._keycloak_tokens.pop(f"code_ttl:{code_str}", None)
                self._keycloak_tokens.pop(f"code_kc_refresh:{code_str}", None)
                removed += 1

        # Stale pending auths — no timestamp available, so trim when > 100
        if len(self._pending_auths) > 100:
            # Remove oldest half (dict preserves insertion order in Python 3.7+)
            keys = list(self._pending_auths.keys())
            for k in keys[: len(keys) // 2]:
                self._pending_auths.pop(k, None)
                removed += 1

        # Stale failed-refresh negative cache (older than 5 minutes)
        for token_str in list(self._failed_refresh_tokens):
            if now - self._failed_refresh_tokens[token_str] > 300:
                self._failed_refresh_tokens.pop(token_str, None)
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

        # Temporarily store KC tokens until exchange_authorization_code retrieves them
        self._keycloak_tokens[f"code:{our_code}"] = kc_tokens.access_token
        self._keycloak_tokens[f"code_ttl:{our_code}"] = str(kc_tokens.expires_in)
        self._keycloak_tokens[f"code_kc_refresh:{our_code}"] = kc_tokens.refresh_token

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
        """Exchange our auth code for MCP tokens.

        Returns Keycloak tokens directly — no opaque MCP token mapping needed.
        The client stores and manages the KC access + refresh tokens.
        load_access_token validates KC JWTs via JWKS (stateless).
        exchange_refresh_token proxies to Keycloak (stateless).
        """
        self._auth_codes.pop(authorization_code.code, None)

        kc_token = self._keycloak_tokens.pop(f"code:{authorization_code.code}", None)
        kc_refresh = self._keycloak_tokens.pop(f"code_kc_refresh:{authorization_code.code}", None)
        kc_expires_in = int(self._keycloak_tokens.pop(f"code_ttl:{authorization_code.code}", None) or ACCESS_TOKEN_LIFETIME)

        if not kc_token or not kc_refresh:
            raise TokenError(error="invalid_grant", error_description="Authorization code not found or already used")

        return OAuthToken(
            access_token=kc_token,
            token_type="Bearer",
            expires_in=kc_expires_in,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
            refresh_token=kc_refresh,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        # Accept any non-empty opaque string as a potential Keycloak refresh token.
        # exchange_refresh_token will validate it by calling Keycloak — no server
        # state needed here.
        if not refresh_token:
            return None
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
        """Proxy the refresh token exchange directly to Keycloak — no server state."""
        kc_tokens = None
        for client_id in (KEYCLOAK_CLIENT_ID, KEYCLOAK_SDK_CLIENT_ID):
            try:
                kc_tokens = await refresh_tokens(refresh_token.token, client_id=client_id)
                break
            except httpx.HTTPStatusError:
                continue

        if kc_tokens is None:
            raise TokenError(error="invalid_grant", error_description="Invalid or expired refresh token")

        return OAuthToken(
            access_token=kc_tokens.access_token,
            token_type="Bearer",
            expires_in=kc_tokens.expires_in,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=kc_tokens.refresh_token,
        )

    async def _try_refresh_token_exchange(
        self, bearer_key: str, refresh_token_value: str,
        client_id: str = KEYCLOAK_CLIENT_ID,
    ) -> AccessToken | None:
        """Try to exchange a Keycloak refresh token for an access token.

        Args:
            bearer_key: The original token string the client sends as Bearer.
                        Used as the key in _access_tokens and _keycloak_tokens.
            refresh_token_value: The actual Keycloak refresh token to send
                                 to the token endpoint.  On first call this
                                 equals bearer_key; on re-refreshes it's the
                                 rotated refresh token from Keycloak.
            client_id: Keycloak client_id to use for the exchange.  Refresh
                       tokens are bound to their issuing client — must match.

        Returns:
            AccessToken if the exchange succeeded, None otherwise.
        """
        try:
            token_response = await refresh_tokens(refresh_token_value, client_id=client_id)
        except httpx.HTTPStatusError:
            logger.debug("Refresh token exchange failed for bearer key %.8s...", bearer_key)
            return None
        except (httpx.RequestError, Exception) as exc:
            logger.warning("Refresh token exchange error: %s", exc)
            return None

        # Validate the fresh access token JWT
        claims = await asyncio.to_thread(
            self._kc_jwt_validator.validate, token_response.access_token
        )
        if claims is None:
            logger.warning("Could not validate access token from refresh exchange")
            return None

        access_token = AccessToken(
            token=bearer_key,
            client_id=claims.get("azp", claims.get("sub", "keycloak-direct")),
            scopes=claims.get("scope", "").split() if claims.get("scope") else [],
            expires_at=int(claims["exp"]) if claims.get("exp") else int(time.time()) + token_response.expires_in,
        )

        # Cache keyed by the original bearer token
        self._access_tokens[bearer_key] = access_token
        # Map to the FRESH access token for SDK calls
        self._keycloak_tokens[bearer_key] = token_response.access_token
        # Store the NEW refresh token (Keycloak may rotate refresh tokens)
        self._refresh_token_state[bearer_key] = token_response.refresh_token
        # Remember which client issued the token (azp from JWT) for re-refreshes
        self._refresh_client_ids[bearer_key] = claims.get("azp", client_id)

        logger.debug(
            "Refresh exchange OK: bearer_key=%.8s..., fresh_access=%.8s..., client_id=%s, expires_at=%s",
            bearer_key, token_response.access_token, claims.get("azp", client_id), access_token.expires_at,
        )

        return access_token

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Validate an access token.  Called on every request.

        Three paths:
        1. Cached token — fast dict lookup (MCP OAuth + cached JWT/refresh results)
        2. Direct Keycloak JWT — JWKS signature validation (Bearer token mode)
        3. Keycloak refresh token — exchange for access token via token endpoint
        """
        logger.debug("load_access_token: token=%.8s..., len=%d", token, len(token))

        # Path 1: Cached token (in-memory dict lookup — fast path)
        at = self._access_tokens.get(token)
        if at is not None:
            if at.expires_at and time.time() > at.expires_at:
                # Token expired — check if this was a refresh-token-based session
                if token in self._refresh_token_state:
                    remembered_client = self._refresh_client_ids.get(token, KEYCLOAK_CLIENT_ID)
                    refreshed = await self._try_refresh_token_exchange(
                        token, self._refresh_token_state[token],
                        client_id=remembered_client,
                    )
                    if refreshed is not None:
                        return refreshed
                    # Refresh failed — clean up entirely
                    self._refresh_token_state.pop(token, None)
                    self._refresh_client_ids.pop(token, None)
                self._access_tokens.pop(token, None)
                self._keycloak_tokens.pop(token, None)
                logger.debug("load_access_token: Path 1 expired, refresh failed — token removed")
                return None
            logger.debug("load_access_token: Path 1 cache hit, client_id=%s", at.client_id)
            return at

        # Path 2: Try as direct Keycloak JWT (JWKS signature validation)
        claims = await asyncio.to_thread(self._kc_jwt_validator.validate, token)
        if claims is not None:
            access_token = AccessToken(
                token=token,
                client_id=claims.get("azp", claims.get("sub", "keycloak-direct")),
                scopes=claims.get("scope", "").split() if claims.get("scope") else [],
                expires_at=int(claims["exp"]) if claims.get("exp") else int(time.time()) + ACCESS_TOKEN_LIFETIME,
            )
            # Cache so subsequent requests with same token skip JWT decode
            self._access_tokens[token] = access_token
            # Identity mapping — the Keycloak JWT IS the token for SDK calls
            self._keycloak_tokens[token] = token
            logger.debug("load_access_token: Path 2 JWT valid, azp=%s", claims.get("azp"))
            return access_token

        # Path 2.5: Fastn API key — hex string, not a JWT (no dots)
        # Accept provisionally; the Fastn backend validates when the SDK calls it.
        if "." not in token and all(c in "0123456789abcdef" for c in token.lower()):
            access_token = AccessToken(
                token=token,
                client_id="api-key",
                scopes=["read", "write"],
                expires_at=int(time.time()) + ACCESS_TOKEN_LIFETIME,
            )
            self._access_tokens[token] = access_token
            # Identity mapping — _resolve_auth_token reads from _keycloak_tokens
            self._keycloak_tokens[token] = token
            logger.debug("load_access_token: Path 2.5 API key accepted, token=%.8s...", token)
            return access_token

        # Path 3: Try as Keycloak refresh token (opaque string → exchange for access token)
        # Refresh tokens are bound to their issuing client — try fastn-oauth, then fastn-sdk.
        # After the first success, azp from the JWT is stored for future re-refreshes.
        failed_at = self._failed_refresh_tokens.get(token)
        if failed_at and time.time() - failed_at < 60:
            return None  # Negative cache — don't retry failed tokens within 60s

        refreshed = await self._try_refresh_token_exchange(
            token, token, client_id=KEYCLOAK_CLIENT_ID,
        )
        if refreshed is None:
            refreshed = await self._try_refresh_token_exchange(
                token, token, client_id=KEYCLOAK_SDK_CLIENT_ID,
            )
        if refreshed is None:
            self._failed_refresh_tokens[token] = time.time()
            logger.debug("load_access_token: Path 3 refresh failed for token=%.8s...", token)
        else:
            logger.debug("load_access_token: Path 3 refresh succeeded for token=%.8s...", token)
        return refreshed

    async def revoke_token(
        self, token: AccessToken | RefreshToken
    ) -> None:
        """Revoke an access or refresh token.

        KC JWTs are stateless — we can only evict our local cache.
        The token remains valid at Keycloak until it expires naturally.
        """
        # Evict from JWT cache (populated by load_access_token Path 2)
        self._access_tokens.pop(token.token, None)
        self._keycloak_tokens.pop(token.token, None)
        self._refresh_token_state.pop(token.token, None)
        self._refresh_client_ids.pop(token.token, None)
