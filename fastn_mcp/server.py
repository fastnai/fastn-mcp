"""Fastn MCP Server — stateless translation layer wrapping the Fastn SDK.

Exposes MCP tools for AI agents and apps (Claude Desktop, Cursor, Lovable, and any MCP client):

  Tools:  find_tools, execute_tool, list_connectors
  Kit:    configure_connect_kit, configure_connect_kit_auth
  Admin:  list_projects, list_skills, deploy_flow
  Agents: list_flows, get_flow_schema, run_flow, delete_flow, generate_flow, update_flow

Transports:
  stdio            Local pipe (Claude Desktop / Cursor)
  sse              SSE + Streamable HTTP (remote, GET /sse + POST /shttp)
  streamable-http  Streamable HTTP only (POST /mcp)

Architecture:
  MCP Client → MCP Protocol → This Server → Fastn SDK → Fastn API
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re as _re
import time as _time
import threading
import uuid
from contextvars import ContextVar
from secrets import token_urlsafe
from typing import Any, Dict, Optional

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from fastn import (
    AsyncFastnClient,
    APIError,
    AuthError,
    ConfigError,
    ConnectorNotFoundError,
    FastnError,
    FlowNotFoundError,
)

from fastn_mcp.auth import FastnOAuthProvider

logger = logging.getLogger("fastn-mcp")

# GraphQL mutation for deleting flows (verified working via Fastn web app)
DELETE_SCHEMA_MUTATION = """
mutation deleteModelSchema($input: GetEntityInput!) {
  deleteModelSchema(input: $input)
}
"""

# GraphQL for Connect Kit metadata
_GET_KIT_METADATA_QUERY = """
query GetWidgetMetadata($input: GetEntityInput!) {
  widgetMetadata(input: $input) {
    authenticationApi
    isCustomAuthenticationEnabled
    filterWidgets
    showFilterBar
    showLabels
    isRBACEnabled
    styles
    disableFor
    isAIAgentWidgetEnabled
    labelsLayout
    advancedSettings
    widgetsMetrics
    __typename
  }
}
"""

_SAVE_KIT_METADATA_MUTATION = """
mutation SaveWidgetMetadata($input: SaveWidgetMetadataInput!) {
  saveWidgetMetadata(input: $input) {
    authenticationApi
    isCustomAuthenticationEnabled
    advancedSettings
    __typename
  }
}
"""

# GraphQL mutation for deploying flows to a stage
_DEPLOY_FLOW_MUTATION = """
mutation deployApiToStage($input: deployApiToStageInput!) {
  deployApiToStage(input: $input) {
    id
    __typename
  }
}
"""

# ---------------------------------------------------------------------------
# Default server instance — used for stdio transport and as fallback
# ---------------------------------------------------------------------------

_SERVER_INSTRUCTIONS = (
    "Fastn — 250+ pre-built connectors (Stripe, Slack, Jira, Salesforce, HubSpot, "
    "Postgres, and more) with managed OAuth, sub-second execution, and multi-step "
    "flow orchestration. "
    "Workflow: "
    "To browse or list connectors → list_connectors. "
    "To execute an action → find_tools → execute_tool. "
    "If find_tools returns nothing relevant → list_connectors (connector may need connecting). "
    "If any tool returns WORKSPACE_NOT_SET → list_projects, then pass project_id. "
    "For multi-step automations use generate_flow, then trigger with run_flow. "
    "When generating backend code (Edge Functions, API routes), call Fastn REST API "
    "at https://live.fastn.ai/api/fastn/executeTool with headers x-fastn-api-key and "
    "x-fastn-space-id. Never install per-service SDKs — Fastn handles all auth."
)

server = Server("fastn", instructions=_SERVER_INSTRUCTIONS)

# OAuth provider instance — set when auth is enabled (remote transports)
_oauth_provider: Optional["FastnOAuthProvider"] = None

# Module-level verbose flag — set by __main__.py when --verbose is passed
_verbose: bool = False

# Track active SSE sessions — used to detect dead sessions before POSTing.
# The MCP SDK's SseServerTransport closes stream writers when the GET drops
# but never removes the session from _read_stream_writers, so POSTs find a
# stale writer and silently drop messages. We maintain our own set to pre-check.
_active_sse_sessions: set[str] = set()

# ---------------------------------------------------------------------------
# Server mode — controls which tools are exposed (set once at startup)
# ---------------------------------------------------------------------------
# "agent" → all tools (Fastn + flows + config)
# "tools"   → Fastn tools only
_server_mode: str = "agent"
_server_project_id: str | None = None
_server_skill_id: str | None = None
_server_url: str | None = None

# ---------------------------------------------------------------------------
# Flow builder session store — two-tier (in-memory + optional Firestore)
# ---------------------------------------------------------------------------
_FLOW_SESSION_TTL = 1800  # 30 minutes
_FLOW_SESSION_MAX = 10_000  # max concurrent sessions (prevents OOM)
_FLOW_SESSION_MAX_USES = 5  # max retrievals before code is consumed

_flow_sessions: Dict[str, tuple[dict, float, int]] = {}  # code → (params, expiry_ts, uses_left)
_flow_sessions_lock = threading.Lock()

# Optional Firestore client — auto-detected on GCP Cloud Run
_firestore_db: Any = None
_FIRESTORE_COLLECTION = "fastn_mcp_flow_sessions"


def _init_firestore() -> None:
    """Try to connect to Firestore. No-op if unavailable (local dev)."""
    global _firestore_db
    try:
        from google.cloud import firestore  # type: ignore[import-untyped]
        _firestore_db = firestore.AsyncClient()
        logger.info("Firestore connected — flow sessions shared across instances")
    except Exception:
        logger.info("Firestore not available — flow sessions in-memory only")


def _evict_expired_sessions() -> None:
    """Remove expired entries from in-memory store. Caller must hold lock."""
    now = _time.time()
    expired = [k for k, (_, exp, uses) in _flow_sessions.items() if exp <= now or uses <= 0]
    for k in expired:
        del _flow_sessions[k]


async def _store_flow_session(params: dict) -> str:
    """Store flow builder params, return a cryptographically secure code."""
    code = token_urlsafe(16)  # 22-char URL-safe, 128-bit entropy
    expiry = _time.time() + _FLOW_SESSION_TTL

    # Local store
    with _flow_sessions_lock:
        if len(_flow_sessions) >= _FLOW_SESSION_MAX:
            _evict_expired_sessions()
        if len(_flow_sessions) >= _FLOW_SESSION_MAX:
            raise RuntimeError("Too many active flow sessions")
        _flow_sessions[code] = (params, expiry, _FLOW_SESSION_MAX_USES)

    # Firestore (best-effort, for cross-instance sharing)
    if _firestore_db is not None:
        try:
            await _firestore_db.collection(_FIRESTORE_COLLECTION).document(code).set({
                "params": json.dumps(params),
                "expiry": expiry,
                "uses_left": _FLOW_SESSION_MAX_USES,
            })
        except Exception as exc:
            logger.warning("Firestore write failed: %s", exc)

    return code


async def _pop_flow_session(code: str) -> dict | None:
    """Retrieve session params. Allows up to _FLOW_SESSION_MAX_USES reads before expiry."""
    # Try local first (fast path)
    with _flow_sessions_lock:
        entry = _flow_sessions.get(code)
        if entry is not None:
            params, expiry, uses_left = entry
            if _time.time() > expiry or uses_left <= 0:
                del _flow_sessions[code]
                return None
            _flow_sessions[code] = (params, expiry, uses_left - 1)
            return params

    # Fallback to Firestore (cross-instance)
    if _firestore_db is not None:
        try:
            doc_ref = _firestore_db.collection(_FIRESTORE_COLLECTION).document(code)
            doc = await doc_ref.get()
            if doc.exists:
                data = doc.to_dict()
                if data.get("expiry", 0) <= _time.time():
                    await doc_ref.delete()
                    return None
                uses_left = data.get("uses_left", 0)
                if uses_left <= 0:
                    await doc_ref.delete()
                    return None
                await doc_ref.update({"uses_left": uses_left - 1})
                return json.loads(data["params"])
        except Exception as exc:
            logger.warning("Firestore read failed: %s", exc)

    return None


def _cleanup_flow_sessions() -> None:
    """Purge expired in-memory sessions. Called from periodic cleanup loop."""
    with _flow_sessions_lock:
        _evict_expired_sessions()


FASTN_TOOL_NAMES = {"find_tools", "execute_tool", "list_connectors", "list_skills", "list_projects"}

# Request-scoped project_id — set per-connection (SSE) or per-request (shttp).
_request_project_id: ContextVar[str | None] = ContextVar("_request_project_id", default=None)

# Request-scoped skill_id — set when URL includes /tools/{project_id}/{skill_id}.
_request_skill_id: ContextVar[str | None] = ContextVar("_request_skill_id", default=None)

_FASTN_PATH_RE = _re.compile(r"^/tools(?:/([a-f0-9-]{36}))?(?:/([a-f0-9-]{36}))?$")


def _parse_mode_from_path(path: str) -> tuple[str, str | None, str | None]:
    """Parse mode, project_id, and skill_id from URL sub-path.

    "/" or ""                    → ("agent", None, None)
    "/tools"                      → ("tools", None, None)
    "/tools/<project_id>"         → ("tools", "<project_id>", None)
    "/tools/<project_id>/<skill>" → ("tools", "<project_id>", "<skill_id>")
    """
    m = _FASTN_PATH_RE.match(path)
    if m:
        return "tools", m.group(1), m.group(2)
    return "agent", None, None


# Tools available with API key authentication (no OAuth session)
API_KEY_TOOLS = {"find_tools", "execute_tool"}


def _is_api_key_auth() -> bool:
    """Check if the current request uses API key authentication."""
    if _oauth_provider is None:
        return False
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        mcp_token_obj = get_access_token()
        if mcp_token_obj is not None and mcp_token_obj.client_id == "api-key":
            return True
    except (LookupError, AttributeError, TypeError):
        pass
    return False


# ---------------------------------------------------------------------------
# Helpers: token resolution + SDK client creation
# ---------------------------------------------------------------------------

def _resolve_auth_token(arguments: Dict[str, Any]) -> tuple[str | None, str | None]:
    """Resolve the Keycloak auth token from available sources.

    Resolution order:
    1. OAuth provider (auth enabled) — MCP token → Keycloak token mapping
    2. HTTP Authorization: Bearer header — direct passthrough (works in --no-auth)
    3. Tool arguments (access_token field)

    Returns (token, source) where source is "args", "oauth", "bearer-header", or None.
    """
    auth_token = arguments.get("access_token")
    token_source = "args" if auth_token else None

    # If OAuth is active, use the Keycloak token from the auth context
    if _oauth_provider is not None:
        try:
            from mcp.server.auth.middleware.auth_context import get_access_token

            mcp_token_obj = get_access_token()
            if mcp_token_obj is not None:
                kc_token = _oauth_provider.get_keycloak_token(mcp_token_obj.token)
                if kc_token:
                    auth_token = kc_token
                    # Detect API key: identity-mapped (kc_token == mcp token)
                    # and hex format (not a JWT which has dots)
                    if kc_token == mcp_token_obj.token and "." not in kc_token:
                        token_source = "api-key"
                    else:
                        token_source = "oauth"
        except (LookupError, AttributeError, TypeError):
            pass  # Fall back to header / arguments

    # Fallback: extract Bearer token from HTTP Authorization header
    # Works in both --no-auth mode and when OAuth didn't resolve a token.
    # Note: server.request_context reads a shared ContextVar (request_ctx)
    # that any active MCP Server instance populates — this works regardless
    # of which Server instance is processing the current request.
    if auth_token is None:
        try:
            ctx = server.request_context
            if ctx.request is not None:
                auth_header = ctx.request.headers.get("authorization", "")
                if auth_header.lower().startswith("bearer "):
                    auth_token = auth_header[7:]
                    token_source = "bearer-header"
        except LookupError:
            pass  # No request context (e.g. stdio transport)

    return auth_token, token_source


def _resolve_request_headers() -> dict[str, str]:
    """Extract Fastn headers from the current HTTP request, if any.

    Reads x-project-id from request headers.
    API keys and auth tokens are passed via the Authorization header
    and resolved by _resolve_auth_token.
    """
    try:
        ctx = server.request_context
        if ctx.request is not None:
            headers = ctx.request.headers
            return {
                "project_id": headers.get("x-project-id", ""),
            }
    except LookupError:
        pass  # No request context (e.g. stdio transport)
    return {}


def _get_client(arguments: Dict[str, Any]) -> AsyncFastnClient:
    """Create an AsyncFastnClient from tool arguments."""
    auth_token, token_source = _resolve_auth_token(arguments)
    req_headers = _resolve_request_headers()

    # Log token details for debugging INVALID_TOKEN errors
    token_preview = None
    if auth_token and len(auth_token) > 16:
        token_preview = f"{auth_token[:8]}...{auth_token[-8:]}"
    elif auth_token:
        token_preview = f"{auth_token[:4]}..."

    logger.debug(
        "SDK client: token_source=%s, has_token=%s, token_preview=%s",
        token_source, auth_token is not None, token_preview,
    )

    # Resolution priority: tool arguments > request headers > request contextvar > startup config > SDK defaults
    project_id = (
        arguments.get("project_id")
        or req_headers.get("project_id")
        or _request_project_id.get()
        or _server_project_id
    )
    tenant_id = arguments.get("tenant_id", "")

    # When the Bearer token is a Fastn API key, pass it as api_key
    # so the SDK sends it as x-fastn-api-key header (not Bearer).
    api_key = arguments.get("api_key")
    if token_source == "api-key":
        api_key = auth_token
        auth_token = None

    return AsyncFastnClient(
        api_key=api_key,
        project_id=project_id,
        auth_token=auth_token,
        tenant_id=tenant_id,
        verbose=_verbose,
    )


def _error_result(error_code: str, message: str) -> list[TextContent]:
    """Build an MCP error response."""
    return [TextContent(
        type="text",
        text=json.dumps({"error": error_code, "message": message}),
    )]


def _success_result(data: Any) -> list[TextContent]:
    """Build an MCP success response."""
    return [TextContent(
        type="text",
        text=json.dumps(data, default=str),
    )]


@contextlib.asynccontextmanager
async def _sdk_client(arguments: Dict[str, Any]):
    """Create an SDK client and ensure it is closed after use."""
    client = _get_client(arguments)
    try:
        yield client
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

# -- Reusable sub-schemas (inlined from theme.mcp.schema.json $defs) --------

_CSS_COLOR = {"type": "string", "description": "CSS color: hex, rgb(), rgba(), hsl()"}
_CSS_SIZE = {"type": "string", "description": "CSS size (px/rem/em/%)"}
_CSS_SPACING = {"type": "string", "description": "CSS spacing (1-4 values, px/rem/em)"}
_CSS_RADIUS = {"type": "string", "description": "CSS border-radius"}
_FONT_WEIGHT = {
    "type": "integer",
    "description": "Font weight (100-900, multiples of 100)",
    "minimum": 100,
    "maximum": 900,
}

_LIGHT_DARK_COLOR = {
    "type": "object",
    "description": "Color split by light/dark mode.",
    "additionalProperties": False,
    "properties": {
        "light": _CSS_COLOR,
        "dark": _CSS_COLOR,
    },
}

_SEMANTIC_COLORS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "text": _CSS_COLOR,
        "success": _CSS_COLOR,
        "error": _CSS_COLOR,
    },
}

_BUTTON_HOVER = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "backgroundColor": _CSS_COLOR,
        "textColor": _CSS_COLOR,
    },
}

_BUTTON_VARIANT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "backgroundColor": _CSS_COLOR,
        "textColor": _CSS_COLOR,
        "hover": _BUTTON_HOVER,
    },
}

_BUTTON_VARIANT_WITH_BORDER = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "backgroundColor": _CSS_COLOR,
        "textColor": _CSS_COLOR,
        "hover": _BUTTON_HOVER,
        "border": {"type": "string", "description": "CSS border, e.g. '1px solid #4338ca'"},
    },
}

_BUTTON_THEME = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "primary": _BUTTON_VARIANT,
        "secondary": _BUTTON_VARIANT_WITH_BORDER,
        "tertiary": _BUTTON_VARIANT_WITH_BORDER,
    },
}

_CARD_DISABLED = {
    "type": "object",
    "description": "Disabled-state overrides for cards.",
    "additionalProperties": False,
    "properties": {
        "cursor": {"type": "string", "default": "not-allowed"},
        "opacity": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.95},
        "pointerEvents": {"type": "string", "default": "none"},
        "backgroundColor": _CSS_COLOR,
    },
}

_CARD_MODE_STYLE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "width": {"type": "string", "description": "CSS width"},
        "minWidth": {"type": "string", "description": "CSS min-width"},
        "height": {"type": "string", "description": "CSS height"},
        "padding": _CSS_SPACING,
        "border": {"type": "string", "description": "CSS border"},
        "borderRadius": _CSS_RADIUS,
        "boxShadow": {"type": "string", "description": "CSS box-shadow"},
        "disabled": _CARD_DISABLED,
    },
}

_TYPOGRAPHY_BLOCK = {
    "type": "object",
    "description": "Typography token group.",
    "additionalProperties": False,
    "properties": {
        "fontSize": _CSS_SIZE,
        "lineHeight": _CSS_SIZE,
        "fontWeight": _FONT_WEIGHT,
    },
}

_FILTER_BUTTON_STYLE = {
    "type": "object",
    "description": "Button styles inside filter UI.",
    "additionalProperties": False,
    "properties": {
        "fontSize": {**_CSS_SIZE, "default": "14px"},
        "fontWeight": {**_FONT_WEIGHT, "default": 500},
        "padding": {**_CSS_SPACING, "default": "8px 20px"},
        "borderRadius": {**_CSS_RADIUS, "default": "24px"},
        "primary": _BUTTON_VARIANT,
        "secondary": _BUTTON_VARIANT_WITH_BORDER,
    },
}

_FILTER_MODE_STYLE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "backgroundColor": _CSS_COLOR,
        "button": _FILTER_BUTTON_STYLE,
    },
}

# -- Complete theme schema (matches theme.mcp.schema.json) -------------------

_THEME_SCHEMA: dict = {
    "type": "object",
    "description": (
        "Theme configuration for the Connect Kit widget. Supports light/dark "
        "modes. All fields have sensible defaults — only override the fields "
        "you need to change."
    ),
    "additionalProperties": False,
    "required": ["fontFamily"],
    "properties": {
        "mode": {
            "type": "string",
            "description": "Preferred color mode.",
            "enum": ["light", "dark", "system"],
            "default": "system",
        },
        "fontFamily": {
            "type": "string",
            "description": "CSS font-family stack.",
            "default": "Inter, system-ui, sans-serif",
        },
        "backgroundColor": {
            **_LIGHT_DARK_COLOR,
            "description": "Primary surface background color.",
        },
        "secondaryBackgroundColor": {
            **_LIGHT_DARK_COLOR,
            "description": "Secondary surface background (filters, panels).",
        },
        "color": {
            "type": "object",
            "description": "Semantic text + status colors per mode.",
            "additionalProperties": False,
            "properties": {
                "light": _SEMANTIC_COLORS,
                "dark": _SEMANTIC_COLORS,
            },
        },
        "card": {
            "type": "object",
            "description": "Card component styling per mode.",
            "additionalProperties": False,
            "properties": {
                "light": _CARD_MODE_STYLE,
                "dark": _CARD_MODE_STYLE,
            },
        },
        "button": {
            "type": "object",
            "description": "Button base sizing + per-mode variants.",
            "additionalProperties": False,
            "properties": {
                "fontSize": {**_CSS_SIZE, "default": "16px"},
                "fontWeight": {**_FONT_WEIGHT, "default": 400},
                "padding": {**_CSS_SPACING, "default": "8px 20px"},
                "lineHeight": {**_CSS_SIZE, "default": "20px"},
                "borderRadius": {**_CSS_RADIUS, "default": "24px"},
                "light": _BUTTON_THEME,
                "dark": _BUTTON_THEME,
            },
        },
        "avatar": {
            "type": "object",
            "description": "Avatar image shape and sizing.",
            "additionalProperties": False,
            "properties": {
                "width": {**_CSS_SIZE, "default": "46px"},
                "height": {**_CSS_SIZE, "default": "46px"},
                "borderRadius": {
                    "type": "string",
                    "description": "CSS border-radius (px/rem/%).",
                    "default": "12%",
                },
            },
        },
        "header": {**_TYPOGRAPHY_BLOCK, "description": "Header typography."},
        "title": {**_TYPOGRAPHY_BLOCK, "description": "Title typography."},
        "description": {**_TYPOGRAPHY_BLOCK, "description": "Description typography."},
        "content": {**_TYPOGRAPHY_BLOCK, "description": "Body/content typography."},
        "popModalPosition": {
            "type": "string",
            "description": "Popover/modal anchor position.",
            "enum": [
                "topCenter", "topLeft", "topRight",
                "bottomCenter", "bottomLeft", "bottomRight",
                "center",
            ],
            "default": "topCenter",
        },
        "modal": {
            "type": "object",
            "description": "Modal overlay and content styling.",
            "additionalProperties": False,
            "properties": {
                "overlay": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "backgroundColor": {**_CSS_COLOR, "default": "rgba(0, 0, 0, 0.5)"},
                        "backdropFilter": {
                            "type": "string",
                            "description": "CSS backdrop-filter.",
                            "default": "blur(10px)",
                        },
                    },
                },
                "content": {
                    "type": "object",
                    "description": "Free-form modal content style overrides.",
                    "additionalProperties": True,
                },
            },
        },
        "filterStyles": {
            "type": "object",
            "description": "Filter bar/panel styling per mode.",
            "additionalProperties": False,
            "properties": {
                "light": _FILTER_MODE_STYLE,
                "dark": _FILTER_MODE_STYLE,
            },
        },
    },
}

TOOLS = [
    # =====================================================================
    # TOOLS — Discovery and execution
    # Browse: list_connectors
    # Execute: find_tools → execute_tool
    # =====================================================================
    Tool(
        name="find_tools",
        description=(
            "Search for connector tools to perform a specific action "
            "(send message, create ticket, query database, send email, "
            "etc). Returns tools that are active and connected in this "
            "project with actionId and inputSchema. Next step: pass the "
            "actionId to execute_tool. If no relevant results, call "
            "list_connectors — the connector may exist but not be "
            "connected yet."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Describe what you need, including context about what you are building (e.g. 'send a Slack notification when a new order is placed' rather than just 'slack'). Richer prompts return more relevant tools.",
                },
                "goal": {
                    "type": "string",
                    "description": "The broader objective you are working toward (e.g. 'build a CRM dashboard', 'e-commerce checkout flow'). Helps rank tools by relevance.",
                },
                "platform": {
                    "type": "string",
                    "description": "AI platform making this call (e.g. 'lovable', 'cursor', 'claude-desktop', 'bolt', 'v0'). Set via project instructions.",
                },
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Narrow results to connector domains (e.g. ['payments', 'crm', 'messaging', 'database', 'email', 'project-management'])",
                },
            },
            "required": ["prompt"],
        },
    ),
    Tool(
        name="execute_tool",
        description=(
            "Execute a connector tool by its actionId. Call find_tools "
            "first to get the actionId and inputSchema, then call this with "
            "the action_id and matching parameters. Returns the result "
            "directly. Only use actionIds returned by find_tools — do not "
            "guess or fabricate IDs."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "string",
                    "description": "The actionId returned by find_tools",
                },
                "parameters": {
                    "type": "object",
                    "description": "Key-value parameters matching the tool's inputSchema",
                },
                "connection_id": {
                    "type": "string",
                    "description": "Connection ID when a connector has multiple connections (optional)",
                },
            },
            "required": ["action_id", "parameters"],
        },
    ),
    Tool(
        name="list_connectors",
        description=(
            "Browse all available connectors (200+) including Slack, "
            "Jira, GitHub, Salesforce, Gmail, Stripe, and more. Use this "
            "when the user asks what connectors or integrations are "
            "available, or to check if a specific connector exists. "
            "Returns connector names and a connect_url to enable them. "
            "Also call this when find_tools returns no relevant results "
            "— the connector may need connecting first. Use query to "
            "filter by name (e.g. 'teams', 'jira')."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Filter by connector name (e.g. 'slack', 'jira')",
                },
            },
        },
    ),
    # =====================================================================
    # ADMIN — Project configuration and management
    # =====================================================================
    Tool(
        name="list_skills",
        description=(
            "List available skills in the current project. Each skill is a "
            "scoped set of tools and instructions for a specific capability "
            "(e.g. customer onboarding, incident response). Returns skill "
            "names, descriptions, and IDs."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="list_projects",
        description=(
            "List available projects (workspaces) for the authenticated "
            "user. Call this when any tool returns a WORKSPACE_NOT_SET "
            "error, or when the user wants to switch projects. Returns "
            "project IDs and names. IMPORTANT: After listing, you MUST "
            "present the projects to the user and let them choose which "
            "one to use. Then pass the selected project_id in all "
            "subsequent tool calls."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="deploy_flow",
        description=(
            "Deploy a flow to a specific stage (DRAFT or LIVE). Use this to "
            "activate flow changes in production. Required after: "
            "(1) configure_connect_kit_auth — call with "
            'flow_id="fastnCustomAuth" stage="LIVE", '
            "(2) any flow created via generate_flow — deploy to LIVE "
            "when the user is ready to activate it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "flow_id": {
                    "type": "string",
                    "description": "The flow ID to deploy (e.g. 'fastnCustomAuth')",
                },
                "stage": {
                    "type": "string",
                    "enum": ["DRAFT", "LIVE"],
                    "description": "Target stage — DRAFT or LIVE",
                    "default": "LIVE",
                },
                "comment": {
                    "type": "string",
                    "description": "Optional deployment comment",
                },
            },
            "required": ["flow_id"],
        },
    ),
    # =====================================================================
    # AGENTS — Flow automation
    # Manage saved automations (flows). Use list_flows to see existing
    # flows, run_flow to execute them, delete_flow to remove them.
    # generate_flow opens an interactive flow builder.
    # =====================================================================
    Tool(
        name="list_flows",
        description=(
            "List saved automations (flows) in the current project. "
            "Returns flow_id, name, and status for each flow. Use the "
            "flow_id with run_flow to execute or delete_flow to remove."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": 'Filter by status: "active", "paused", "error". Omit for all.',
                },
            },
        },
    ),
    Tool(
        name="get_flow_schema",
        description=(
            "Get the input schema of a specific flow. Each flow has its own "
            "unique input parameters — you MUST call this for the specific "
            "flow_id you intend to run BEFORE calling run_flow. Returns "
            "field names and a JSON Schema describing the expected input. "
            "Do NOT reuse or guess parameters from other flows."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "flow_id": {
                    "type": "string",
                    "description": "The flow_id from list_flows",
                },
            },
            "required": ["flow_id"],
        },
    ),
    Tool(
        name="run_flow",
        description=(
            "Execute a saved automation by its flow_id. IMPORTANT: You MUST "
            "call get_flow_schema for this specific flow_id first to discover "
            "its required input parameters. Each flow has different parameters "
            "— never guess or reuse parameters from another flow."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "flow_id": {
                    "type": "string",
                    "description": "The flow_id from list_flows",
                },
                "parameters": {
                    "type": "object",
                    "description": "Optional input parameters for the flow",
                },
            },
            "required": ["flow_id"],
        },
    ),
    Tool(
        name="delete_flow",
        description=(
            "Delete an automation by its flow_id. Moves the flow to trash. "
            "Get the flow_id from list_flows first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "flow_id": {
                    "type": "string",
                    "description": "The flow_id from list_flows",
                },
            },
            "required": ["flow_id"],
        },
    ),
    Tool(
        name="generate_flow",
        description=(
            "Create an automation flow interactively. Returns a popup_url "
            "to the fastn flow builder where the user can describe what "
            "they want, answer clarifying questions, and see the generated "
            "flow — all without going back and forth through the LLM. "
            "The project_id is resolved automatically from the current "
            "session context. IMPORTANT: Always present the popup_url to "
            "the user as a clickable markdown link: "
            "[Open Flow Builder](popup_url). Do NOT show the raw URL."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "What the user wants to automate, e.g. 'Send a Slack message when a Jira ticket is created'",
                },
            },
            "required": ["prompt"],
        },
    ),
    Tool(
        name="update_flow",
        description=(
            "(Under development) Update an existing automation. "
            "Not yet available."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "flow_id": {
                    "type": "string",
                    "description": "The flow_id to update",
                },
                "prompt": {
                    "type": "string",
                    "description": "Plain English description of what to change",
                },
            },
            "required": ["flow_id"],
        },
    ),
    # =====================================================================
    # KIT — Connect Kit auth and styling
    # =====================================================================
    Tool(
        name="configure_connect_kit_auth",
        description=(
            "Register a custom auth provider so Fastn can validate end-user "
            "tokens. IMPORTANT: You MUST ask the user for both the auth_url "
            "and user_token values — do NOT fabricate, guess, or use "
            "placeholder values. The auth_url is ONLY the base URL of the "
            "auth provider (no tokens, no query params). The user_token is "
            "a separate parameter — a real JWT string the user provides. "
            "After configuring, you MUST call deploy_flow with "
            'flow_id="fastnCustomAuth" and stage="LIVE" to activate it.'
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "auth_url": {
                    "type": "string",
                    "description": (
                        "Base URL of the auth provider ONLY — do NOT append "
                        "tokens or paths. Examples: "
                        "'https://auth.example.com/realms/myapp' (Keycloak), "
                        "'https://xyz.supabase.co' (Supabase), "
                        "'https://accounts.google.com' (OIDC issuer). "
                        "The userinfo endpoint is resolved automatically."
                    ),
                },
                "user_token": {
                    "type": "string",
                    "description": (
                        "A real JWT token string from the user — ask the user "
                        "to provide this value. Must be a valid token that "
                        "works against the auth_url provider. Do NOT use "
                        "placeholders or variable names."
                    ),
                },
                "user_id": {
                    "type": "string",
                    "description": (
                        "The logged-in user's unique ID. Connector connections "
                        "are saved under this identity. If omitted, extracted "
                        "from the 'sub' claim in the userinfo response."
                    ),
                },
            },
            "required": ["auth_url", "user_token"],
        },
    ),
    Tool(
        name="configure_connect_kit",
        description=(
            "Update the Connect Kit styling for the current project. "
            "The styles schema describes every supported field with types, "
            "defaults, and constraints — use it to generate a valid theme. "
            "Top-level style keys: mode, fontFamily, backgroundColor, "
            "secondaryBackgroundColor, color, card, button, avatar, header, "
            "title, description, content, popModalPosition, modal, filterStyles."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "styles": _THEME_SCHEMA,
            },
            "required": ["styles"],
        },
    ),
]


# ---------------------------------------------------------------------------
# Register handlers
# ---------------------------------------------------------------------------

@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    """Return tools based on server mode (stdio only).

    HTTP transports use per-path Server instances instead.
    """
    if _is_api_key_auth():
        return [t for t in TOOLS if t.name in API_KEY_TOOLS]
    if _server_mode == "tools":
        names = FASTN_TOOL_NAMES
        if _server_project_id:
            names = names - {"list_projects"}
        if _server_skill_id:
            names = names - {"list_skills"}
        return [t for t in TOOLS if t.name in names]
    return TOOLS


_SENSITIVE_KEYS = {"authorization", "cookie", "x-fastn-api-key", "access_token", "api_key"}


def _redact(data: dict, *, mask: str = "***") -> dict:
    """Redact sensitive keys from a dict for safe logging."""
    return {k: mask if k.lower() in _SENSITIVE_KEYS else v for k, v in data.items()}


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch a tool call to the appropriate handler."""
    # Log incoming request with headers
    request_headers: dict = {}
    try:
        ctx = server.request_context
        if ctx.request is not None:
            request_headers = dict(ctx.request.headers)
    except LookupError:
        pass  # No request context (e.g. stdio transport)

    logger.info(
        "Tool request: %s | args: %s | headers: %s",
        name,
        json.dumps(_redact(arguments), default=str),
        json.dumps(_redact(request_headers)),
    )

    if _is_api_key_auth() and name not in API_KEY_TOOLS:
        result = _error_result(
            "TOOL_NOT_AVAILABLE",
            f"Tool '{name}' is not available with API key authentication. "
            "Only find_tools and execute_tool are supported.",
        )
        logger.warning("Tool response: %s | %s", name, result[0].text)
        return result

    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        result = _error_result("UNKNOWN_TOOL", f"Unknown tool: {name}")
        logger.warning("Tool response: %s | %s", name, result[0].text)
        return result
    try:
        result = await handler(arguments)
        logger.info("Tool response: %s | %s", name, result[0].text[:500])
        return result
    except ConfigError as exc:
        logger.warning("Tool config error: %s | %s", name, exc)
        return _error_result(
            "WORKSPACE_NOT_SET",
            "No project selected. Call list_projects to see available "
            "projects, then pass the chosen project_id in your next tool call.",
        )
    except AuthError as exc:
        logger.warning("Tool auth error: %s | %s", name, exc)
        return _error_result("INVALID_TOKEN", str(exc))
    except ConnectorNotFoundError as exc:
        logger.warning("Connector not found: %s | %s", name, exc)
        return _error_result("CONNECTOR_NOT_FOUND", str(exc))
    except FastnError as exc:
        logger.warning("Tool SDK error: %s | %s", name, exc)
        return _error_result("FASTN_ERROR", str(exc))
    except Exception as exc:
        logger.exception("Tool exception: %s | %s", name, exc)
        return _error_result("INTERNAL_ERROR", f"Unexpected error: {exc}")


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

async def _handle_generate_flow(arguments: dict) -> list[TextContent]:
    """Return a popup URL for the interactive flow builder.

    The popup handles the multi-turn conversation directly with the
    fastn API — no LLM mediation needed.
    """
    prompt = arguments.get("prompt", "")
    if not prompt:
        return _error_result("MISSING_PARAM", "prompt is required")

    # Resolve auth + project via SDK client (same pattern as deploy_flow, etc.)
    async with _sdk_client(arguments) as client:
        auth_token, _ = _resolve_auth_token(arguments)
        if not auth_token:
            return _error_result(
                "INVALID_TOKEN",
                "No auth token available. Authenticate via OAuth.",
            )

        project_id = client._config.resolve_project_id()
        if not project_id:
            return _error_result(
                "MISSING_PARAM",
                "project_id could not be resolved. Pass it as a tool argument or via x-project-id header.",
            )

    # Generate session ID for this flow builder conversation
    session_id = str(uuid.uuid4())

    # Resolve server base URL
    server_base = _server_url
    if not server_base:
        return _error_result(
            "CONFIG_ERROR",
            "Cannot determine server URL. Pass --server-url.",
        )

    # Store params server-side, get a short code
    try:
        code = await _store_flow_session({
            "token": auth_token,
            "project": project_id,
            "session": session_id,
            "prompt": prompt,
        })
    except RuntimeError:
        return _error_result(
            "CAPACITY_EXCEEDED",
            "Too many active flow builder sessions. Try again later.",
        )

    popup_url = f"{server_base.rstrip('/')}/fb/{code}"

    return _success_result({
        "popup_url": popup_url,
        "session_id": session_id,
        "project_id": project_id,
        "message": (
            f"Your flow builder is ready! "
            f"[Open Flow Builder]({popup_url})"
        ),
    })


async def _handle_update_flow(arguments: dict) -> list[TextContent]:
    """Update a flow — under development."""
    return _error_result("UNDER_DEVELOPMENT", "Flow updates are under development.")


async def _handle_delete_flow(arguments: dict) -> list[TextContent]:
    """Delete a flow via GraphQL deleteModelSchema mutation."""
    from fastn._http import _gql_call_async

    flow_id = arguments.get("flow_id", "")
    if not flow_id:
        return _error_result("MISSING_PARAM", "flow_id is required")

    async with _sdk_client(arguments) as client:
        workspace_id = client._config.resolve_project_id()
        variables = {"input": {"clientId": workspace_id, "id": flow_id, "resourceActionType": "TRASH"}}
        result = await _gql_call_async(client, DELETE_SCHEMA_MUTATION, variables)
        return _success_result({"deleted": True, "flow_id": flow_id, "result": result.get("deleteModelSchema")})


async def _handle_get_flow_schema(arguments: dict) -> list[TextContent]:
    """Get the input schema for a specific flow."""
    flow_id = arguments.get("flow_id", "")
    if not flow_id:
        return _error_result("MISSING_PARAM", "flow_id is required")

    async with _sdk_client(arguments) as client:
        result = await client.flows.schema(flow_id)
        return _success_result(result)


async def _handle_run_flow(arguments: dict) -> list[TextContent]:
    """Execute a flow by its flow_id."""
    flow_id = arguments.get("flow_id", "")
    if not flow_id:
        return _error_result("MISSING_PARAM", "flow_id is required")

    async with _sdk_client(arguments) as client:
        result = await client.flows.run(flow_id, input_data=arguments.get("parameters", {}))
        return _success_result(result)


async def _handle_list_flows(arguments: dict) -> list[TextContent]:
    """List all flows in the project."""
    async with _sdk_client(arguments) as client:
        result = await client.flows.list(status=arguments.get("status"))
        return _success_result({"flows": result, "total": len(result)})


async def _handle_find_tools(arguments: dict) -> list[TextContent]:
    """Find relevant tools using natural language."""
    prompt = arguments.get("prompt", "")
    if not prompt:
        return _error_result("MISSING_PARAM", "prompt is required")

    goal = arguments.get("goal")
    platform = arguments.get("platform")
    categories = arguments.get("categories")
    if goal or platform or categories:
        logger.info(
            "find_tools meta | goal: %s | platform: %s | categories: %s | prompt: %s",
            goal, platform, categories, prompt,
        )

    async with _sdk_client(arguments) as client:
        tools = await client.get_tools_for(prompt=prompt, format="raw", limit=5)
        return _success_result({"tools": tools, "total": len(tools)})


async def _handle_list_connectors(arguments: dict) -> list[TextContent]:
    """Browse all available connectors in the project."""
    async with _sdk_client(arguments) as client:
        workspace_id = client._config.resolve_project_id()
        skill_id = (
            _request_skill_id.get()
            or _server_skill_id
            or workspace_id
        )
        connectors = await client.connectors.list()
        query = arguments.get("query", "")
        if query:
            q = query.lower()
            connectors = [
                c for c in connectors
                if q in c["name"].lower() or q in c.get("display_name", "").lower()
            ]
        base_url = f"https://app.fastn.ai/projects/{workspace_id}/fastn/{skill_id}"
        results = [
            {
                "name": c["name"],
                "connect_url": f"{base_url}?open=managetools&query={c.get('display_name') or c['name']}",
            }
            for c in connectors
        ]
        return _success_result({"connectors": results, "total": len(results)})


async def _handle_execute_tool(arguments: dict) -> list[TextContent]:
    """Execute a tool by its actionId."""
    action_id = arguments.get("action_id", "")
    if not action_id:
        return _error_result("MISSING_PARAM", "action_id is required")

    params = arguments.get("parameters", {})
    if not isinstance(params, dict):
        return _error_result("INVALID_PARAM", "parameters must be an object")

    async with _sdk_client(arguments) as client:
        result = await client.execute(
            tool=action_id, params=params, connection_id=arguments.get("connection_id"),
        )
        return _success_result(result)


async def _resolve_userinfo_url(url: str, http) -> str | None:
    """Resolve an auth-related URL to the correct userinfo endpoint.

    Supports:
    - Direct userinfo URLs (passthrough)
    - Keycloak realm URLs → .../protocol/openid-connect/userinfo
    - Supabase project URLs → .../auth/v1/user
    - OIDC Discovery → fetch .well-known/openid-configuration
    """
    from urllib.parse import urlparse

    parsed = urlparse(url.rstrip("/"))
    path = parsed.path

    # Already a userinfo endpoint (direct URL or custom function)
    if path.endswith(("/userinfo", "/user", "/auth-userinfo")):
        return url

    # Keycloak: .../realms/<realm> → .../realms/<realm>/protocol/openid-connect/userinfo
    if "/realms/" in path:
        base = url.rstrip("/")
        # Strip trailing /protocol/... if partially provided
        if "/protocol/" in base:
            base = base[: base.index("/protocol/")]
        return f"{base}/protocol/openid-connect/userinfo"

    # Supabase — only rewrite bare project URLs (no meaningful path)
    # Custom endpoints like /functions/v1/auth-userinfo are already handled above
    host = parsed.hostname or ""
    if ("supabase.co" in host or "supabase.in" in host) and not path.strip("/"):
        return f"{parsed.scheme}://{parsed.netloc}/auth/v1/user"

    # OIDC Discovery
    discovery_url = f"{url.rstrip('/')}/.well-known/openid-configuration"
    try:
        resp = await http.get(discovery_url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            endpoint = data.get("userinfo_endpoint")
            if endpoint:
                return endpoint
    except Exception:
        pass

    return None


async def _handle_configure_connect_kit_auth(arguments: dict) -> list[TextContent]:
    """Register a custom auth provider with Fastn."""
    import httpx
    from fastn._http import _gql_call_async
    from fastn._constants import _UPDATE_RESOLVER_STEP_MUTATION
    from fastn._auth_ns import _build_custom_auth_step

    auth_url = arguments.get("auth_url", "")
    if not auth_url:
        return _error_result("MISSING_PARAM", "auth_url is required")

    user_token = arguments.get("user_token", "")
    if not user_token:
        return _error_result("MISSING_PARAM", "user_token is required")

    async with httpx.AsyncClient() as http:
        # Resolve the auth URL to a userinfo endpoint
        userinfo_url = await _resolve_userinfo_url(auth_url, http)
        if not userinfo_url:
            return _error_result(
                "RESOLUTION_FAILED",
                f"Could not resolve a userinfo endpoint from '{auth_url}'. "
                "Provide an OIDC issuer URL, Keycloak realm URL, Supabase "
                "project URL, or a direct userinfo endpoint URL.",
            )

        # Verify the resolved endpoint works with the provided token
        try:
            resp = await http.get(
                userinfo_url,
                headers={"Authorization": f"Bearer {user_token}"},
                timeout=10,
            )
        except httpx.RequestError as exc:
            return _error_result(
                "VERIFICATION_FAILED",
                f"Could not reach userinfo endpoint ({userinfo_url}): {exc}",
            )

    if resp.status_code != 200:
        return _error_result(
            "VERIFICATION_FAILED",
            f"Userinfo endpoint ({userinfo_url}) returned {resp.status_code}: {resp.text[:500]}",
        )

    # Endpoint verified — resolve user_id from argument or userinfo 'sub' claim
    userinfo = resp.json()
    user_id = arguments.get("user_id") or userinfo.get("sub", "")

    # Pass user_id as tenant_id so connector connections are saved under this user
    arguments.setdefault("tenant_id", user_id)

    # Call the Fastn GraphQL API directly (bypasses SDK's _ensure_fresh_token)
    # using the Fastn OAuth token from the MCP auth context
    async with _sdk_client(arguments) as client:
        workspace_id = client._config.resolve_project_id()
        variables = _build_custom_auth_step(workspace_id, userinfo_url)
        result = await _gql_call_async(client, _UPDATE_RESOLVER_STEP_MUTATION, variables)
        return _success_result({
            **result,
            "verified_user": userinfo,
            "user_id": user_id,
            "resolved_userinfo_url": userinfo_url,
            "review_url": f"https://app.fastn.ai/projects/{workspace_id}/fastn/configure-auth",
            "next_step": (
                'Call deploy_flow with flow_id="fastnCustomAuth" and stage="LIVE" '
                "to activate in production. Then call configure_connect_kit with a "
                "styles object to customize the Connect Kit appearance."
            ),
        })


async def _handle_deploy_flow(arguments: dict) -> list[TextContent]:
    """Deploy a flow to DRAFT or LIVE stage."""
    from fastn._http import _gql_call_async

    flow_id = arguments.get("flow_id", "")
    if not flow_id:
        return _error_result("MISSING_PARAM", "flow_id is required")

    stage = arguments.get("stage", "LIVE").upper()
    if stage not in ("DRAFT", "LIVE"):
        return _error_result("INVALID_PARAM", "stage must be DRAFT or LIVE")

    comment = arguments.get("comment", "")

    async with _sdk_client(arguments) as client:
        workspace_id = client._config.resolve_project_id()
        variables = {
            "input": {
                "clientId": workspace_id,
                "env": stage,
                "id": flow_id,
                "comment": comment,
            }
        }
        result = await _gql_call_async(client, _DEPLOY_FLOW_MUTATION, variables)
        return _success_result(result.get("deployApiToStage", result))


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (returns a new dict)."""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


async def _handle_configure_connect_kit(arguments: dict) -> list[TextContent]:
    """Update Connect Kit styling for the current project."""
    from fastn._http import _gql_call_async

    styles = arguments.get("styles")
    if not styles or not isinstance(styles, dict):
        return _error_result("MISSING_PARAM", "styles is required and must be an object")

    async with _sdk_client(arguments) as client:
        workspace_id = client._config.resolve_project_id()

        # Fetch existing config via direct GraphQL (bypass _ensure_fresh_token)
        get_variables = {"input": {"id": workspace_id, "clientId": workspace_id}}
        get_data = await _gql_call_async(client, _GET_KIT_METADATA_QUERY, get_variables)
        existing = get_data.get("widgetMetadata") or {}

        # Parse styles — the API may return them as a JSON string.
        raw_styles = existing.get("styles", {})
        if isinstance(raw_styles, str):
            try:
                raw_styles = json.loads(raw_styles)
            except (json.JSONDecodeError, TypeError):
                raw_styles = {}
        existing_styles = raw_styles if isinstance(raw_styles, dict) else {}
        merged_styles = _deep_merge(existing_styles, styles)

        # Preserve all existing config fields, only replace styles.
        settings: dict = {}
        _PRESERVE_KEYS = (
            "authenticationApi",
            "isCustomAuthenticationEnabled",
            "filterWidgets",
            "showFilterBar",
            "showLabels",
            "isRBACEnabled",
            "disableFor",
            "isAIAgentWidgetEnabled",
            "labelsLayout",
            "advancedSettings",
            "widgetsMetrics",
        )
        for key in _PRESERVE_KEYS:
            if key in existing:
                settings[key] = existing[key]
        settings["styles"] = json.dumps(merged_styles)

        # Save via direct GraphQL (bypass _ensure_fresh_token)
        save_variables = {"input": {"projectId": workspace_id, **settings}}
        save_data = await _gql_call_async(client, _SAVE_KIT_METADATA_MUTATION, save_variables)
        result = save_data.get("saveWidgetMetadata") or {}
        return _success_result(result)


async def _handle_list_skills(arguments: dict) -> list[TextContent]:
    """List skills available in the current project."""
    async with _sdk_client(arguments) as client:
        skills = await client.skills.list()
        results = [
            {"id": s["id"], "name": s["name"], "description": s.get("description", "")}
            for s in skills
        ]
        return _success_result({"skills": results, "total": len(results)})


async def _handle_list_projects(arguments: dict) -> list[TextContent]:
    """List projects available to the authenticated user."""
    try:
        async with _sdk_client(arguments) as client:
            projects = await client.projects.list()
            return _success_result({"projects": projects, "total": len(projects)})
    except ConfigError:
        # No project set — create a minimal client with just the auth token.
        # list_projects is the tool users call to FIX the missing project,
        # so it must work without one.
        auth_token, _ = _resolve_auth_token(arguments)
        if not auth_token:
            return _error_result(
                "INVALID_TOKEN",
                "No authentication token available. Authenticate first.",
            )
        client = AsyncFastnClient(auth_token=auth_token, verbose=_verbose)
        try:
            projects = await client.projects.list()
            return _success_result({"projects": projects, "total": len(projects)})
        finally:
            await client.close()


# Tool name → handler mapping
_TOOL_HANDLERS = {
    "find_tools": _handle_find_tools,
    "list_connectors": _handle_list_connectors,
    "execute_tool": _handle_execute_tool,
    "list_flows": _handle_list_flows,
    "get_flow_schema": _handle_get_flow_schema,
    "run_flow": _handle_run_flow,
    "delete_flow": _handle_delete_flow,
    "generate_flow": _handle_generate_flow,
    "update_flow": _handle_update_flow,
    "configure_connect_kit_auth": _handle_configure_connect_kit_auth,
    "deploy_flow": _handle_deploy_flow,
    "configure_connect_kit": _handle_configure_connect_kit,
    "list_skills": _handle_list_skills,
    "list_projects": _handle_list_projects,
}


# ---------------------------------------------------------------------------
# Per-mode Server factory — used by HTTP transports
# ---------------------------------------------------------------------------

def _create_mcp_server(tools: list[Tool]) -> Server:
    """Create an MCP Server instance with the given tools.

    Each HTTP endpoint path gets its own Server with a pre-configured tool
    list.  This avoids relying on contextvars or header injection for mode
    filtering — the MCP SDK's stateless mode spawns tasks in a fresh
    context, so runtime filtering is unreliable.

    All created servers share the same call_tool dispatch logic via
    _TOOL_HANDLERS and the same request_ctx ContextVar (defined in the
    MCP SDK), so auth token resolution works regardless of which Server
    instance is processing the request.
    """
    srv = Server("fastn", instructions=_SERVER_INSTRUCTIONS)

    @srv.list_tools()
    async def _list_tools() -> list[Tool]:
        if _is_api_key_auth():
            return [t for t in tools if t.name in API_KEY_TOOLS]
        return tools

    @srv.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        return await handle_call_tool(name, arguments)

    return srv


# ---------------------------------------------------------------------------
# Server entry points — one per transport
# ---------------------------------------------------------------------------

async def run_stdio(mode: str = "agent", project_id: str | None = None, skill_id: str | None = None):
    """Run the Fastn MCP server via stdio transport (local, default)."""
    global _server_mode, _server_project_id, _server_skill_id
    _server_mode = mode
    _server_project_id = project_id
    _server_skill_id = skill_id
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def create_starlette_app(
    transport: str = "sse",
    auth_enabled: bool = True,
    server_url: str = "http://localhost:8000",
):
    """Create a Starlette ASGI app for remote transport.

    Clients choose mode via URL path:
      /shttp          → all tools
      /shttp/tools      → Fastn tools only
      /shttp/tools/{id} → Fastn tools with pre-set project
      (same pattern for /sse)
    """
    import contextlib
    from collections.abc import AsyncIterator

    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware
    from starlette.requests import Request
    from starlette.responses import RedirectResponse, JSONResponse
    from starlette.routing import Mount, Route

    global _oauth_provider

    # ── OAuth setup ──────────────────────────────────────────────────────
    auth_routes: list[Route] = []
    auth_middleware: list[Middleware] = []

    if auth_enabled:
        from pydantic import AnyHttpUrl
        from starlette.middleware.authentication import AuthenticationMiddleware

        from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
        from mcp.server.auth.middleware.bearer_auth import (
            BearerAuthBackend,
            RequireAuthMiddleware,
        )
        from mcp.server.auth.provider import ProviderTokenVerifier
        from mcp.server.auth.routes import (
            create_auth_routes,
            create_protected_resource_routes,
        )
        from mcp.server.auth.settings import (
            ClientRegistrationOptions,
            RevocationOptions,
        )

        provider = FastnOAuthProvider(server_url=server_url)
        _oauth_provider = provider

        token_verifier = ProviderTokenVerifier(provider)

        # Middleware: extract Bearer token + store in context
        auth_middleware = [
            Middleware(
                AuthenticationMiddleware,
                backend=BearerAuthBackend(token_verifier),
            ),
            Middleware(AuthContextMiddleware),
        ]

        # OAuth routes: /.well-known/oauth-authorization-server, /authorize,
        # /token, /register, /revoke
        issuer_url = AnyHttpUrl(server_url)
        auth_routes = create_auth_routes(
            provider=provider,
            issuer_url=issuer_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["read", "write"],
            ),
            revocation_options=RevocationOptions(enabled=True),
        )

        # RFC 9728 Protected Resource Metadata routes
        # Clients (Lovable, etc.) request /.well-known/oauth-protected-resource
        # and /.well-known/oauth-protected-resource/{path} to discover the
        # authorization server that protects this resource.
        resource_url = AnyHttpUrl(server_url)
        auth_routes.extend(
            create_protected_resource_routes(
                resource_url=resource_url,
                authorization_servers=[issuer_url],
                scopes_supported=["read", "write"],
                resource_name="Fastn MCP Server",
            )
        )

        # Keycloak callback route — NOT part of the MCP SDK, this is our
        # custom endpoint that Keycloak redirects to after the user logs in.
        async def handle_keycloak_callback(request: Request):
            """Handle Keycloak OAuth callback."""
            state = request.query_params.get("state")
            code = request.query_params.get("code")
            error = request.query_params.get("error")

            if error:
                return JSONResponse(
                    {"error": error, "error_description": request.query_params.get("error_description", "")},
                    status_code=400,
                )

            if not state or not code:
                return JSONResponse(
                    {"error": "invalid_request", "error_description": "Missing state or code parameter"},
                    status_code=400,
                )

            try:
                redirect_url = await provider.handle_keycloak_callback(state, code)
                return RedirectResponse(url=redirect_url, status_code=302)
            except ValueError as e:
                return JSONResponse(
                    {"error": "invalid_request", "error_description": str(e)},
                    status_code=400,
                )
            except Exception as e:
                logger.exception("Keycloak callback failed")
                return JSONResponse(
                    {"error": "server_error", "error_description": "Authentication failed"},
                    status_code=500,
                )

        auth_routes.append(
            Route("/callback", endpoint=handle_keycloak_callback, methods=["GET"]),
        )
    else:
        _oauth_provider = None

    # ── CORS middleware (always present) ─────────────────────────────────
    cors_middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["*", "Authorization"],
            expose_headers=["mcp-session-id"],
        ),
    ]

    # ── Shared helpers ─────────────────────────────────────────────────
    class _AsgiApp:
        """Wrap an ASGI function so Starlette's Route treats it as an app.

        Starlette Route wraps plain functions with request_response() which
        changes the calling convention to func(Request).  Wrapping in a class
        makes Route detect it as an ASGI app and call it as (scope, receive, send).
        """
        __slots__ = ("_fn",)

        def __init__(self, fn):
            self._fn = fn

        async def __call__(self, scope, receive, send):
            await self._fn(scope, receive, send)

    def _is_closed_resource_group(eg: BaseException) -> bool:
        """Check if an ExceptionGroup contains only ClosedResourceError (nested)."""
        if isinstance(eg, anyio.ClosedResourceError):
            return True
        if isinstance(eg, (ExceptionGroup, BaseExceptionGroup)):
            return all(_is_closed_resource_group(e) for e in eg.exceptions)
        return False

    # ── Per-mode MCP Server + SessionManager setup ──────────────────────
    # Each URL path gets its own Server instance with pre-configured tools.
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    all_tools = TOOLS
    fastn_tools = [t for t in TOOLS if t.name in FASTN_TOOL_NAMES]
    fastn_tools_no_proj = [t for t in TOOLS if t.name in (FASTN_TOOL_NAMES - {"list_projects"})]
    fastn_tools_no_proj_skill = [t for t in TOOLS if t.name in (FASTN_TOOL_NAMES - {"list_projects", "list_skills"})]

    server_all = _create_mcp_server(all_tools)
    server_fastn = _create_mcp_server(fastn_tools)
    server_fastn_proj = _create_mcp_server(fastn_tools_no_proj)
    server_fastn_proj_skill = _create_mcp_server(fastn_tools_no_proj_skill)

    mgr_all = StreamableHTTPSessionManager(app=server_all, stateless=True)
    mgr_fastn = StreamableHTTPSessionManager(app=server_fastn, stateless=True)
    mgr_fastn_proj = StreamableHTTPSessionManager(app=server_fastn_proj, stateless=True)
    mgr_fastn_proj_skill = StreamableHTTPSessionManager(app=server_fastn_proj_skill, stateless=True)

    # Route (exact match) not Mount (prefix match) — Mount appends
    # /{path:path} so it won't match the exact prefix path.

    async def _shttp_all(scope, receive, send):
        logger.info("→ shttp ALL handler (%d tools)", len(all_tools))
        await mgr_all.handle_request(scope, receive, send)

    async def _shttp_fastn(scope, receive, send):
        logger.info("→ shttp Fastn handler (%d tools)", len(fastn_tools))
        await mgr_fastn.handle_request(scope, receive, send)

    async def _shttp_fastn_proj(scope, receive, send):
        project_id = scope.get("path_params", {}).get("project_id", "")
        logger.info("→ shttp Fastn+project handler (project=%s, %d tools)", project_id, len(fastn_tools_no_proj))
        token = _request_project_id.set(project_id)
        try:
            await mgr_fastn_proj.handle_request(scope, receive, send)
        finally:
            _request_project_id.reset(token)

    async def _shttp_fastn_proj_skill(scope, receive, send):
        path_params = scope.get("path_params", {})
        project_id = path_params.get("project_id", "")
        skill_id = path_params.get("skill_id", "")
        logger.info("→ shttp Fastn+project+skill handler (project=%s, skill=%s, %d tools)", project_id, skill_id, len(fastn_tools_no_proj_skill))
        proj_token = _request_project_id.set(project_id)
        skill_token = _request_skill_id.set(skill_id)
        try:
            await mgr_fastn_proj_skill.handle_request(scope, receive, send)
        finally:
            _request_skill_id.reset(skill_token)
            _request_project_id.reset(proj_token)

    if auth_enabled:
        shttp_app_all = RequireAuthMiddleware(_shttp_all, required_scopes=[])
        shttp_app_fastn = RequireAuthMiddleware(_shttp_fastn, required_scopes=[])
        shttp_app_fastn_proj = RequireAuthMiddleware(_shttp_fastn_proj, required_scopes=[])
        shttp_app_fastn_proj_skill = RequireAuthMiddleware(_shttp_fastn_proj_skill, required_scopes=[])
    else:
        shttp_app_all = _AsgiApp(_shttp_all)
        shttp_app_fastn = _AsgiApp(_shttp_fastn)
        shttp_app_fastn_proj = _AsgiApp(_shttp_fastn_proj)
        shttp_app_fastn_proj_skill = _AsgiApp(_shttp_fastn_proj_skill)

    # ── Build transport-specific app ─────────────────────────────────────
    # Resolve which transports to enable
    enable_sse = transport in ("sse", "sse-only")
    enable_shttp = transport in ("sse", "shttp-only", "streamable-http")

    if not enable_sse and not enable_shttp:
        raise ValueError(f"Unknown transport: {transport!r}")

    # ── Root endpoint — dynamic server info ───────────────────────────
    async def handle_root(request: Request):
        """Dynamic server info showing all valid endpoint combinations."""
        base = server_url.rstrip("/")

        all_tool_names = [t.name for t in TOOLS]
        fastn_tool_names = sorted(FASTN_TOOL_NAMES)
        fastn_no_proj_names = sorted(FASTN_TOOL_NAMES - {"list_projects"})
        fastn_no_proj_skill_names = sorted(FASTN_TOOL_NAMES - {"list_projects", "list_skills"})

        endpoints = []
        if enable_shttp:
            endpoints.append({"method": "POST", "path": "/shttp", "url": f"{base}/shttp", "mode": "agent", "tools": len(all_tool_names), "description": "Streamable HTTP — all tools"})
            endpoints.append({"method": "POST", "path": "/shttp/tools", "url": f"{base}/shttp/tools", "mode": "tools", "tools": len(fastn_tool_names), "description": "Streamable HTTP — Fastn tools only"})
            endpoints.append({"method": "POST", "path": "/shttp/tools/{project_id}", "url": f"{base}/shttp/tools/{{project_id}}", "mode": "tools", "tools": len(fastn_no_proj_names), "description": "Streamable HTTP — Fastn with pre-set project"})
            endpoints.append({"method": "POST", "path": "/shttp/tools/{project_id}/{skill_id}", "url": f"{base}/shttp/tools/{{project_id}}/{{skill_id}}", "mode": "tools", "tools": len(fastn_no_proj_skill_names), "description": "Streamable HTTP — Fastn with pre-set project and skill"})
        if enable_sse:
            endpoints.append({"method": "GET", "path": "/sse", "url": f"{base}/sse", "mode": "agent", "tools": len(all_tool_names), "description": "SSE — all tools"})
            endpoints.append({"method": "GET", "path": "/sse/tools", "url": f"{base}/sse/tools", "mode": "tools", "tools": len(fastn_tool_names), "description": "SSE — Fastn tools only"})
            endpoints.append({"method": "GET", "path": "/sse/tools/{project_id}", "url": f"{base}/sse/tools/{{project_id}}", "mode": "tools", "tools": len(fastn_no_proj_names), "description": "SSE — Fastn with pre-set project"})
            endpoints.append({"method": "GET", "path": "/sse/tools/{project_id}/{skill_id}", "url": f"{base}/sse/tools/{{project_id}}/{{skill_id}}", "mode": "tools", "tools": len(fastn_no_proj_skill_names), "description": "SSE — Fastn with pre-set project and skill"})
            endpoints.append({"method": "POST", "path": "/messages/", "url": f"{base}/messages/", "description": "SSE messages"})

        if auth_enabled:
            endpoints.append({"method": "GET", "path": "/.well-known/oauth-authorization-server", "url": f"{base}/.well-known/oauth-authorization-server", "description": "OAuth server metadata"})
            endpoints.append({"method": "GET", "path": "/.well-known/oauth-protected-resource", "url": f"{base}/.well-known/oauth-protected-resource", "description": "Protected resource metadata"})
            endpoints.append({"method": "POST", "path": "/authorize", "url": f"{base}/authorize", "description": "OAuth authorization"})
            endpoints.append({"method": "POST", "path": "/token", "url": f"{base}/token", "description": "OAuth token"})
            endpoints.append({"method": "POST", "path": "/register", "url": f"{base}/register", "description": "OAuth client registration"})
            endpoints.append({"method": "GET", "path": "/callback", "url": f"{base}/callback", "description": "Keycloak callback"})

        modes = {
            "agent": {"description": "All tools (Fastn + flows + config)", "tools": all_tool_names},
            "tools": {"description": "Discovery and execution tools only", "tools": fastn_tool_names},
        }

        return JSONResponse({
            "name": "Fastn MCP Server",
            "auth": "enabled" if auth_enabled else "disabled",
            "endpoints": endpoints,
            "modes": modes,
        })

    # ── Flow builder popup ─────────────────────────────────────────────
    # Serve the single-file popup HTML.
    # 1) Package static dir (Docker / pip install)
    # 2) Sibling repo dev fallback (fastn-agent-kit/dist/)
    _pkg_static = os.path.join(os.path.dirname(__file__), "static", "flow-builder.html")
    _dev_fallback = os.path.join(os.path.dirname(__file__), "..", "..", "fastn-agent-kit", "dist", "flow-builder.html")
    _flow_builder_html_path = _pkg_static if os.path.isfile(_pkg_static) else _dev_fallback

    async def handle_flow_builder(request: Request):
        from starlette.responses import HTMLResponse
        resolved = os.path.abspath(_flow_builder_html_path)
        if os.path.isfile(resolved):
            with open(resolved) as f:
                return HTMLResponse(f.read())
        return JSONResponse(
            {"error": "flow-builder.html not found. Run npm run build in fastn-agent-kit."},
            status_code=404,
        )

    # ── Flow builder short-link redirect ─────────────────────────────
    # /fb/{code} is a one-time-use short URL that retrieves stored
    # session params and redirects to /flow-builder.html with query args.
    async def handle_flow_redirect(request: Request):
        from urllib.parse import urlencode
        code = request.path_params["code"]
        params = await _pop_flow_session(code)
        if params is None:
            return JSONResponse(
                {"error": "invalid_or_expired",
                 "message": "This flow builder link has expired or already been used."},
                status_code=410,
            )
        query = urlencode({
            "token": params.get("token", ""),
            "project": params.get("project", ""),
            "session": params.get("session", ""),
            "prompt": params.get("prompt", ""),
        })
        return RedirectResponse(url=f"/flow-builder.html?{query}", status_code=302)

    routes = list(auth_routes)
    routes.append(Route("/fb/{code}", endpoint=handle_flow_redirect, methods=["GET"]))
    routes.append(Route("/flow-builder.html", endpoint=handle_flow_builder, methods=["GET"]))
    routes.append(Route("/", endpoint=handle_root, methods=["GET"]))
    transport_names = []

    # RFC 9728 path-based protected resource metadata for each transport
    # e.g. /.well-known/oauth-protected-resource/sse for the SSE endpoint
    if auth_enabled:
        if enable_sse:
            sse_resource_url = AnyHttpUrl(str(server_url).rstrip("/") + "/sse")
            routes.extend(
                create_protected_resource_routes(
                    resource_url=sse_resource_url,
                    authorization_servers=[issuer_url],
                    scopes_supported=["read", "write"],
                    resource_name="Fastn MCP Server",
                )
            )
        if enable_shttp:
            shttp_resource_url = AnyHttpUrl(str(server_url).rstrip("/") + "/shttp")
            routes.extend(
                create_protected_resource_routes(
                    resource_url=shttp_resource_url,
                    authorization_servers=[issuer_url],
                    scopes_supported=["read", "write"],
                    resource_name="Fastn MCP Server",
                )
            )

    # SSE transport
    if enable_sse:
        from mcp.server.sse import SseServerTransport

        sse_transport = SseServerTransport("/messages/")

        def _make_sse_handler(sse_server: Server, parse_project_from_path: bool = False):
            """Create an SSE handler bound to a specific MCP Server."""

            async def handle_sse(scope, receive, send):
                """Handle incoming SSE connections."""
                # Optionally parse project_id and skill_id from URL path
                project_token = None
                skill_token = None
                if parse_project_from_path:
                    path = scope.get("path", "") or "/"
                    uuids = _re.findall(r"[a-f0-9-]{36}", path)
                    if len(uuids) >= 1:
                        project_token = _request_project_id.set(uuids[0])
                    if len(uuids) >= 2:
                        skill_token = _request_skill_id.set(uuids[1])

                session_id: str | None = None
                try:
                    async with sse_transport.connect_sse(scope, receive, send) as (
                        read_stream,
                        write_stream,
                    ):
                        for sid in sse_transport._read_stream_writers:
                            session_id = sid.hex
                        if session_id:
                            _active_sse_sessions.add(session_id)
                            logger.debug("SSE session started: %s", session_id)

                        await sse_server.run(
                            read_stream,
                            write_stream,
                            sse_server.create_initialization_options(),
                        )
                except anyio.ClosedResourceError:
                    logger.debug("SSE client disconnected")
                except (ExceptionGroup, BaseExceptionGroup) as eg:
                    if _is_closed_resource_group(eg):
                        logger.debug("SSE client disconnected")
                    else:
                        logger.exception("Unexpected errors in SSE handler")
                finally:
                    if skill_token is not None:
                        _request_skill_id.reset(skill_token)
                    if project_token is not None:
                        _request_project_id.reset(project_token)
                    if session_id:
                        _active_sse_sessions.discard(session_id)
                        from uuid import UUID
                        try:
                            del sse_transport._read_stream_writers[UUID(hex=session_id)]
                        except KeyError:
                            pass
                        logger.debug("SSE session cleaned up: %s", session_id)

            return handle_sse

        sse_all = _make_sse_handler(server_all)
        sse_fastn = _make_sse_handler(server_fastn)
        sse_fastn_proj = _make_sse_handler(server_fastn_proj, parse_project_from_path=True)
        sse_fastn_proj_skill = _make_sse_handler(server_fastn_proj_skill, parse_project_from_path=True)

        async def handle_post_message(scope, receive, send):
            """Wrap SSE POST handler to detect dead sessions early."""
            from starlette.requests import Request
            from starlette.responses import Response as StarletteResponse

            request = Request(scope, receive)
            session_id_param = request.query_params.get("session_id")

            if session_id_param and session_id_param not in _active_sse_sessions:
                logger.warning(
                    "POST to dead SSE session %s — returning 404",
                    session_id_param,
                )
                resp = StarletteResponse(
                    "Session not found or disconnected",
                    status_code=404,
                )
                await resp(scope, receive, send)
                return

            try:
                await sse_transport.handle_post_message(scope, receive, send)
            except anyio.ClosedResourceError:
                logger.debug("SSE POST to closed session (client disconnected)")
                if session_id_param:
                    _active_sse_sessions.discard(session_id_param)
                    from uuid import UUID
                    try:
                        del sse_transport._read_stream_writers[UUID(hex=session_id_param)]
                    except KeyError:
                        pass

        if auth_enabled:
            sse_handler_all = RequireAuthMiddleware(sse_all, required_scopes=[])
            sse_handler_fastn = RequireAuthMiddleware(sse_fastn, required_scopes=[])
            sse_handler_fastn_proj = RequireAuthMiddleware(sse_fastn_proj, required_scopes=[])
            sse_handler_fastn_proj_skill = RequireAuthMiddleware(sse_fastn_proj_skill, required_scopes=[])
            messages_handler = RequireAuthMiddleware(handle_post_message, required_scopes=[])
        else:
            sse_handler_all = _AsgiApp(sse_all)
            sse_handler_fastn = _AsgiApp(sse_fastn)
            sse_handler_fastn_proj = _AsgiApp(sse_fastn_proj)
            sse_handler_fastn_proj_skill = _AsgiApp(sse_fastn_proj_skill)
            messages_handler = _AsgiApp(handle_post_message)

        routes.append(Route("/sse", endpoint=sse_handler_all))
        routes.append(Route("/sse/tools", endpoint=sse_handler_fastn))
        routes.append(Route("/sse/tools/{project_id}/{skill_id}", endpoint=sse_handler_fastn_proj_skill))
        routes.append(Route("/sse/tools/{project_id}", endpoint=sse_handler_fastn_proj))
        routes.append(Mount("/messages/", app=messages_handler))
        transport_names.append("SSE")

    # Streamable HTTP transport — most-specific path first
    # Uses Route (exact match) not Mount (prefix match) because Mount
    # appends /{path:path} and won't match the exact prefix path.
    if enable_shttp:
        routes.append(Route("/shttp/tools/{project_id}/{skill_id}", endpoint=shttp_app_fastn_proj_skill))
        routes.append(Route("/shttp/tools/{project_id:path}", endpoint=shttp_app_fastn_proj))
        routes.append(Route("/shttp/tools", endpoint=shttp_app_fastn))
        routes.append(Route("/shttp", endpoint=shttp_app_all))
        transport_names.append("Streamable HTTP")

    transport_label = " + ".join(transport_names)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        logger.info("Fastn MCP server started (%s, auth=%s)", transport_label, auth_enabled)
        _init_firestore()
        async with mgr_all.run():
            async with mgr_fastn.run():
                async with mgr_fastn_proj.run():
                    async with mgr_fastn_proj_skill.run():
                        async with anyio.create_task_group() as tg:
                            if _oauth_provider is not None:
                                async def _cleanup_loop() -> None:
                                    while True:
                                        await anyio.sleep(300)
                                        _oauth_provider.cleanup_expired()
                                tg.start_soon(_cleanup_loop)

                            async def _flow_session_cleanup_loop() -> None:
                                while True:
                                    await anyio.sleep(300)
                                    _cleanup_flow_sessions()
                            tg.start_soon(_flow_session_cleanup_loop)

                            yield
                            tg.cancel_scope.cancel()
        logger.info("Fastn MCP server stopped")

    app = Starlette(
        debug=False,
        routes=routes,
        lifespan=lifespan,
        middleware=cors_middleware + auth_middleware,
    )

    return app


def _print_startup_info(
    transport: str,
    host: str | None = None,
    port: int | None = None,
    auth_enabled: bool = True,
    server_url: str | None = None,
):
    """Print server configuration summary on startup."""
    enable_sse = transport in ("sse", "sse-only")
    enable_shttp = transport in ("sse", "shttp-only", "streamable-http")

    lines = [
        "",
        "=" * 60,
        "  Fastn MCP Server",
        "=" * 60,
    ]

    # Transport
    transport_map = {
        "stdio": "stdio (pipe)",
        "sse": "SSE + Streamable HTTP",
        "sse-only": "SSE only",
        "shttp-only": "Streamable HTTP only",
        "streamable-http": "Streamable HTTP only",
    }
    lines.append(f"  Transport : {transport_map.get(transport, transport)}")

    # Network (remote transports only)
    if transport != "stdio":
        lines.append(f"  Auth      : {'enabled (OAuth)' if auth_enabled else 'disabled'}")
        if server_url:
            lines.append(f"  Server URL: {server_url}")

        base = server_url or f"http://{host}:{port}"
        base = base.rstrip("/")

        lines.append("")
        lines.append("  Endpoints (mode filtering is runtime — all combinations available):")
        if enable_shttp:
            lines.append(f"    POST {base}/shttp                   all tools ({len(TOOLS)})")
            lines.append(f"    POST {base}/shttp/tools               Fastn tools ({len(FASTN_TOOL_NAMES)})")
            lines.append(f"    POST {base}/shttp/tools/{{project_id}}  Fastn + project ({len(FASTN_TOOL_NAMES) - 1})")
            lines.append(f"    POST {base}/shttp/tools/{{project_id}}/{{skill_id}}  Fastn + project + skill ({len(FASTN_TOOL_NAMES) - 2})")
        if enable_sse:
            lines.append(f"    GET  {base}/sse                     all tools ({len(TOOLS)})")
            lines.append(f"    GET  {base}/sse/tools                 Fastn tools ({len(FASTN_TOOL_NAMES)})")
            lines.append(f"    GET  {base}/sse/tools/{{project_id}}    Fastn + project ({len(FASTN_TOOL_NAMES) - 1})")
            lines.append(f"    GET  {base}/sse/tools/{{project_id}}/{{skill_id}}    Fastn + project + skill ({len(FASTN_TOOL_NAMES) - 2})")
            lines.append(f"    POST {base}/messages/               SSE messages")
    else:
        lines.append(f"  Mode      : {_server_mode}")
        if _server_project_id:
            lines.append(f"  Project   : {_server_project_id}")
        if _server_skill_id:
            lines.append(f"  Skill     : {_server_skill_id}")

    # Tools
    lines.append("")
    lines.append(f"  Tools ({len(TOOLS)}):")
    _TOOL_TAGS = {
        "find_tools": "Tools", "execute_tool": "Tools", "list_connectors": "Tools",
        "configure_connect_kit": "Kit", "configure_connect_kit_auth": "Kit",
        "list_projects": "Admin", "list_skills": "Admin", "deploy_flow": "Admin",
    }
    for tool in TOOLS:
        tag = f"[{_TOOL_TAGS.get(tool.name, 'Agents')}]"
        lines.append(f"    {tag:10s} {tool.name}")

    lines.append("")
    lines.append("=" * 60)
    lines.append("")

    logger.info("\n".join(lines))


async def main(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = 8000,
    auth_enabled: bool = True,
    server_url: Optional[str] = None,
    mode: str = "agent",
    project_id: Optional[str] = None,
    skill_id: Optional[str] = None,
):
    """Run the Fastn MCP server.

    HTTP transports use URL path for mode filtering (/shttp/tools, /sse/tools).
    The --mode, --project, and --skill flags apply to stdio transport only.
    """
    if transport == "stdio":
        _print_startup_info(transport=transport)
        await run_stdio(mode=mode, project_id=project_id, skill_id=skill_id)
    elif transport in ("sse", "sse-only", "shttp-only", "streamable-http"):
        import uvicorn

        if server_url is None:
            scheme = "http"
            display_host = "localhost" if host == "0.0.0.0" else host
            server_url = f"{scheme}://{display_host}:{port}"

        global _server_url
        _server_url = server_url

        _print_startup_info(
            transport=transport,
            host=host,
            port=port,
            auth_enabled=auth_enabled,
            server_url=server_url,
        )

        app = create_starlette_app(
            transport=transport,
            auth_enabled=auth_enabled,
            server_url=server_url,
        )
        config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            log_level="info",
        )
        uv_server = uvicorn.Server(config)
        await uv_server.serve()
    else:
        raise ValueError(f"Unknown transport: {transport!r}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
