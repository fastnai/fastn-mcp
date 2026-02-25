# Fastn MCP Server

**Give your AI agents and apps instant, secure access to 250+ enterprise systems.**

Fastn MCP Server is a production-ready [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) gateway that connects AI agents and apps to Slack, Jira, GitHub, Salesforce, HubSpot, Postgres, and 200+ more services — with fully managed auth, governed access, and sub-second execution.

Built on the [Fastn SDK](https://github.com/fastnai/fastn-sdk), this server exposes MCP tools that any compatible AI platform can use out of the box.

## Why Fastn MCP Server?

| Feature | Description |
|---------|-------------|
| **250+ Connectors** | Slack, Jira, GitHub, Salesforce, HubSpot, Postgres, Stripe, Notion, Linear, and more |
| **MCP Native** | Works with Claude Desktop, Cursor, Lovable, Bolt, v0, and any MCP-compatible client |
| **Fully Managed Auth** | OAuth 2.1 for every connector — no token management, no app registration, no refresh handling |
| **Governed Access** | Role-based permissions, audit trails, and enterprise compliance controls |
| **SOC 2 Certified** | Enterprise-grade security and compliance built into the platform |
| **Sub-Second Execution** | Direct API calls through the Fastn platform with built-in caching and connection pooling |
| **Multiple Transports** | stdio (local), SSE, and Streamable HTTP for any deployment model |
| **Production Ready** | Docker support, health checks, structured logging, and OAuth 2.1 protected resource metadata |
| **Flow Automation** | Create, manage, and execute multi-step workflows that compose tools |

## Quick Start

### 1. Sign up at [app.fastn.ai](https://app.fastn.ai)

Create an account and connect your first connectors (Gmail, Slack, GitHub, etc.).

### 2. Connect your MCP client

**Hosted server (recommended)** — no installation needed:

```
https://mcp.live.fastn.ai/shttp
```

Point any MCP client at this URL. Authentication is handled via MCP OAuth 2.1 automatically. See [Client Configuration](#client-configuration) for Claude Desktop and Cursor examples.

**Self-hosted** — install and run your own instance:

```bash
pip install fastn-mcp-server
fastn-mcp --shttp --port 8000
```

## Connecting to the MCP Server

### Endpoints

Use the hosted server (`mcp.live.fastn.ai`) or your self-hosted instance. Each path exposes a different set of tools:

| Endpoint | Tools |
|----------|-------|
| `/shttp` | All 11 tools: `find_tools`, `execute_tool`, `list_connectors`, `list_skills`, `list_projects`, `list_flows`, `run_flow`, `delete_flow`, `create_flow`, `update_flow`, `configure_custom_auth` |
| `/shttp/tools` | 5 tools: `find_tools`, `execute_tool`, `list_connectors`, `list_skills`, `list_projects` |
| `/shttp/tools/{project_id}` | 4 tools: `find_tools`, `execute_tool`, `list_connectors`, `list_skills` |
| `/shttp/tools/{project_id}/{skill_id}` | 3 tools: `find_tools`, `execute_tool`, `list_connectors` |
| `/sse`, `/sse/tools`, etc. | Same pattern for SSE transport |

**Examples with the hosted server:**

```
https://mcp.live.fastn.ai/shttp                              # All tools
https://mcp.live.fastn.ai/shttp/tools                           # Fastn tools only
https://mcp.live.fastn.ai/shttp/tools/{project_id}              # Pre-set project
https://mcp.live.fastn.ai/shttp/tools/{project_id}/{skill_id}   # Pre-set project + skill
```

### Authentication

The MCP server supports three authentication methods. Get your credentials from [app.fastn.ai](https://app.fastn.ai).

#### MCP OAuth 2.1 (Recommended)

Standard MCP OAuth flow with PKCE. The server bridges to Fastn's identity provider automatically. Just point your client at the URL — you'll be prompted to authenticate:

```json
{
  "mcpServers": {
    "tools": {
      "url": "https://mcp.live.fastn.ai/shttp"
    }
  }
}
```

#### Token / API Key

Pass a Fastn auth token or API key via the `Authorization` header in your MCP client config:

```json
{
  "mcpServers": {
    "tools": {
      "url": "https://mcp.live.fastn.ai/shttp",
      "headers": {
        "Authorization": "Bearer <your-token-or-api-key>"
      }
    }
  }
}
```

You can also pass `x-project-id` to scope requests to a specific project:

```json
{
  "mcpServers": {
    "tools": {
      "url": "https://mcp.live.fastn.ai/shttp",
      "headers": {
        "Authorization": "Bearer <your-token-or-api-key>",
        "x-project-id": "<your-project-id>"
      }
    }
  }
}
```

For local stdio transport, pass credentials as environment variables:

```json
{
  "mcpServers": {
    "tools": {
      "command": "fastn-mcp",
      "args": ["--stdio"],
      "env": {
        "FASTN_API_KEY": "your-api-key",
        "FASTN_PROJECT_ID": "your-project-id"
      }
    }
  }
}
```

## MCP Tools

The server exposes these tools to AI agents:

### Fastn Tools

| Tool | Description |
|------|-------------|
| `find_tools` | Search for available connector tools by natural language prompt. Returns matching tools with IDs and input schemas. |
| `execute_tool` | Execute a connector tool by its ID with parameters. Returns the result directly. |
| `list_connectors` | Browse all 250+ available connectors in the registry, including ones not yet connected. |
| `list_skills` | List available skills in the project. |
| `list_projects` | List available projects for the authenticated user. |

**Workflow:** Browse connectors → `list_connectors`. Execute an action → `find_tools` → `execute_tool`. If `find_tools` returns nothing relevant → `list_connectors` (connector may need connecting).

### Flow Management

| Tool | Description |
|------|-------------|
| `list_flows` | List saved automations (flows) in the project. |
| `run_flow` | Execute a saved flow by its ID. |
| `delete_flow` | Remove a flow from the project. |
| `create_flow` | *(Under development)* Create a flow from natural language. |
| `update_flow` | *(Under development)* Update an existing flow. |

### Configuration

| Tool | Description |
|------|-------------|
| `configure_custom_auth` | Register a custom JWT auth provider (Auth0, Firebase, Supabase). |

## Architecture

```
AI Agent (Claude, Cursor, Lovable)
    │
    ▼
MCP Protocol (stdio / SSE / Streamable HTTP)
    │
    ▼
┌─────────────────────────────┐
│   Fastn MCP Server          │
│   ┌───────────────────────┐ │
│   │ Tool Discovery        │ │  find_tools, list_connectors
│   │ Tool Execution        │ │  execute_tool
│   │ Flow Management       │ │  list/run/delete/create flows
│   │ Auth & Config         │ │  OAuth 2.1, custom JWT
│   └───────────────────────┘ │
└─────────────────────────────┘
    │
    ▼
Fastn SDK → Fastn API
    │
    ▼
250+ Connectors (Slack, Jira, GitHub, Salesforce, Postgres, ...)
```

**Connectors** provide tools. **Flows** compose tools. **Agents** run flows and tools with reasoning.

## Client Configuration

The JSON examples above work with any MCP client. Here are the config file locations:

| Client | Config File |
|--------|------------|
| **Claude Desktop** | `claude_desktop_config.json` |
| **Cursor** | `.cursor/mcp.json` in your project |
| **Claude Code** | `.mcp.json` in your project |

For Cursor, use the `/shttp/tools` endpoint to expose only Fastn tools (recommended for coding assistants):

```json
{
  "mcpServers": {
    "tools": {
      "url": "https://mcp.live.fastn.ai/shttp/tools"
    }
  }
}
```

## Self-Hosting

### Transport Modes

**stdio (local)** — For pipe-based clients:

```bash
fastn-mcp --stdio
fastn-mcp --stdio --mode tools                          # Fastn tools only
fastn-mcp --stdio --mode tools --project ID              # Fastn tools + pre-set project
fastn-mcp --stdio --mode tools --project ID --skill ID   # Fastn tools + pre-set project and skill
```

**SSE + Streamable HTTP (remote)** — For web-based AI platforms:

```bash
fastn-mcp --sse --shttp --port 8000
```

### OAuth Setup

Remote transports use MCP OAuth 2.1 by default. The server implements RFC 9728 Protected Resource Metadata:

```bash
fastn-mcp --sse --shttp --port 8000
# OAuth endpoints auto-configured at /.well-known/oauth-protected-resource
```

Set the public URL for OAuth metadata:

```bash
fastn-mcp --sse --shttp --port 8000 --server-url https://mcp.example.com
```

To disable OAuth for local development:

```bash
fastn-mcp --sse --port 8000 --no-auth
```

### Docker

```bash
docker build -t fastn-mcp .
docker run -p 8000:8000 fastn-mcp
```

```bash
docker compose up -d
```

### Environment Variables

Server configuration for self-hosted deployments:

```bash
FASTN_MCP_PORT=8000
FASTN_MCP_HOST=0.0.0.0
FASTN_MCP_SERVER_URL=https://your-public-url
FASTN_MCP_TRANSPORT=both
FASTN_MCP_NO_AUTH=false
FASTN_MCP_VERBOSE=false
```

| Variable | Required | Description |
|----------|----------|-------------|
| `FASTN_MCP_PORT` | No | Server port (default: `8000`) |
| `FASTN_MCP_HOST` | No | Bind address (default: `0.0.0.0`) |
| `FASTN_MCP_SERVER_URL` | No | Public URL for OAuth metadata |
| `FASTN_MCP_NO_AUTH` | No | Set to `true` to disable OAuth (dev only) |
| `FASTN_MCP_TRANSPORT` | No | Transport mode: `sse`, `shttp`, `both` (default: `both`) |
| `FASTN_MCP_VERBOSE` | No | Set to `true` for debug logging |

### Local Development with ngrok

For local development and testing only — use ngrok to expose your local server:

```bash
# Terminal 1: Start the MCP server
fastn-mcp --shttp --port 8000 --verbose

# Terminal 2: Start ngrok tunnel
ngrok http 8000
```

Then restart with the ngrok URL so OAuth metadata resolves:

```bash
fastn-mcp --shttp --port 8000 \
  --server-url https://abc123.ngrok-free.dev \
  --verbose
```

For production, deploy with Docker behind a reverse proxy with a real domain.

## Development

### Setup

```bash
git clone https://github.com/fastnai/fastn-mcp.git
cd fastn-mcp
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Run Tests

```bash
pytest tests/ -q
```

### Project Structure

```
fastn-mcp/
├── fastn_mcp/
│   ├── __init__.py          # Package metadata
│   ├── __main__.py          # CLI entry point
│   ├── server.py            # MCP server, tools, handlers, routing
│   ├── auth.py              # OAuth 2.1 provider (RFC 9728)
│   └── tools/               # Tool utilities
├── tests/
│   ├── test_server.py       # 185+ tests covering all tools and transports
│   └── conftest.py          # Test fixtures
├── Dockerfile               # Production container
├── docker-compose.yml       # Docker Compose for deployment
├── pyproject.toml           # Python project configuration
└── README.md
```

## CLI Reference

```
fastn-mcp [OPTIONS]

Transport:
  --stdio          Use stdio transport (Claude Desktop, Cursor)
  --sse            Enable SSE transport
  --shttp          Enable Streamable HTTP transport

Server:
  --host HOST      Bind address (default: 0.0.0.0)
  --port PORT      Port number (default: 8000)
  --no-auth        Disable OAuth (development only)
  --server-url URL Public URL for OAuth metadata

Mode:
  --mode {agent,tools} Tool mode for stdio: "agent" (all tools) or "tools" (Fastn tools only)
  --project ID     Pre-set project ID for stdio
  --skill ID       Pre-set skill ID for stdio (requires --project)

Debug:
  -v, --verbose    Enable debug logging
```

## Supported Connectors

Fastn provides 250+ pre-built connectors including:

**Communication:** Slack, Microsoft Teams, Discord, Gmail, Outlook, SendGrid, Twilio

**Project Management:** Jira, Linear, Asana, Trello, Monday.com, ClickUp, Notion

**Development:** GitHub, GitLab, Bitbucket, PagerDuty, Sentry, Datadog

**CRM & Sales:** Salesforce, HubSpot, Pipedrive, Zoho CRM

**Databases:** PostgreSQL, MySQL, MongoDB, Redis, Supabase, Firebase

**Cloud:** AWS, Google Cloud, Azure, Cloudflare

**Finance:** Stripe, QuickBooks, Xero

**And 200+ more** — browse the full catalog at [fastn.ai/integrations](https://fastn.ai/integrations)

## License

MIT

## Links

- [Fastn Platform](https://fastn.ai)
- [Fastn SDK](https://github.com/fastnai/fastn-sdk)
- [MCP Specification](https://modelcontextprotocol.io/)
- [Documentation](https://docs.fastn.ai)
