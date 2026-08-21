# Runtime for seeds, e2e, evals and load drivers — runs INSIDE the compose
# network so it reaches dependencies exactly as the service does (and as a
# Container App would reach real Azure).
# The reference TypeScript MCP client lives here too. A second, INDEPENDENT
# implementation of the protocol is what turns "our client works" into "the
# protocol works" — a Python server that only a Python client can drive passes
# one witness and fails the other. Its dependencies live outside /app because
# the repo is bind-mounted over that path at run time.
FROM node:22-slim AS jsdeps
WORKDIR /opt/mcp-js
RUN npm init -y >/dev/null \
    && npm install --no-fund --no-audit @modelcontextprotocol/sdk \
    && npm ls --depth=0 @modelcontextprotocol/sdk

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=jsdeps /opt/mcp-js /opt/mcp-js
# mssql-python bundles its own ODBC driver; it needs these to load.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates libltdl7 libkrb5-3 libgssapi-krb5-2 curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1 PYTHONPATH=/app
CMD ["python", "-m", "seed.run"]
