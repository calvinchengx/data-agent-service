# The executor. Python implementation; services/warehouse-query-go/ implements
# the same OpenAPI contract (services/contract/openapi.json).
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/
# mssql-python bundles Microsoft's ODBC driver; these are what it needs to load.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates libltdl7 libkrb5-3 libgssapi-krb5-2 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1
COPY services/warehouse-query-py/ /app/
EXPOSE 8090
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090", "--no-access-log"]
