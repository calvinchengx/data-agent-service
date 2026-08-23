# The executor. Python implementation; services/warehouse-query-go/ implements
# the same OpenAPI contract (services/contract/openapi.json).
# Base images come from a pull-through mirror, not Docker Hub directly.
#
# Docker Hub rate-limits anonymous pulls, and a release runner is anonymous.
# That is not hypothetical here: cutting v0.2.0 failed on
# `python:3.12-slim: failed to resolve source metadata` while nothing in the
# repository had changed -- the kind of failure that wastes an afternoon
# because it looks like a code problem and is a quota.
#
# mirror.gcr.io is a cache, not a fork: the manifests it serves for these tags
# are byte-identical to Docker Hub's, checked before switching. Override
# BASE_REGISTRY to pull from anywhere else, including Docker Hub itself.
ARG BASE_REGISTRY=mirror.gcr.io/library
FROM ${BASE_REGISTRY}/python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/
# mssql-python bundles Microsoft's ODBC driver; these are what it needs to load.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates libltdl7 libkrb5-3 libgssapi-krb5-2 \
    && rm -rf /var/lib/apt/lists/*
# Which implementation this image is; see the Go Dockerfile.
LABEL com.calvinx.das.executor="py"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1
# The `keyvault:` resolver is shared with the seeds and the agent rather than
# reimplemented here. One implementation of a syntax an OPERATOR writes into
# configuration matters more than keeping this image to a single directory:
# two resolvers would eventually disagree about what a reference means.
COPY vaultref.py /app/
COPY services/warehouse-query-py/ /app/
EXPOSE 8090
# Declared here rather than in compose: the two executors ship different
# runtimes, so each image is what knows how to probe itself.
HEALTHCHECK --interval=10s --timeout=5s --retries=12 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8090/health',timeout=5).status==200 else 1)"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090", "--no-access-log"]
