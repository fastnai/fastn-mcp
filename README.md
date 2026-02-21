# Fastn MCP Server

**The fastest way to give AI agents access to 250+ enterprise integrations.**

Fastn MCP Server is a production-ready [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) gateway that connects AI coding assistants and agents to Slack, Jira, GitHub, Salesforce, HubSpot, Postgres, and 200+ more services — with centralized auth, observability, and sub-second execution.

Built on the [Fastn SDK](https://github.com/nicely-gg/fastn-sdk), this server exposes MCP tools that any compatible AI platform can use out of the box.

## Why Fastn MCP Server?

| Feature | Description |
|---------|-------------|
| **250+ Connectors** | Slack, Jira, GitHub, Salesforce, HubSpot, Postgres, Stripe, Notion, Linear, and more |
| **MCP Native** | Works with Claude Desktop, Cursor, Lovable, Bolt, v0, and any MCP-compatible client |
| **Centralized Auth** | OAuth 2.1, API keys, and multi-tenant credential management — no per-tool auth setup |
| **Sub-Second Execution** | Direct API calls through the Fastn platform with built-in caching and connection pooling |
| **Multiple Transports** | stdio (local), SSE, and Streamable HTTP for any deployment model |
| **Production Ready** | Docker support, health checks, structured logging, and OAuth 2.1 protected resource metadata |
| **Flow Automation** | Create, manage, and execute multi-step workflows that compose tools |

## Quick Start

### 1. Install

```bash
pip install fastn-mcp-server
```

Or clone and install from source:

```bash
git clone https://github.com/nicely-gg/fastn-mcp.git
cd fastn-mcp
pip install -e ".[dev]"
```

### 2. Configure

Set your Fastn credentials:

```bash
export FASTN_API_KEY="your-api-key"
export FASTN_PROJECT_ID="your-project-id"
```

Get your API key and project ID from [app.fastn.dev](https://app.fastn.dev).

### 3. Run

**Local (stdio) — for Claude Desktop, Cursor:**

```bash
fastn-mcp --stdio
```

**Remote (SSE + Streamable HTTP) — for web-based AI platforms:**

```bash
fastn-mcp --sse --shttp --port 8000
```

**Docker:**

```bash
docker build -t fastn-mcp .
docker run -p 8000:8000 \
  -e FASTN_API_KEY=your-key \
  -e FASTN_PROJECT_ID=your-project-id \
  fastn-mcp
```

## MCP Tools

The server exposes these tools to AI agents:

### Discovery & Execution

| Tool | Description |
|------|-------------|
| `find_tools` | Search for available tools by natural language prompt. Returns matching tools with IDs and input schemas. |
| `execute_action` | Execute a tool by its ID with parameters. Returns the result directly. |
| `discover_tools` | Browse all 250+ available connectors in the registry, including ones not yet connected. |
| `list_projects` | List available workspaces for the authenticated user. |

**Workflow:** `find_tools` → `execute_action`. If `find_tools` returns nothing, call `discover_tools` to check if the connector exists but isn't connected yet.

### Flow Management

| Tool | Description |
|------|-------------|
| `list_flows` | List saved automations (flows) in the workspace. |
| `run_flow` | Execute a saved flow by its ID. |
| `delete_flow` | Remove a flow from the workspace. |
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
│   │ Tool Discovery        │ │  find_tools, discover_tools
│   │ Tool Execution        │ │  execute_action
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

## Transport Modes

### stdio (Local)

For pipe-based clients like Claude Desktop and Cursor:

```bash
fastn-mcp --stdio
```

### SSE + Streamable HTTP (Remote)

For web-based AI platforms. Supports per-path tool filtering:

```bash
fastn-mcp --sse --shttp --port 8000
```

| Endpoint | Tools Exposed |
|----------|--------------|
| `POST /shttp` | All tools (discovery + flows + config) |
| `POST /shttp/ucl` | Discovery tools only (4 tools) |
| `POST /shttp/ucl/{project_id}` | Discovery tools with pre-set project (3 tools) |
| `GET /sse`, `GET /sse/ucl`, etc. | Same pattern for SSE transport |

### Tool Mode (stdio)

Filter tools in stdio mode:

```bash
fastn-mcp --stdio --mode ucl              # Discovery tools only
fastn-mcp --stdio --mode ucl --project ID  # Discovery + pre-set project
```

## Authentication

### OAuth 2.1 (Production)

The server implements RFC 9728 Protected Resource Metadata for OAuth 2.1:

```bash
fastn-mcp --sse --shttp --port 8000
# OAuth endpoints auto-configured at /.well-known/oauth-protected-resource
```

Set the public URL for OAuth metadata:

```bash
fastn-mcp --sse --shttp --port 8000 --server-url https://mcp.example.com
```

### No Auth (Development)

For local testing:

```bash
fastn-mcp --sse --port 8000 --no-auth
```

## Docker Deployment

### Build and Run

```bash
docker build -t fastn-mcp .
docker run -p 8000:8000 \
  -e FASTN_API_KEY=your-key \
  -e FASTN_PROJECT_ID=your-project-id \
  fastn-mcp
```

### Docker Compose

```bash
docker compose up -d
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `FASTN_API_KEY` | Yes | Your Fastn API key from [app.fastn.dev](https://app.fastn.dev) |
| `FASTN_PROJECT_ID` | Yes | Your workspace/project ID |
| `FASTN_MCP_PORT` | No | Server port (default: `8000`) |
| `FASTN_MCP_HOST` | No | Bind address (default: `0.0.0.0`) |
| `FASTN_MCP_SERVER_URL` | No | Public URL for OAuth metadata |
| `FASTN_MCP_NO_AUTH` | No | Set to `true` to disable OAuth (dev only) |
| `FASTN_MCP_TRANSPORT` | No | Transport mode: `sse`, `shttp`, `both` (default: `both`) |
| `FASTN_MCP_VERBOSE` | No | Set to `true` for debug logging |

## Claude Desktop Configuration

Add to your Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "fastn": {
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

## Cursor Configuration

Add to `.cursor/mcp.json` in your project:

```json
{
  "mcpServers": {
    "fastn": {
      "command": "fastn-mcp",
      "args": ["--stdio", "--mode", "ucl"],
      "env": {
        "FASTN_API_KEY": "your-api-key",
        "FASTN_PROJECT_ID": "your-project-id"
      }
    }
  }
}
```

## Development

### Setup

```bash
git clone https://github.com/nicely-gg/fastn-mcp.git
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
│   ├── test_server.py       # 180+ tests covering all tools and transports
│   └── conftest.py          # Test fixtures
├── docs/
│   └── architecture.md      # Detailed architecture documentation
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
  --mode {agent,ucl} Tool mode for stdio: "agent" (all tools) or "ucl" (discovery only)
  --project ID     Pre-set project ID for stdio

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

**And 200+ more** — browse the full catalog at [app.fastn.dev](https://app.fastn.dev)

## License

MIT

## Links

- [Fastn Platform](https://fastn.dev)
- [Fastn SDK](https://github.com/nicely-gg/fastn-sdk)
- [MCP Specification](https://modelcontextprotocol.io/)
- [Documentation](https://docs.fastn.dev)
