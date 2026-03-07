# ── Build stage ────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

WORKDIR /fastn-mcp

# Install build dependencies
RUN pip install --no-cache-dir hatchling

# Copy project files
COPY pyproject.toml .
COPY README.md .
COPY fastn_mcp/ fastn_mcp/

# Install all dependencies directly into site-packages
RUN pip install --no-cache-dir \
    "mcp>=1.2.0" \
    "fastn-ai>=0.3.6" \
    . && \
    pip uninstall -y pip

# ── Runtime stage ─────────────────────────────────────────────────────
FROM python:3.13-slim

LABEL maintainer="Fastn <support@fastn.ai>"
LABEL description="Fastn MCP Server — MCP gateway for 250+ enterprise integrations"
LABEL org.opencontainers.image.source="https://github.com/fastnai/fastn-mcp"

# Create non-root user
RUN groupadd --gid 1000 fastn && \
    useradd --uid 1000 --gid fastn --shell /bin/bash --create-home fastn

WORKDIR /app

# Copy installed packages from build stage
COPY --from=builder /usr/local/lib/python3.13 /usr/local/lib/python3.13
COPY --from=builder /usr/local/bin/fastn-mcp-docker /usr/local/bin/fastn-mcp-docker

# Environment defaults
ENV FASTN_MCP_HOST=0.0.0.0 \
    FASTN_MCP_PORT=8000 \
    FASTN_MCP_TRANSPORT=both \
    PYTHONUNBUFFERED=1

EXPOSE 8000

USER fastn

ENTRYPOINT ["fastn-mcp-docker"]
