# Fastn Architecture & Design

> A technical overview of the Fastn integration platform: SDK, MCP Server, and Control Plane.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Fastn SDK](#2-fastn-sdk)
3. [Fastn MCP Server](#3-fastn-mcp-server)
4. [Fastn Control Plane](#4-fastn-control-plane)
5. [Data Flows](#5-data-flows)
6. [Security & Authentication](#6-security--authentication)
7. [Deployment](#7-deployment)

---

## 1. System Overview

Fastn is an integration platform that enables AI agents and applications to discover, authorize, and execute enterprise integrations through a unified interface. The system comprises three layers:

```
+------------------------------------------------------+
|              AI Agent / Vibe Platform                 |
|         (Lovable, Cursor, Bolt, v0, Claude)          |
+---------------------------+--------------------------+
                            |
                    MCP Protocol / SDK API
                            |
+---------------------------+--------------------------+
|                    Fastn MCP Server                   |
|          (MCP tools over SSE / Streamable HTTP)       |
+---------------------------+--------------------------+
                            |
                      Fastn SDK
                            |
+---------------------------+--------------------------+
|                  Fastn Control Plane                  |
|    (Tool Registry, Action Execution, Auth, Flows)     |
+---------------------------+--------------------------+
                            |
              +-------------+-------------+
              |             |             |
         +----+----+  +----+----+  +-----+-----+
         |  Slack  |  |  Jira   |  |  GitHub   |  ...250+ integrations
         +---------+  +---------+  +-----------+
```

**Design Principles:**

- **Stateless** -- No server-side session state; all context flows per-request
- **Transport-agnostic** -- stdio for local agents, SSE and Streamable HTTP for remote platforms
- **Auth-flexible** -- API keys, JWT tokens, OAuth 2.0 with PKCE, custom auth providers
- **LLM-native** -- Prompt-based tool discovery, output in OpenAI/Anthropic/Gemini formats

---

## 2. Fastn SDK

The Fastn SDK is a Python client library that provides sync (`FastnClient`) and async (`AsyncFastnClient`) interfaces to the Fastn Control Plane.

### 2.1 Client Architecture

```
FastnClient / AsyncFastnClient
    |
    +-- config: FastnConfig          (credentials, project, stage)
    +-- admin.connectors             (local registry browsing)
    +-- flows                        (flow CRUD + execution)
    +-- projects                     (workspace enumeration)
    +-- connections                  (OAuth connection management)
    +-- execute(action_id, params)   (direct action execution)
    +-- get_tools_for(prompt)        (AI-powered tool discovery)
    +-- <connector>.<action>(...)    (dynamic dispatch)
```

### 2.2 Configuration Resolution

Configuration is resolved with the following priority (highest first):

| Priority | Source                  | Example                                |
| -------- | ---------------------- | -------------------------------------- |
| 1        | Constructor parameters  | `FastnClient(api_key="sk_...")  `      |
| 2        | Environment variables   | `FASTN_API_KEY`, `FASTN_PROJECT_ID`    |
| 3        | Config file             | `.fastn/config.json`                   |

Environment variables:

| Variable           | Purpose                                          |
| ------------------ | ------------------------------------------------ |
| `FASTN_API_KEY`    | API key for workspace authentication             |
| `FASTN_PROJECT_ID` | Workspace / project UUID                         |
| `FASTN_AUTH_TOKEN`  | JWT from `fastn login` (Keycloak-issued)         |
| `FASTN_TENANT_ID`  | Tenant ID for multi-tenant operations            |
| `FASTN_STAGE`      | Environment: `LIVE` (default), `STAGING`, `DEV`  |

### 2.3 Core Capabilities

#### Tool Discovery

Two approaches for finding integration actions:

**Prompt-based discovery** (recommended for AI agents):

```python
actions = await client.get_tools_for(
    prompt="send a message on Slack",
    format="raw",    # or "openai", "anthropic", "gemini", "bedrock"
    limit=5,
)
```

Calls `POST /api/ucl/getTools` -- the server uses AI to match the prompt against the tool registry and returns actions with their `actionId` and `inputSchema`.

**Registry browsing** (for exploration):

```python
tools = client.admin.connectors.list()         # all integrations
actions = client.admin.connectors.get_tools("slack")  # actions for one tool
```

Reads from the local registry (`.fastn/registry.json`), synced via `fastn sync`.

#### Action Execution

```python
result = await client.execute(
    action_id="act_slack_send_message",
    params={"channel": "general", "text": "Hello"},
    connection_id=None,   # optional, for multi-connection
    tenant_id=None,       # optional tenant override
)
```

Calls `POST /api/ucl/executeTool`. The Control Plane proxies the request to the target integration (Slack, Jira, etc.) and returns the result.

#### Dynamic Dispatch

```python
# Attribute-based access — connector.action(params)
result = client.slack.send_message(channel="general", text="Hello")
result = client.jira.create_issue(project="PROJ", summary="Bug fix")
```

The SDK intercepts attribute access via `__getattr__`, resolves the connector and action from the registry, and calls `execute()` internally.

#### Flow Management

```python
flows = await client.flows.list(status="active")
result = await client.flows.run(flow_id="flow_abc", params={})
await client.flows.delete(flow_id="flow_abc")
```

Flows are saved automations. Listing uses GraphQL; execution routes through `executeTool`; deletion uses the `deleteModelSchema` GraphQL mutation.

#### Project Enumeration

```python
projects = await client.projects.list()
# [{"id": "proj_abc", "name": "My Workspace", "packageType": "free"}, ...]
```

Uses the `getOrganizations` GraphQL query. Requires JWT authentication.

### 2.4 LLM Format Support

The SDK formats tool schemas for direct use with major LLM providers:

| Format       | Target                        |
| ------------ | ----------------------------- |
| `"openai"`   | OpenAI function calling       |
| `"anthropic"`| Anthropic tool use            |
| `"gemini"`   | Google Gemini / Vertex AI     |
| `"bedrock"`  | AWS Bedrock Converse API      |
| `"raw"`      | Fastn native schemas          |

### 2.5 Exception Hierarchy

```
FastnError
+-- AuthError
|   +-- OAuthError
+-- ConfigError
+-- ConnectorNotFoundError
+-- ToolNotFoundError
+-- ConnectionNotFoundError
+-- FlowNotFoundError
+-- RunNotFoundError
+-- APIError
+-- RegistryError
```

---

## 3. Fastn MCP Server

The MCP Server wraps the Fastn SDK and exposes it as MCP (Model Context Protocol) tools for AI agent platforms.

### 3.1 Transport Modes

```
+-------------------+-------------------------------------------+
|    Transport      |  Use Case                                 |
+-------------------+-------------------------------------------+
| stdio             | Local agents (Claude Desktop, Cursor)     |
| SSE               | Remote platforms (legacy, bidirectional)   |
| Streamable HTTP   | Remote platforms (recommended, stateless)  |
| SSE + shttp       | Both transports simultaneously            |
+-------------------+-------------------------------------------+
```

**Endpoints (runtime path-based mode filtering):**

```
POST /shttp                     all tools (10)
POST /shttp/ucl                 UCL tools only (4)
POST /shttp/ucl/{project_id}    UCL + pre-set project (3)

GET  /sse                       all tools (10)
GET  /sse/ucl                   UCL tools only (4)
GET  /sse/ucl/{project_id}      UCL + pre-set project (3)
POST /messages/                 SSE message posting
```

### 3.2 Tool Categories

The server exposes two categories of tools, filtered at runtime by the URL path the client connects to:

#### UCL Tools (Discovery + Execution)

| Tool              | Purpose                                           |
| ----------------- | ------------------------------------------------- |
| `find_tools`      | Prompt-based search for executable actions        |
| `execute_action`  | Run an action by its `actionId`                   |
| `discover_tools`  | Browse all tools in the workspace registry        |
| `list_projects`   | List workspaces for the authenticated user        |

**Intended workflow:** `find_tools` (describe what you need) -> `execute_action` (run the matched action)

#### Agent Tools (Flow Management + Config)

| Tool                     | Purpose                                  |
| ------------------------ | ---------------------------------------- |
| `list_flows`             | List saved automations                   |
| `run_flow`               | Execute a flow by its ID                 |
| `delete_flow`            | Remove a flow (moves to trash)           |
| `create_flow`            | Create a flow from prompt (under dev)    |
| `update_flow`            | Update an existing flow (under dev)      |
| `configure_custom_auth`  | Register a custom JWT issuer             |

### 3.3 Mode Filtering Architecture

Mode filtering happens at runtime based on the URL path the client connects to:

```
Client connects to /shttp/ucl
    |
    v
handle_shttp_request()
    |-- Parse path: /ucl -> mode="ucl"
    |-- Inject x-fastn-mode header into ASGI scope
    |-- Delegate to StreamableHTTPSessionManager
            |
            v
        MCP SDK spawns server task (fresh context)
            |
            v
        handle_list_tools()
            |-- Read x-fastn-mode from request headers
            |-- Filter tools: return UCL tools only
```

The mode is injected as an HTTP header (`x-fastn-mode`) into the ASGI scope because the MCP SDK's stateless mode spawns server tasks in a separate context where Python `ContextVar` values are not propagated.

### 3.4 Token Resolution

For each tool call, the server resolves authentication in priority order:

```
1. OAuth provider     MCP token -> Keycloak token mapping
2. Bearer header      Authorization: Bearer <token> passthrough
3. Tool arguments     access_token field in the tool call
```

### 3.5 Error Mapping

| MCP Error Code      | SDK Exception    | Meaning                          |
| -------------------- | --------------- | -------------------------------- |
| `INVALID_TOKEN`      | `AuthError`     | Invalid or expired credentials   |
| `WORKSPACE_NOT_SET`  | `ConfigError`   | Missing project ID               |
| `FASTN_ERROR`        | `APIError`      | Control Plane API error          |
| `UNDER_DEVELOPMENT`  | --              | Feature not yet available        |
| `MISSING_PARAM`      | --              | Required argument missing        |
| `AUTH_REQUIRED`      | `ConfigError`   | No credentials available         |

### 3.6 ASGI Application Stack

```
Starlette Application
    |
    +-- CORSMiddleware         (allow cross-origin requests)
    +-- AuthenticationMiddleware (Bearer token extraction, if OAuth)
    +-- AuthContextMiddleware   (store token in request context, if OAuth)
    |
    +-- Routes:
        +-- GET  /                              Server info (JSON)
        +-- GET  /sse, /sse/ucl, /sse/ucl/{id}  SSE connections
        +-- POST /messages/                     SSE message posting
        +-- POST /shttp/**                      Streamable HTTP (Mount)
        +-- OAuth routes (if enabled):
            +-- GET  /.well-known/oauth-*       Metadata
            +-- POST /authorize                 Authorization
            +-- POST /token                     Token exchange
            +-- POST /register                  Client registration
            +-- POST /revoke                    Token revocation
            +-- GET  /callback                  Keycloak callback
    |
    +-- Lifespan:
        +-- StreamableHTTPSessionManager        (stateless, shared)
        +-- Token cleanup loop                  (every 5 min, if OAuth)
```

---

## 4. Fastn Control Plane

The Control Plane is the cloud backend that manages the tool registry, executes actions, handles authentication, and orchestrates flows.

### 4.1 API Surface

| Endpoint                          | Method | Purpose                        |
| --------------------------------- | ------ | ------------------------------ |
| `/api/ucl/getTools`               | POST   | AI-powered tool discovery      |
| `/api/ucl/executeTool`            | POST   | Execute an action              |
| `/api/connections/initiate`       | POST   | Start OAuth flow for a tool    |
| `/api/connections/status`         | GET    | Check connection status        |
| `/api/auth/configure_custom_auth` | POST   | Register custom JWT issuer     |
| `/api/graphql`                    | POST   | GraphQL (projects, flows)      |

### 4.2 Tool Registry

The registry catalogs 250+ integrations across categories:

| Category              | Examples                                  |
| --------------------- | ----------------------------------------- |
| Communication         | Slack, Teams, Discord, Telegram, Email    |
| Project Management    | Jira, Asana, Monday, Trello, Linear       |
| Development           | GitHub, GitLab, Bitbucket, Azure DevOps   |
| CRM                   | Salesforce, HubSpot, Pipedrive            |
| Data & Analytics      | Google Sheets, Airtable, Datadog          |
| Cloud Infrastructure  | AWS, Google Cloud, Azure                  |

Each integration exposes multiple actions (e.g., Slack: `send_message`, `create_channel`, `list_users`). Each action has a typed `inputSchema` and `outputSchema`.

### 4.3 Action Execution

When `executeTool` is called:

1. The Control Plane validates the request (auth, workspace, action exists)
2. Retrieves the user's connection credentials for the target integration
3. Translates the normalized parameters to the integration's native API format
4. Calls the integration's API (e.g., Slack Web API)
5. Returns the result in a normalized format

This proxy model means:
- Users never handle raw integration credentials in their code
- The SDK/MCP server never sees OAuth tokens for individual integrations
- Connection management (OAuth flows, token refresh) is handled by the platform

### 4.4 GraphQL Queries

Used internally for control plane operations:

- `getOrganizations` -- List user's projects/workspaces
- `callCoreProjectFlow` -- List flows (registered tools) in a workspace
- `deleteModelSchema` -- Delete a flow (move to trash)

---

## 5. Data Flows

### 5.1 Tool Discovery and Execution (Primary Workflow)

```
AI Agent                    MCP Server              SDK                 Control Plane
   |                            |                    |                       |
   |-- tools/list ------------->|                    |                       |
   |<-- [find_tools,           |                    |                       |
   |     execute_action, ...]  |                    |                       |
   |                            |                    |                       |
   |-- find_tools              |                    |                       |
   |   {prompt: "send Slack    |                    |                       |
   |    message"}              |                    |                       |
   |--------------------------->|                    |                       |
   |                            |-- get_tools_for -->|                       |
   |                            |                    |-- POST /getTools ---->|
   |                            |                    |<-- actions[] ---------|
   |                            |<-- actions[] ------|                       |
   |<-- {actions: [{           |                    |                       |
   |      actionId: "act_...", |                    |                       |
   |      inputSchema: {...}   |                    |                       |
   |    }]}                    |                    |                       |
   |                            |                    |                       |
   |-- execute_action          |                    |                       |
   |   {action_id: "act_...", |                    |                       |
   |    parameters: {          |                    |                       |
   |      channel: "general",  |                    |                       |
   |      text: "Hello"       |                    |                       |
   |    }}                     |                    |                       |
   |--------------------------->|                    |                       |
   |                            |-- execute() ------>|                       |
   |                            |                    |-- POST /executeTool ->|
   |                            |                    |                       |-- Slack API
   |                            |                    |                       |<- response
   |                            |                    |<-- result ------------|
   |                            |<-- result ---------|                       |
   |<-- {ok: true, ...}       |                    |                       |
```

### 5.2 OAuth Authentication Flow

```
MCP Client          MCP Server                  Keycloak
   |                    |                           |
   |-- GET /authorize ->|                           |
   |   (PKCE challenge) |                           |
   |                    |-- Generate KC PKCE ------->|
   |                    |   Store pending auth       |
   |<-- 302 to KC -----|                           |
   |                    |                           |
   |-- GET /auth ------>|                           |
   |                    |            [User logs in]  |
   |<-- 302 /callback --|                           |
   |                    |                           |
   |-- GET /callback -->|                           |
   |   (KC code)        |-- Exchange KC code ------>|
   |                    |<-- KC tokens -------------|
   |                    |   Generate MCP code        |
   |<-- 302 to client --|   Map MCP -> KC token     |
   |   (MCP code)       |                           |
   |                    |                           |
   |-- POST /token ---->|                           |
   |   (MCP code +      |                           |
   |    code_verifier)  |                           |
   |                    |-- Issue MCP access_token   |
   |<-- {access_token,  |   Map MCP token -> KC     |
   |     refresh_token} |                           |
   |                    |                           |
   |-- Bearer <token> ->|                           |
   |   (tool calls)     |-- Resolve KC token        |
   |                    |   from MCP token mapping   |
   |                    |-- SDK call with KC token   |
```

The MCP server acts as a bridge between the MCP OAuth flow (which the AI platform understands) and Keycloak (which Fastn uses for identity). Double PKCE is used: one between the MCP client and server, another between the server and Keycloak.

### 5.3 Connection Management

```
User App              SDK / MCP              Control Plane         Integration
   |                      |                       |                     |
   |-- initiate           |                       |                     |
   |   (connector="slack")|                       |                     |
   |--------------------->|                       |                     |
   |                      |-- POST /initiate ---->|                     |
   |                      |<-- {auth_url,         |                     |
   |                      |     connection_id} ---|                     |
   |<-- auth_url ---------|                       |                     |
   |                      |                       |                     |
   |  [User completes OAuth in browser]           |                     |
   |                      |                       |-- OAuth exchange -->|
   |                      |                       |<-- tokens ---------|
   |                      |                       |   Store encrypted   |
   |                      |                       |                     |
   |-- execute_action --->|                       |                     |
   |   (uses connection)  |-- executeTool ------->|                     |
   |                      |                       |-- Use stored ------>|
   |                      |                       |   credentials       |
   |                      |                       |<-- result ---------|
   |<-- result -----------|                       |                     |
```

---

## 6. Security & Authentication

### 6.1 Authentication Methods

| Method                | Use Case                                    | Mechanism                        |
| --------------------- | ------------------------------------------- | -------------------------------- |
| API Key               | Service-to-service                          | `x-fastn-api-key` header         |
| JWT (Bearer Token)    | User-authenticated sessions                 | `Authorization: Bearer <jwt>`    |
| OAuth 2.0 + PKCE      | Remote MCP clients (Lovable, Bolt, etc.)    | Double PKCE via Keycloak         |
| Custom Auth Provider  | End-user tokens from Auth0, Firebase, etc.  | JWKS validation                  |

### 6.2 Credential Protection

- Config file stored with `0o600` permissions (owner read/write only)
- Sensitive headers redacted in logs (`authorization`, `cookie`, `x-fastn-api-key`)
- Sensitive tool arguments redacted in logs (`access_token`, `api_key`)
- Integration credentials never leave the Control Plane

### 6.3 OAuth Security

- PKCE (S256) for both MCP and Keycloak legs
- Strict redirect URI allowlist (hardcoded domains)
- Auth codes expire in 5 minutes
- Access tokens expire in 1 hour
- Refresh tokens expire in 24 hours
- Client registration rate limited (10/minute)
- Expired tokens cleaned up every 5 minutes

### 6.4 Multi-Tenancy

Per-call tenant isolation via `tenant_id`:

```python
client.execute(action_id="...", params={...}, tenant_id="acme-corp")
```

The Control Plane uses the tenant ID to resolve the correct connection credentials, ensuring tenant A's Slack token is never used for tenant B's requests.

---

## 7. Deployment

### 7.1 Local Agent (stdio)

For Claude Desktop, Cursor, and other local MCP clients:

```json
{
  "mcpServers": {
    "fastn": {
      "command": "python",
      "args": ["-m", "fastn_mcp", "--stdio"]
    }
  }
}
```

### 7.2 Remote Server

For Lovable, Bolt, v0, and other remote platforms:

```bash
# Production with OAuth
python -m fastn_mcp --shttp --port 8000 \
    --server-url https://mcp.example.com

# Development without OAuth
python -m fastn_mcp --sse --shttp --port 8000 --no-auth --verbose
```

### 7.3 CLI Options

```
--stdio              stdio transport (local agents)
--sse                SSE transport (remote, legacy)
--shttp              Streamable HTTP transport (remote, recommended)
--host HOST          Bind address (default: 0.0.0.0)
--port PORT          Port (default: 8000)
--no-auth            Disable OAuth (for local testing)
--server-url URL     Public URL for OAuth metadata
--mode {all,ucl}     Tool mode for stdio (default: all)
--project ID         Pre-set project ID for stdio
-v, --verbose        Enable verbose logging
```

### 7.4 Environments

| Stage     | Base URL                    | Purpose        |
| --------- | --------------------------- | -------------- |
| `LIVE`    | `https://live.fastn.ai`     | Production     |
| `STAGING` | `https://staging.fastn.ai`  | Pre-production |
| `DEV`     | `https://dev.fastn.ai`      | Development    |

---

*This document describes the architecture of the Fastn integration platform as of February 2026.*
