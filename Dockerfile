# ── Build stage ────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /fastn-mcp

# Install build dependencies
RUN pip install --no-cache-dir hatchling

# Copy project files
COPY pyproject.toml .
COPY README.md .
COPY fastn_mcp/ fastn_mcp/

# Build wheel
RUN pip wheel --no-cache-dir --wheel-dir /wheels .
RUN pip wheel --no-cache-dir --wheel-dir /wheels \
    "mcp[cli]>=1.2.0" \
    "fastn-sdk>=0.2.3" \
    "httpx>=0.28.1" \
    "starlette>=0.27.0" \
    "uvicorn>=0.24.0" \
    "PyJWT[crypto]>=2.8.0"

# ── Runtime stage ─────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL maintainer="Fastn <support@fastn.dev>"
LABEL description="Fastn MCP Server — MCP gateway for 250+ enterprise integrations"
LABEL org.opencontainers.image.source="https://github.com/fastnai/fastn-mcp"

# Create non-root user
RUN groupadd --gid 1000 fastn && \
    useradd --uid 1000 --gid fastn --shell /bin/bash --create-home fastn

WORKDIR /app

# Install wheels from build stage
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# Environment defaults
ENV FASTN_MCP_HOST=0.0.0.0 \
    FASTN_MCP_PORT=8000 \
    FASTN_MCP_TRANSPORT=both \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Health check — hit the OAuth metadata endpoint (always available)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${FASTN_MCP_PORT}/.well-known/oauth-protected-resource')" || exit 1



# Entrypoint script handles env-to-CLI flag translation
COPY --chown=fastn:fastn docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh
RUN ls /app

# Switch to non-root user
USER fastn

ENTRYPOINT ["/app/docker-entrypoint.sh"]
