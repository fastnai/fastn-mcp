"""Fastn MCP Server — stateless translation layer wrapping the Fastn SDK.

Exposes MCP tools for AI agents and apps (Claude Desktop, Cursor, Lovable, and any MCP client):

  UCL:    find_tools, execute_tool, discover_tools, list_skills, list_projects
  Agent:  list_flows, run_flow, delete_flow, create_flow*, update_flow*,
          configure_custom_auth

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
import re as _re
from contextvars import ContextVar
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

# ---------------------------------------------------------------------------
# Default server instance — used for stdio transport and as fallback
# ---------------------------------------------------------------------------

_SERVER_INSTRUCTIONS = (
    "Fastn — 250+ pre-built connectors (Stripe, Slack, Jira, Salesforce, HubSpot, "
    "Postgres, and more) with managed OAuth, sub-second execution, and multi-step "
    "flow orchestration. "
    "Workflow: find_tools → execute_tool. "
    "If find_tools returns nothing, call discover_tools to check if the connector "
    "exists but is not yet connected — show the user the connect_url. "
    "For multi-step automations use create_flow, then trigger with run_flow. "
    "When generating backend code (Edge Functions, API routes), call Fastn REST API "
    "at https://live.fastn.ai/api/ucl/executeTool with headers x-fastn-api-key and "
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
# "agent" → all tools (UCL + flows + config)
# "ucl"   → UCL tools only
_server_mode: str = "agent"
_server_project_id: str | None = None
_server_skill_id: str | None = None

UCL_TOOL_NAMES = {"find_tools", "execute_tool", "discover_tools", "list_skills", "list_projects"}

# Request-scoped project_id — set per-connection (SSE) or per-request (shttp).
_request_project_id: ContextVar[str | None] = ContextVar("_request_project_id", default=None)

# Request-scoped skill_id — set when URL includes /ucl/{project_id}/{skill_id}.
_request_skill_id: ContextVar[str | None] = ContextVar("_request_skill_id", default=None)

_UCL_PATH_RE = _re.compile(r"^/ucl(?:/([a-f0-9-]{36}))?(?:/([a-f0-9-]{36}))?$")


def _parse_mode_from_path(path: str) -> tuple[str, str | None, str | None]:
    """Parse mode, project_id, and skill_id from URL sub-path.

    "/" or ""                    → ("agent", None, None)
    "/ucl"                      → ("ucl", None, None)
    "/ucl/<project_id>"         → ("ucl", "<project_id>", None)
    "/ucl/<project_id>/<skill>" → ("ucl", "<project_id>", "<skill_id>")
    """
    m = _UCL_PATH_RE.match(path)
    if m:
        return "ucl", m.group(1), m.group(2)
    return "agent", None, None


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

    Reads x-project-id and x-tenant-id from request headers.
    API keys and auth tokens are passed via the Authorization header
    and resolved by _resolve_auth_token.
    """
    try:
        ctx = server.request_context
        if ctx.request is not None:
            headers = ctx.request.headers
            return {
                "project_id": headers.get("x-project-id", ""),
                "tenant_id": headers.get("x-tenant-id", ""),
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
    tenant_id = arguments.get("tenant_id") or req_headers.get("tenant_id") or ""

    return AsyncFastnClient(
        api_key=arguments.get("api_key"),
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

TOOLS = [
    # =====================================================================
    # UCL TOOLS
    # Workflow: find_tools → execute_tool
    # Fallback: discover_tools → prompt user to connect at app.ucl.dev
    # =====================================================================
    Tool(
        name="find_tools",
        description=(
            "ALWAYS call this first when the user wants to use any "
            "connector tool (send message, create ticket, send email, "
            "etc). Searches for tools that are active and connected in "
            "this project. Returns matching tools with actionId and "
            "inputSchema. Next step: pass the actionId to execute_tool. "
            "IMPORTANT: If no results are returned, you MUST immediately "
            "call discover_tools to check if the connector exists but "
            "is not yet connected — never tell the user something is "
            "unavailable without checking discover_tools first."
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
        name="discover_tools",
        description=(
            "Fallback: call this automatically when find_tools returns no "
            "results. Browses all available connectors (200+) in the "
            "project, including ones not yet connected. Returns connector "
            "names and a connect_url. If the user's requested connector "
            "exists but is not connected, show them the connect_url so "
            "they can enable it, then retry find_tools. Use the query "
            "parameter to filter by connector name (e.g. 'teams', 'jira')."
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
            "List available projects for the authenticated user. Use this "
            "when you get PROJECT_NOT_SET errors or the user has multiple "
            "projects. Returns project IDs and names — pass the selected "
            "ID as project_id in subsequent tool calls."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    # =====================================================================
    # FLOW MANAGEMENT TOOLS
    # Manage saved automations (flows). Use list_flows to see existing
    # flows, run_flow to execute them, delete_flow to remove them.
    # create_flow and update_flow are under development.
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
        name="run_flow",
        description=(
            "Execute a saved automation by its flow_id. Get the flow_id "
            "from list_flows first. Returns the execution result directly."
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
        name="create_flow",
        description=(
            "(Under development) Create an automation from a natural "
            "language prompt. Not yet available. To run connector tools "
            "now, use: find_tools → execute_tool."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Plain English description of the automation to create",
                },
                "answers": {
                    "type": "object",
                    "description": "Key-value pairs answering questions from a previous call",
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
    # CONFIGURATION TOOLS
    # Setup and configuration for the project.
    # =====================================================================
    Tool(
        name="configure_custom_auth",
        description=(
            "Register a custom auth provider so Fastn can validate end-user "
            "JWTs. Call this after configuring Auth0, Firebase Auth, Supabase "
            "Auth, or similar. Once registered, pass end-user tokens in API "
            "calls for per-user authorization."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issuer_url": {
                    "type": "string",
                    "description": 'The "iss" claim value in your JWTs',
                },
                "jwks_uri": {
                    "type": "string",
                    "description": "The JWKS endpoint URL",
                },
                "user_id_claim": {
                    "type": "string",
                    "description": 'The JWT claim to use as the user ID. Defaults to "sub".',
                    "default": "sub",
                },
            },
            "required": ["issuer_url", "jwks_uri"],
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
    if _server_mode == "ucl":
        names = UCL_TOOL_NAMES
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
        return _error_result("WORKSPACE_NOT_SET", str(exc))
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

async def _handle_create_flow(arguments: dict) -> list[TextContent]:
    """Create a flow — under development."""
    return _error_result(
        "UNDER_DEVELOPMENT",
        "Flow creation is under development. Use find_tools → execute_tool.",
    )


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


async def _handle_run_flow(arguments: dict) -> list[TextContent]:
    """Execute a flow by its flow_id."""
    flow_id = arguments.get("flow_id", "")
    if not flow_id:
        return _error_result("MISSING_PARAM", "flow_id is required")

    async with _sdk_client(arguments) as client:
        result = await client.execute(tool=flow_id, params=arguments.get("parameters", {}))
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


async def _handle_discover_tools(arguments: dict) -> list[TextContent]:
    """Browse all available connectors in the project."""
    async with _sdk_client(arguments) as client:
        workspace_id = client._config.resolve_project_id()
        agent_id = arguments.get("project_id") or workspace_id
        connectors = client.connectors.list()
        query = arguments.get("query", "")
        if query:
            q = query.lower()
            connectors = [
                c for c in connectors
                if q in c["name"].lower() or q in c.get("display_name", "").lower()
            ]
        connect_url = f"https://app.ucl.dev/projects/{workspace_id}/ucl/{agent_id}"
        results = [{"name": c["name"], "connect_url": connect_url} for c in connectors]
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


async def _handle_configure_custom_auth(arguments: dict) -> list[TextContent]:
    """Register a custom auth provider with Fastn."""
    async with _sdk_client(arguments) as client:
        result = await client.configure_custom_auth(
            issuer_url=arguments["issuer_url"],
            jwks_uri=arguments["jwks_uri"],
            user_id_claim=arguments.get("user_id_claim", "sub"),
        )
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
    async with _sdk_client(arguments) as client:
        projects = await client.projects.list()
        return _success_result({"projects": projects, "total": len(projects)})


# Tool name → handler mapping
_TOOL_HANDLERS = {
    "find_tools": _handle_find_tools,
    "discover_tools": _handle_discover_tools,
    "execute_tool": _handle_execute_tool,
    "list_flows": _handle_list_flows,
    "run_flow": _handle_run_flow,
    "delete_flow": _handle_delete_flow,
    "create_flow": _handle_create_flow,
    "update_flow": _handle_update_flow,
    "configure_custom_auth": _handle_configure_custom_auth,
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
      /shttp/ucl      → UCL tools only
      /shttp/ucl/{id} → UCL tools with pre-set project
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
    ucl_tools = [t for t in TOOLS if t.name in UCL_TOOL_NAMES]
    ucl_tools_no_proj = [t for t in TOOLS if t.name in (UCL_TOOL_NAMES - {"list_projects"})]
    ucl_tools_no_proj_skill = [t for t in TOOLS if t.name in (UCL_TOOL_NAMES - {"list_projects", "list_skills"})]

    server_all = _create_mcp_server(all_tools)
    server_ucl = _create_mcp_server(ucl_tools)
    server_ucl_proj = _create_mcp_server(ucl_tools_no_proj)
    server_ucl_proj_skill = _create_mcp_server(ucl_tools_no_proj_skill)

    mgr_all = StreamableHTTPSessionManager(app=server_all, stateless=True)
    mgr_ucl = StreamableHTTPSessionManager(app=server_ucl, stateless=True)
    mgr_ucl_proj = StreamableHTTPSessionManager(app=server_ucl_proj, stateless=True)
    mgr_ucl_proj_skill = StreamableHTTPSessionManager(app=server_ucl_proj_skill, stateless=True)

    # Route (exact match) not Mount (prefix match) — Mount appends
    # /{path:path} so it won't match the exact prefix path.

    async def _shttp_all(scope, receive, send):
        logger.info("→ shttp ALL handler (%d tools)", len(all_tools))
        await mgr_all.handle_request(scope, receive, send)

    async def _shttp_ucl(scope, receive, send):
        logger.info("→ shttp UCL handler (%d tools)", len(ucl_tools))
        await mgr_ucl.handle_request(scope, receive, send)

    async def _shttp_ucl_proj(scope, receive, send):
        project_id = scope.get("path_params", {}).get("project_id", "")
        logger.info("→ shttp UCL+project handler (project=%s, %d tools)", project_id, len(ucl_tools_no_proj))
        token = _request_project_id.set(project_id)
        try:
            await mgr_ucl_proj.handle_request(scope, receive, send)
        finally:
            _request_project_id.reset(token)

    async def _shttp_ucl_proj_skill(scope, receive, send):
        path_params = scope.get("path_params", {})
        project_id = path_params.get("project_id", "")
        skill_id = path_params.get("skill_id", "")
        logger.info("→ shttp UCL+project+skill handler (project=%s, skill=%s, %d tools)", project_id, skill_id, len(ucl_tools_no_proj_skill))
        proj_token = _request_project_id.set(project_id)
        skill_token = _request_skill_id.set(skill_id)
        try:
            await mgr_ucl_proj_skill.handle_request(scope, receive, send)
        finally:
            _request_skill_id.reset(skill_token)
            _request_project_id.reset(proj_token)

    if auth_enabled:
        shttp_app_all = RequireAuthMiddleware(_shttp_all, required_scopes=[])
        shttp_app_ucl = RequireAuthMiddleware(_shttp_ucl, required_scopes=[])
        shttp_app_ucl_proj = RequireAuthMiddleware(_shttp_ucl_proj, required_scopes=[])
        shttp_app_ucl_proj_skill = RequireAuthMiddleware(_shttp_ucl_proj_skill, required_scopes=[])
    else:
        shttp_app_all = _AsgiApp(_shttp_all)
        shttp_app_ucl = _AsgiApp(_shttp_ucl)
        shttp_app_ucl_proj = _AsgiApp(_shttp_ucl_proj)
        shttp_app_ucl_proj_skill = _AsgiApp(_shttp_ucl_proj_skill)

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
        ucl_tool_names = sorted(UCL_TOOL_NAMES)
        ucl_no_proj_names = sorted(UCL_TOOL_NAMES - {"list_projects"})
        ucl_no_proj_skill_names = sorted(UCL_TOOL_NAMES - {"list_projects", "list_skills"})

        endpoints = []
        if enable_shttp:
            endpoints.append({"method": "POST", "path": "/shttp", "url": f"{base}/shttp", "mode": "agent", "tools": len(all_tool_names), "description": "Streamable HTTP — all tools"})
            endpoints.append({"method": "POST", "path": "/shttp/ucl", "url": f"{base}/shttp/ucl", "mode": "ucl", "tools": len(ucl_tool_names), "description": "Streamable HTTP — UCL tools only"})
            endpoints.append({"method": "POST", "path": "/shttp/ucl/{project_id}", "url": f"{base}/shttp/ucl/{{project_id}}", "mode": "ucl", "tools": len(ucl_no_proj_names), "description": "Streamable HTTP — UCL with pre-set project"})
            endpoints.append({"method": "POST", "path": "/shttp/ucl/{project_id}/{skill_id}", "url": f"{base}/shttp/ucl/{{project_id}}/{{skill_id}}", "mode": "ucl", "tools": len(ucl_no_proj_skill_names), "description": "Streamable HTTP — UCL with pre-set project and skill"})
        if enable_sse:
            endpoints.append({"method": "GET", "path": "/sse", "url": f"{base}/sse", "mode": "agent", "tools": len(all_tool_names), "description": "SSE — all tools"})
            endpoints.append({"method": "GET", "path": "/sse/ucl", "url": f"{base}/sse/ucl", "mode": "ucl", "tools": len(ucl_tool_names), "description": "SSE — UCL tools only"})
            endpoints.append({"method": "GET", "path": "/sse/ucl/{project_id}", "url": f"{base}/sse/ucl/{{project_id}}", "mode": "ucl", "tools": len(ucl_no_proj_names), "description": "SSE — UCL with pre-set project"})
            endpoints.append({"method": "GET", "path": "/sse/ucl/{project_id}/{skill_id}", "url": f"{base}/sse/ucl/{{project_id}}/{{skill_id}}", "mode": "ucl", "tools": len(ucl_no_proj_skill_names), "description": "SSE — UCL with pre-set project and skill"})
            endpoints.append({"method": "POST", "path": "/messages/", "url": f"{base}/messages/", "description": "SSE messages"})

        if auth_enabled:
            endpoints.append({"method": "GET", "path": "/.well-known/oauth-authorization-server", "url": f"{base}/.well-known/oauth-authorization-server", "description": "OAuth server metadata"})
            endpoints.append({"method": "GET", "path": "/.well-known/oauth-protected-resource", "url": f"{base}/.well-known/oauth-protected-resource", "description": "Protected resource metadata"})
            endpoints.append({"method": "POST", "path": "/authorize", "url": f"{base}/authorize", "description": "OAuth authorization"})
            endpoints.append({"method": "POST", "path": "/token", "url": f"{base}/token", "description": "OAuth token"})
            endpoints.append({"method": "POST", "path": "/register", "url": f"{base}/register", "description": "OAuth client registration"})
            endpoints.append({"method": "GET", "path": "/callback", "url": f"{base}/callback", "description": "Keycloak callback"})

        modes = {
            "agent": {"description": "All tools (UCL + flows + config)", "tools": all_tool_names},
            "ucl": {"description": "Discovery and execution tools only", "tools": ucl_tool_names},
        }

        return JSONResponse({
            "name": "Fastn MCP Server",
            "auth": "enabled" if auth_enabled else "disabled",
            "endpoints": endpoints,
            "modes": modes,
        })

    routes = list(auth_routes)
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
        sse_ucl = _make_sse_handler(server_ucl)
        sse_ucl_proj = _make_sse_handler(server_ucl_proj, parse_project_from_path=True)
        sse_ucl_proj_skill = _make_sse_handler(server_ucl_proj_skill, parse_project_from_path=True)

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
            sse_handler_ucl = RequireAuthMiddleware(sse_ucl, required_scopes=[])
            sse_handler_ucl_proj = RequireAuthMiddleware(sse_ucl_proj, required_scopes=[])
            sse_handler_ucl_proj_skill = RequireAuthMiddleware(sse_ucl_proj_skill, required_scopes=[])
            messages_handler = RequireAuthMiddleware(handle_post_message, required_scopes=[])
        else:
            sse_handler_all = _AsgiApp(sse_all)
            sse_handler_ucl = _AsgiApp(sse_ucl)
            sse_handler_ucl_proj = _AsgiApp(sse_ucl_proj)
            sse_handler_ucl_proj_skill = _AsgiApp(sse_ucl_proj_skill)
            messages_handler = _AsgiApp(handle_post_message)

        routes.append(Route("/sse", endpoint=sse_handler_all))
        routes.append(Route("/sse/ucl", endpoint=sse_handler_ucl))
        routes.append(Route("/sse/ucl/{project_id}/{skill_id}", endpoint=sse_handler_ucl_proj_skill))
        routes.append(Route("/sse/ucl/{project_id}", endpoint=sse_handler_ucl_proj))
        routes.append(Mount("/messages/", app=messages_handler))
        transport_names.append("SSE")

    # Streamable HTTP transport — most-specific path first
    # Uses Route (exact match) not Mount (prefix match) because Mount
    # appends /{path:path} and won't match the exact prefix path.
    if enable_shttp:
        routes.append(Route("/shttp/ucl/{project_id}/{skill_id}", endpoint=shttp_app_ucl_proj_skill))
        routes.append(Route("/shttp/ucl/{project_id:path}", endpoint=shttp_app_ucl_proj))
        routes.append(Route("/shttp/ucl", endpoint=shttp_app_ucl))
        routes.append(Route("/shttp", endpoint=shttp_app_all))
        transport_names.append("Streamable HTTP")

    transport_label = " + ".join(transport_names)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        logger.info("Fastn MCP server started (%s, auth=%s)", transport_label, auth_enabled)
        async with mgr_all.run():
            async with mgr_ucl.run():
                async with mgr_ucl_proj.run():
                    async with mgr_ucl_proj_skill.run():
                        async with anyio.create_task_group() as tg:
                            if _oauth_provider is not None:
                                async def _cleanup_loop() -> None:
                                    while True:
                                        await anyio.sleep(300)
                                        _oauth_provider.cleanup_expired()
                                tg.start_soon(_cleanup_loop)
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
            lines.append(f"    POST {base}/shttp/ucl               UCL tools ({len(UCL_TOOL_NAMES)})")
            lines.append(f"    POST {base}/shttp/ucl/{{project_id}}  UCL + project ({len(UCL_TOOL_NAMES) - 1})")
            lines.append(f"    POST {base}/shttp/ucl/{{project_id}}/{{skill_id}}  UCL + project + skill ({len(UCL_TOOL_NAMES) - 2})")
        if enable_sse:
            lines.append(f"    GET  {base}/sse                     all tools ({len(TOOLS)})")
            lines.append(f"    GET  {base}/sse/ucl                 UCL tools ({len(UCL_TOOL_NAMES)})")
            lines.append(f"    GET  {base}/sse/ucl/{{project_id}}    UCL + project ({len(UCL_TOOL_NAMES) - 1})")
            lines.append(f"    GET  {base}/sse/ucl/{{project_id}}/{{skill_id}}    UCL + project + skill ({len(UCL_TOOL_NAMES) - 2})")
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
    for tool in TOOLS:
        tag = "[UCL]" if tool.name in UCL_TOOL_NAMES else "[Agent]"
        lines.append(f"    {tag:8s} {tool.name}")

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

    HTTP transports use URL path for mode filtering (/shttp/ucl, /sse/ucl).
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
