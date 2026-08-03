# generate-data-mcp — community-built image for the Docker MCP catalog
# (registry-research.md §3). stdio MCP server: no HTTP port to expose, so the
# HEALTHCHECK below only verifies the package imports cleanly, not that the
# running stdio process is actively serving — Docker/MCP clients that need a
# stronger liveness signal should wrap this with a supervisor.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY generate_data_mcp ./generate_data_mcp

RUN pip install --no-cache-dir .

RUN useradd --create-home --shell /usr/sbin/nologin app
USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import generate_data_mcp.server" || exit 1

ENTRYPOINT ["generate-data-mcp"]
