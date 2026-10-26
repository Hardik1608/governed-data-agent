#!/usr/bin/env bash
# Build the image and smoke-test a container, then remove everything this script created.
#   scripts/docker_check.sh                      # log: docker_check.log in the repo root
# On a shared host, set DOCKER_CHECK_LOCK to a lock directory (taken with mkdir, released on
# exit) and MIN_FREE_MB to wait for free memory first, e.g.
#   DOCKER_CHECK_LOCK=~/.heavy_job.lock MIN_FREE_MB=2600 scripts/docker_check.sh
# Steps: a private buildx builder capped at 2 GB builds the image (its cache is removed with
# the builder); checks: non-root uid, CLI, the API starting on a synthetic warehouse (/healthz,
# /readyz, AI-disclosure header, Docker HEALTHCHECK), `docker compose config`, promtool on the
# alert rules. Result: reports/docker/docker_check.md was produced from this script's log.
set -u
cd "$(dirname "$0")/.."
LOCK="${DOCKER_CHECK_LOCK:-}"
MIN_FREE_MB="${MIN_FREE_MB:-0}"
LOG="$PWD/docker_check.log"
IMG=governed-data-agent:ci
BUILDER=gda-builder
PORT="${DOCKER_CHECK_PORT:-18731}"
log() { echo "[$(date -u +%H:%M:%SZ)] $*" | tee -a "$LOG"; }
HAD_BUILDKIT=$(docker images -q moby/buildkit:buildx-stable-1)

if [ -n "$LOCK" ] || [ "$MIN_FREE_MB" -gt 0 ]; then
  log "waiting for ${LOCK:-no lock} and ${MIN_FREE_MB} MB of available memory"
  deadline=$(( $(date +%s) + 10800 ))
  while true; do
    avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
    if [ "$avail" -ge "$MIN_FREE_MB" ] && { [ -z "$LOCK" ] || mkdir "$LOCK" 2> /dev/null; }; then break; fi
    if [ "$(date +%s)" -ge "$deadline" ]; then log "gave up after 3 h"; exit 2; fi
    sleep 10
  done
  if [ -n "$LOCK" ]; then
    printf '{\n  "project": "governed-data-agent",\n  "pid": %s,\n  "host": "%s",\n  "started": "%s",\n  "job": "docker build + smoke test"\n}\n' \
      "$$" "$(hostname)" "$(date -u +%FT%T+00:00)" > "$LOCK/owner.json"
  fi
fi
cleanup() {
  docker rm -f gda-smoke > /dev/null 2>&1
  docker rmi -f "$IMG" > /dev/null 2>&1
  docker buildx rm "$BUILDER" > /dev/null 2>&1
  if [ -z "$HAD_BUILDKIT" ]; then docker rmi moby/buildkit:buildx-stable-1 > /dev/null 2>&1; fi
  if [ -n "$LOCK" ]; then rm -rf "$LOCK"; fi
  log "cleanup done${LOCK:+, lock released}"
}
trap cleanup EXIT

if ! docker buildx create --name "$BUILDER" --driver docker-container \
    --driver-opt memory=2g --driver-opt memory-swap=2g >> "$LOG" 2>&1; then
  log "RESULT builder: FAILED to create a capped builder"; exit 3
fi
log "build start"
start=$(date +%s)
if ! timeout 1800 docker buildx build --builder "$BUILDER" --load -t "$IMG" . >> "$LOG" 2>&1; then
  log "RESULT build: FAILED"; exit 4
fi
log "RESULT build: ok in $(( $(date +%s) - start )) s, image $(docker image inspect "$IMG" --format '{{.Size}}') bytes"

uid=$(docker run --rm --memory 512m "$IMG" id -u 2>> "$LOG")
log "RESULT uid: $uid (expected 10001)"
if docker run --rm --memory 512m "$IMG" data-agent --help > /dev/null 2>> "$LOG"; then
  log "RESULT cli: ok"
else
  log "RESULT cli: FAILED"
fi

docker run -d --name gda-smoke --memory 1g -p "127.0.0.1:$PORT:8000" \
  -e DATA_AGENT_PII_SECRET=smoke-test-only -e DATA_AGENT_EMBEDDINGS=fake -e DATA_AGENT_TRACING=off \
  -e DATA_AGENT_WAREHOUSE=/tmp/mini/warehouse/mini.duckdb -e DATA_AGENT_REPLICA_DIR=/tmp/mini/replicas \
  -e DATA_AGENT_DBT_DIR=/tmp/mini/no-dbt -e DATA_AGENT_INDEX_DB=/tmp/mini/index.duckdb \
  -e DATA_AGENT_STATE_DIR=/tmp/state \
  "$IMG" sh -c "data-agent build-mini --out /tmp/mini && exec data-agent serve --host 0.0.0.0 --port 8000" \
  >> "$LOG" 2>&1
ready=""
for _ in $(seq 1 90); do
  ready=$(curl -sf "http://127.0.0.1:$PORT/readyz" 2> /dev/null) && break
  sleep 2
done
log "RESULT readyz: ${ready:-NOT READY}"
log "RESULT healthz: $(curl -sf "http://127.0.0.1:$PORT/healthz" 2> /dev/null || echo FAILED)"
log "RESULT ai-disclosure header: $(curl -sfI "http://127.0.0.1:$PORT/healthz" 2> /dev/null | tr -d '\r' | grep -i '^x-ai-generated' || echo missing)"
health=starting
for _ in $(seq 1 30); do
  health=$(docker inspect gda-smoke --format '{{.State.Health.Status}}' 2> /dev/null)
  [ "$health" = "healthy" ] && break
  sleep 5
done
log "RESULT docker healthcheck: $health"
docker logs gda-smoke 2>&1 | tail -n 15 >> "$LOG"

mkdir -p data/state
[ -f data/state/tokens.json ] || echo '{"tokens": []}' > data/state/tokens.json
if docker compose config -q >> "$LOG" 2>&1; then log "RESULT compose config: ok"; else log "RESULT compose config: FAILED"; fi
if docker run --rm -v "$PWD/monitoring:/m:ro" --entrypoint promtool prom/prometheus:v3.13.4 \
    check rules /m/alerts.yml >> "$LOG" 2>&1; then
  log "RESULT promtool rules: ok"
else
  log "RESULT promtool rules: FAILED"
fi
log "done"
