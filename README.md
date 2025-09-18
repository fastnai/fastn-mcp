# Unified Context Layer (UCL) MCP Gateway 

Unified Context Layer (UCL) is a multi-tenant Model Context Protocol (MCP) server that enables AI agents, automation platforms, and applications to connect to over 1,000 SaaS tools—such as Slack, Jira, Gmail, Shopify, Notion, and more—via a single standardized /command endpoint. UCL abstracts away SDK sprawl, glue code, and complex authentication flows, allowing developers to orchestrate context-rich, cross-platform integrations without building and maintaining separate connectors for each service.

## Features

- **Integrated platform support** - Use services like Slack, Notion, HubSpot, and more through the Fastn server
- **Flexible authentication** - Use either API key or tenant-based authentication
- **Comprehensive logging** - Detailed logs for troubleshooting
- **Error handling** - Robust error management for various scenarios

## Prerequisites

- Python 3.10 or higher

## Installation Options

### Option 1: Package Installation (Recommended)

The easiest way to install the UCL server is using pip:

```bash
pip install fastn-mcp-server
```

To find the exact path of the installed command:
- On macOS/Linux: `which fastn-mcp-server`
- On Windows: `where fastn-mcp-server`

## After Package Installation

```bash
{
  "mcpServers": {
      "fastn": {
          "command": "fastn-mcp-server",
          "args": [
              "--api_key",
              "YOUR_API_KEY",
              "--space_id",
              "YOUR_WORKSPACE_ID"
          ]
      }
  }
}
```

### Option 2: Manual Setup

```bash
# Clone repository and navigate to directory
git clone <your-repo-url> && cd fastn-server

# macOS/Linux: Install UV, create virtual environment, and install dependencies
curl -LsSf https://astral.sh/uv/install.sh | sh && uv venv && source .venv/bin/activate && uv pip install -e .

# Windows (PowerShell): Install UV, create a virtual environment, and install dependencies
powershell -c "irm https://astral.sh/uv/install.ps1 | iex" && uv venv && .venv\Scripts\activate && uv pip install -e .

# Install dependencies directly
uv pip install "httpx>=0.28.1" "mcp[cli]>=1.2.0"
```

## UCL Account Setup

1. Log in to your UCL account or sign up for a new UCL account
3. Activate the service(s)/connector(s) you want to use
4. Go to the "Integrate" section on the left-hand side and follow the provided instructions to connect UCL to your agents.
5. Alternatively, you can also select and different method to use UCL as mentioned within the integrate section.

## Running the Server

The server supports two authentication methods:

### Authentication Method 1: API Key

```bash
# Package installation
fastn-mcp-server --api_key YOUR_API_KEY --space_id YOUR_WORKSPACE_ID

# Manual installation
uv run fastn-server.py --api_key YOUR_API_KEY --space_id YOUR_WORKSPACE_ID
```

### Authentication Method 2: Tenant-based

```bash
# Package installation
fastn-mcp-server --space_id YOUR_WORKSPACE_ID --tenant_id YOUR_TENANT_ID --auth_token YOUR_AUTH_TOKEN

# Manual installation
uv run fastn-server.py --space_id YOUR_WORKSPACE_ID --tenant_id YOUR_TENANT_ID --auth_token YOUR_AUTH_TOKEN
```

## Integration with AI Assistants

### Claude Integration

1. Open the Claude configuration file:
   - Windows: `notepad "%APPDATA%\Claude\claude_desktop_config.json"` or `code "%APPDATA%\Claude\claude_desktop_config.json"`
   - Mac: `code ~/Library/Application\ Support/Claude/claude_desktop_config.json`

2. Add the appropriate configuration:

#### Using Package Installation

```json
{
    "mcpServers": {
        "fastn": {
            "command": "/path/to/fastn-mcp-server",
            "args": [
                "--api_key",
                "YOUR_API_KEY",
                "--space_id",
                "YOUR_WORKSPACE_ID"
            ]
        }
    }
}
```

Or with tenant authentication:

```json
{
    "mcpServers": {
        "fastn": {
            "command": "/path/to/fastn-mcp-server",
            "args": [
                "--space_id",
                "YOUR_WORKSPACE_ID",
                "--tenant_id",
                "YOUR_TENANT_ID",
                "--auth_token",
                "YOUR_AUTH_TOKEN"
            ]
        }
    }
}
```

#### Using Manual Installation

API Key authentication:

```json
{
    "mcpServers": {
        "fastn": {
            "command": "/path/to/your/uv",
            "args": [
                "--directory",
                "/path/to/your/fastn-server",
                "run",
                "fastn-server.py",
                "--api_key",
                "YOUR_API_KEY",
                "--space_id",
                "YOUR_WORKSPACE_ID"
            ]
        }
    }
}
```

Tenant authentication:

```json
{
    "mcpServers": {
        "fastn": {
            "command": "/path/to/your/uv",
            "args": [
                "--directory",
                "/path/to/your/fastn-server",
                "run",
                "fastn-server.py",
                "--space_id",
                "YOUR_WORKSPACE_ID",
                "--tenant_id",
                "YOUR_TENANT_ID",
                "--auth_token",
                "YOUR_AUTH_TOKEN"
            ]
        }
    }
}
```

### Cursor Integration

1. Open Cursor settings
2. Navigate to the "Tools & Integrations" tab and click "Add Custom MCP"
3. Click on "Add new MCP server"
4. Add a name for your server (e.g., "fastn")
5. Head back to UCL and within the Integrate section, head over to "Real Time Event Streaming" mentioned at the bottom of the Integrate section
6. Copy the JSON command and head back to Cursor to paste the file in mcp.json and save.

## Docker Integration

### Step 1: Setup Environment Configuration

Create a `.env` file in your project directory with your UCL credentials:

```bash
# Configuration Format 1: Basic API Key and Space ID
API_KEY=your_actual_api_key
SPACE_ID=your_actual_space_id

# Configuration Format 2: Extended with Tenant ID and Auth Token
TENANT_ID=your_tenant_id
AUTH_TOKEN=your_actual_auth_token

# Set configuration mode: "basic" or "extended"
CONFIG_MODE=extended
```

### Step 2: Build and Run with Docker Compose

First, build and start the container:

```bash
docker-compose up --build
```

This will create the UCL server image and verify it starts correctly.

### Step 3: Configure AI Assistants for Docker Integration

#### Claude Desktop Integration

1. Open the Claude configuration file:
   - Windows: `notepad "%APPDATA%\Claude\claude_desktop_config.json"`
   - Mac: `code ~/Library/Application\ Support/Claude/claude_desktop_config.json`

2. Add the Docker configuration:

```json
{
  "mcpServers": {
    "ucl": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "--env-file", "/path/to/your/fastn-stdio-server/.env",
        "ucl-stdio-server"
      ]
    }
  }
}
```

**Note:** Replace `/path/to/your/fastn-stdio-server/.env` with the actual path to your `.env` file.

#### Alternative: Using Environment Variables

If you prefer to pass environment variables directly:

```json
{
  "mcpServers": {
    "ucl": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "API_KEY=your_actual_api_key",
        "-e", "SPACE_ID=your_actual_space_id", 
        "-e", "TENANT_ID=your_tenant_id",
        "-e", "AUTH_TOKEN=your_actual_auth_token",
        "-e", "CONFIG_MODE=extended",
        "ucl-stdio-server"
      ]
    }
  }
}
```

### Benefits of Docker Integration

- **Isolation**: UCL server runs in a secure container environment
- **Consistency**: Same runtime across different machines and platforms
- **Easy Setup**: No need to install Python dependencies locally
- **Scalability**: Can be deployed in cloud environments or orchestrated with Kubernetes

## Troubleshooting

### Package Structure Error

If you encounter an error like this during installation:
```
ValueError: Unable to determine which files to ship inside the wheel using the following heuristics:
The most likely cause of this is that there is no directory that matches the name of your project (fastn).
```

**Quick Fix:**
1. Make sure `pyproject.toml` has the wheel configuration:
```toml
[tool.hatch.build.targets.wheel]
packages = ["."]
```

2. Then install dependencies:
```bash
uv pip install "httpx>=0.28.1" "mcp[cli]>=1.2.0"
```

## Support

- Documentation: https://docs.fastn.ai/ucl-unified-context-layer/about-ucl
- Community: [https://discord.gg/Nvd5p8axU3](https://discord.gg/Nvd5p8axU3)

## License

This project is licensed under the terms included in the [LICENSE](LICENSE) file.
