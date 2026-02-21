#!/bin/bash
set -e

# Build CLI args from environment variables
ARGS=""

# Transport mode
case "${FASTN_MCP_TRANSPORT:-both}" in
    sse)   ARGS="--sse" ;;
    shttp) ARGS="--shttp" ;;
    both)  ARGS="--sse --shttp" ;;
    *)     ARGS="--sse --shttp" ;;
esac

# Host and port
ARGS="$ARGS --host ${FASTN_MCP_HOST:-0.0.0.0}"
ARGS="$ARGS --port ${FASTN_MCP_PORT:-8000}"

# Optional: disable auth
if [ "${FASTN_MCP_NO_AUTH}" = "true" ]; then
    ARGS="$ARGS --no-auth"
fi

# Optional: server URL for OAuth metadata
if [ -n "${FASTN_MCP_SERVER_URL}" ]; then
    ARGS="$ARGS --server-url ${FASTN_MCP_SERVER_URL}"
fi

# Optional: verbose logging
if [ "${FASTN_MCP_VERBOSE}" = "true" ]; then
    ARGS="$ARGS --verbose"
fi

exec fastn-mcp $ARGS "$@"
