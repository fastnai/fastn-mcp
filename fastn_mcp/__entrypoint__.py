"""Docker entrypoint — reads env vars and delegates to the fastn-mcp CLI."""

import os
import sys


def main() -> None:
    args = ["fastn-mcp"]

    transport = os.environ.get("FASTN_MCP_TRANSPORT", "both").lower()
    if transport == "sse":
        args.append("--sse")
    elif transport == "shttp":
        args.append("--shttp")
    else:  # both / default
        args += ["--sse", "--shttp"]

    args += ["--host", os.environ.get("FASTN_MCP_HOST", "0.0.0.0")]
    args += ["--port", os.environ.get("FASTN_MCP_PORT", "8000")]

    if os.environ.get("FASTN_MCP_NO_AUTH", "").lower() == "true":
        args.append("--no-auth")

    server_url = os.environ.get("FASTN_MCP_SERVER_URL", "")
    if server_url:
        args += ["--server-url", server_url]

    if os.environ.get("FASTN_MCP_VERBOSE", "").lower() == "true":
        args.append("--verbose")

    # Pass through any extra CLI args appended to `docker run`
    args += sys.argv[1:]

    # Replace this process with fastn-mcp
    from fastn_mcp.__main__ import cli
    sys.argv = args
    cli()


if __name__ == "__main__":
    main()
