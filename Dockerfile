# syntax=docker/dockerfile:1.7
# Governed Data Agent: API (default) and MCP server (`data-agent mcp --transport http`).
# The image holds code and configuration only; replicas, dbt artefacts and the catalog index
# are mounted read-only, state (checkpoints, audit log) goes to a writable volume.

FROM python:3.11.9-slim-bookworm AS build
WORKDIR /src
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.11.9-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_AGENT_HOME=/app \
    MLFLOW_DISABLE_AGENT_HINT=1
RUN groupadd --gid 10001 agent && useradd --uid 10001 --gid agent --create-home agent
WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels governed-data-agent && rm -rf /wheels
COPY configs ./configs
COPY golden ./golden
RUN mkdir -p /app/data/state && chown -R agent:agent /app/data
USER agent
EXPOSE 8000 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"
CMD ["data-agent", "serve", "--host", "0.0.0.0", "--port", "8000"]
