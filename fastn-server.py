from typing import Any, Dict
import httpx
import asyncio
import json
import logging
import argparse
import re
import sys
from mcp.server.fastmcp import FastMCP

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Parse command-line arguments
parser = argparse.ArgumentParser(description="Run MCP server with specified API credentials and use case.")
parser.add_argument("--api_key", help="API key for authentication.")
parser.add_argument("--space_id", required=True, help="Space ID for the target environment.")
parser.add_argument("--tenant_id", help="Tenant ID for the target environment.")
parser.add_argument("--auth_token", help="Auth token for the target environment.")
parser.add_argument("--agent_id", help="Optional agent ID for filtering tools. If not provided, space_id will be used as agent_id.")
args = parser.parse_args()

# Validate arguments
if not args.api_key and not (args.tenant_id and args.auth_token):
    parser.error("Either --api_key OR both --tenant_id and --auth_token must be provided")

# Initialize FastMCP server
mcp = FastMCP("fastn")

# API Endpoints
GET_TOOLS_URL = "https://live.fastn.ai/api/ucl/getTools"
EXECUTE_TOOL_URL = "https://live.fastn.ai/api/ucl/executeTool"

# Common headers
HEADERS = {
    "Content-Type": "application/json",
    "x-fastn-space-id": args.space_id,
    "stage": "LIVE",
    "x-fastn-custom-auth": "true"
}

# Add agent ID to headers - use space_id as default if agent_id not provided
agent_id = args.agent_id if args.agent_id else args.space_id
HEADERS["x-fastn-space-agent-id"] = agent_id

# Log the agent ID being used
if args.agent_id:
    logging.info(f"Using provided agent ID: {agent_id}")
else:
    logging.info(f"No agent ID provided, using space_id as agent_id: {agent_id}")

# Add authentication headers based on provided credentials
if args.api_key:
    # API key authentication
    HEADERS["x-fastn-api-key"] = args.api_key
    HEADERS["x-fastn-space-tenantid"] = ""
else:
    # Tenant authentication
    auth_token = args.auth_token
    if auth_token and not auth_token.startswith('Bearer '):
        auth_token = "Bearer " + auth_token
    
    HEADERS["x-fastn-api-key"] = auth_token.replace("Bearer ", "")
    HEADERS["x-fastn-space-tenantid"] = args.tenant_id
    HEADERS["authorization"] = auth_token

async def fetch_tools() -> list:
    """Fetch tool definitions from the getTools API endpoint."""
    data = {
        "input": {
            "spaceId" : args.space_id
        }
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(GET_TOOLS_URL, headers=HEADERS, json=data)
            response.raise_for_status()
            tools = response.json()
            logging.info(f"Fetched {len(tools)} tools.")
            return tools
        except httpx.HTTPStatusError as e:
            logging.error(f"HTTP error: {e.response.status_code} - {e.response.text}")
        except httpx.RequestError as e:
            logging.error(f"Request error: {e}")
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
    return []

async def execute_tool(action_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool by calling the executeTool API."""
    data = {"input": {"actionId": action_id, "parameters": parameters}}
    logging.info(f"Executing tool with parameters: {json.dumps(parameters)}")
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(EXECUTE_TOOL_URL, headers=HEADERS, json=data)
            response.raise_for_status()
            result = response.json()
            return json.dumps(result)
        except httpx.HTTPStatusError as e:
            logging.error(f"Execution failed: {e.response.status_code} - {e.response.text}")
            return json.dumps({"error": f"HTTP error: {e.response.status_code}"})
        except Exception as e:
            logging.error(f"Unexpected execution error: {e}")
            return json.dumps({"error": str(e)})

def validate_tool_name(name: str) -> str:
    """
    Validate and sanitize tool name to match pattern '^[a-zA-Z0-9_-]{1,64}$'.
    Returns sanitized name or raises ValueError if name can't be sanitized.
    """
    # Check if name already matches pattern
    if re.match(r'^[a-zA-Z0-9_-]{1,64}$', name):
        return name
    
    # Try to sanitize name
    sanitized = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
    
    # Truncate if too long
    if len(sanitized) > 64:
        sanitized = sanitized[:64]
    
    # Ensure it's not empty
    if not sanitized:
        sanitized = "tool_" + str(hash(name))[:10]
    
    logging.warning(f"Tool name sanitized: '{name}' → '{sanitized}'")
    return sanitized

def register_tool(tool_def: Dict[str, Any]):
    """Dynamically create and register a tool based on the tool definition."""
    try:
        action_id = tool_def["actionId"]
        function_info = tool_def["function"]
        original_name = function_info["name"]
        
        try:
            # Validate and sanitize the function name
            function_name = validate_tool_name(original_name)
        except ValueError as e:
            logging.error(f"Invalid tool name '{original_name}': {e}. Skipping tool.")
            return
            
        parameters_schema = function_info.get("parameters", {})
        description = function_info.get("description", "")

        # Define the async function with the correct signature for direct tool registration
        @mcp.tool(name=function_name, description=description)
        async def dynamic_tool(params_obj = parameters_schema) -> str:
            """Dynamically created tool function."""
            try:
                logging.info(f"Tool {function_name} called with params: {params_obj}")
                return await execute_tool(action_id, params_obj)
            except Exception as e:
                logging.error(f"Error in {function_name}: {e}")
                return json.dumps({"error": str(e)})

        # Rename the function to match the tool name for better logging
        dynamic_tool.__name__ = function_name
        
        logging.info(f"Registered tool: {function_name}")
        logging.info(f"Description: {description}")
    except KeyError as e:
        logging.error(f"Invalid tool definition missing key: {e}")
    except Exception as e:
        logging.error(f"Error registering tool: {e}")

def initialize_tools():
    """Fetch tools and register them before running MCP."""
    tools_data = asyncio.run(fetch_tools())
    for tool_def in tools_data:
        register_tool(tool_def)

if __name__ == "__main__":
    initialize_tools()
    mcp.run(transport='stdio')