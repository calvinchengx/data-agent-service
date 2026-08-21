# Runtime for seeds, e2e, evals and load drivers — runs INSIDE the compose
# network so it reaches dependencies exactly as the service does (and as a
# Container App would reach real Azure).
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/
# mssql-python bundles its own ODBC driver; it needs these to load.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates libltdl7 libkrb5-3 libgssapi-krb5-2 curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1 PYTHONPATH=/app
CMD ["python", "-m", "seed.run"]
