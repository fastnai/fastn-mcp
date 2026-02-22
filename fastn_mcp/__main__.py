"""Entry point for running the Fastn MCP server: python -m fastn_mcp

Usage:
    python -m fastn_mcp --sse --shttp --port 8000       # SSE + Streamable HTTP
    python -m fastn_mcp --stdio                          # stdio (Claude Desktop)
    python -m fastn_mcp --stdio --mode ucl               # UCL tools only
    python -m fastn_mcp --sse --port 8000 --no-auth      # no OAuth (testing)
    python -m fastn_mcp --sse --server-url https://...   # explicit public URL

Endpoints (mode via URL path):
    POST /shttp                                all tools
    POST /shttp/ucl                            UCL tools only
    POST /shttp/ucl/{project_id}               UCL + pre-set project
    POST /shttp/ucl/{project_id}/{skill_id}    UCL + pre-set project and skill
    GET  /sse, /sse/ucl, ...                   same pattern for SSE
"""

import argparse
import asyncio
import logging
import os

from fastn_mcp.server import main


def cli():
    parser = argparse.ArgumentParser(
        prog="fastn-mcp",
        description="Fastn MCP Server — connector tools for AI agent platforms",
    )

    # Transport flags
    parser.add_argument(
        "--stdio",
        action="store_true",
        default=False,
        help="Use stdio transport (for local pipe-based clients like Claude Desktop)",
    )
    parser.add_argument(
        "--sse",
        action="store_true",
        default=False,
        help="Enable SSE transport (GET /sse + POST /messages/)",
    )
    parser.add_argument(
        "--shttp",
        action="store_true",
        default=False,
        help="Enable Streamable HTTP transport (POST /shttp)",
    )

    # Server options
    parser.add_argument(
        "--host",
        default=os.environ.get("FASTN_MCP_HOST", "0.0.0.0"),
        help="Bind address (default: 0.0.0.0, env: FASTN_MCP_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("FASTN_MCP_PORT", "8000")),
        help="Port (default: 8000, env: FASTN_MCP_PORT)",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        default=os.environ.get("FASTN_MCP_NO_AUTH", "").lower() in ("true", "1", "yes"),
        help="Disable OAuth authentication (for local testing, env: FASTN_MCP_NO_AUTH)",
    )
    parser.add_argument(
        "--server-url",
        default=os.environ.get("FASTN_MCP_SERVER_URL"),
        help="Public URL of this server (for OAuth metadata, env: FASTN_MCP_SERVER_URL)",
    )
    parser.add_argument(
        "--mode",
        choices=["agent", "ucl"],
        default="agent",
        help='Tool mode for stdio: "agent" (all tools) or "ucl" '
             '(UCL tools only). HTTP transports use URL path '
             '(/shttp/ucl, /sse/ucl).',
    )
    parser.add_argument(
        "--project",
        default=None,
        metavar="PROJECT_ID",
        help="Pre-configure the project ID for stdio transport. "
             "For HTTP transports, include project in URL path "
             "(/shttp/ucl/{project_id}).",
    )
    parser.add_argument(
        "--skill",
        default=None,
        metavar="SKILL_ID",
        help="Pre-configure the skill ID for stdio transport. "
             "For HTTP transports, include skill in URL path "
             "(/shttp/ucl/{project_id}/{skill_id}).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=os.environ.get("FASTN_MCP_VERBOSE", "").lower() in ("true", "1", "yes"),
        help="Enable verbose logging (env: FASTN_MCP_VERBOSE)",
    )
    args = parser.parse_args()

    # Resolve transport mode
    if args.stdio:
        transport = "stdio"
    elif args.sse and not args.shttp:
        transport = "sse-only"
    elif args.shttp and not args.sse:
        transport = "shttp-only"
    else:
        # Default: both SSE + Streamable HTTP (also covers --sse --shttp)
        transport = "sse"

    # ── Logging setup ────────────────────────────────────────────────────
    level = logging.DEBUG if args.verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    datefmt = "%H:%M:%S"

    # Configure our logger — propagate=False prevents duplicate output
    # (our handler + root logger's handler from uvicorn)
    fastn_logger = logging.getLogger("fastn-mcp")
    fastn_logger.setLevel(level)
    fastn_logger.propagate = False
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    fastn_logger.addHandler(handler)

    # In verbose mode, also show fastn-mcp.auth logs
    auth_logger = logging.getLogger("fastn-mcp.auth")
    auth_logger.setLevel(level)

    # Set module-level verbose flag so SDK client gets verbose=True
    import fastn_mcp.server as _server_module
    _server_module._verbose = args.verbose

    asyncio.run(main(
        transport=transport,
        host=args.host,
        port=args.port,
        auth_enabled=not args.no_auth,
        server_url=args.server_url,
        mode=args.mode,
        project_id=args.project,
        skill_id=args.skill,
    ))


cli()
