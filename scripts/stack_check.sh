#!/usr/bin/env bash
# Run the Compose stack (API + MCP + MLflow) once on real replicas, load-test it, tear it down.
#   scripts/stack_check.sh            (from the repo root, with data/ built and .env present)
# Needs: data/replicas, data/dbt, data/index; data/state/tokens.json holding an analyst and a
# viewer token whose plain values are in $ANALYST_TOKEN / $VIEWER_TOKEN; a reachable model server.
# On a shared host set STACK_CHECK_LOCK (a lock directory taken with mkdir) and MIN_FREE_MB.
# Memory: api 704m + mcp 576m + mlflow 768m = 2 GiB (docker-compose.limits.yml), image built
# with a private buildx builder capped at 2 GB. Everything it creates is removed at the end.
set -u
cd "$(dirname "$0")/.."
LOCK="${STACK_CHECK_LOCK:-}"
MIN_FREE_MB="${MIN_FREE_MB:-0}"
OUT="${STACK_CHECK_OUT:-$PWD/stack_check}"
LOG="$OUT/stack_check.log"
IMG=governed-data-agent:local
MLFLOW_IMG=ghcr.io/mlflow/mlflow:v3.17.1
BUILDER=gda-stack-builder
COMPOSE=(docker compose -p gda-stack -f docker-compose.yml -f "$OUT/docker-compose.limits.yml")
mkdir -p "$OUT"
log() { echo "[$(date -u +%H:%M:%SZ)] $*" | tee -a "$LOG"; }
HAD_BUILDKIT=$(docker images -q moby/buildkit:buildx-stable-1)
HAD_MLFLOW=$(docker images -q "$MLFLOW_IMG")

cat > "$OUT/docker-compose.limits.yml" << 'YAML'
services:
  api:
    mem_limit: 704m
    memswap_limit: 704m
  mcp:
    mem_limit: 576m
    memswap_limit: 576m
  mlflow:
    mem_limit: 768m
    memswap_limit: 768m
YAML

if [ -n "$LOCK" ] || [ "$MIN_FREE_MB" -gt 0 ]; then
  log "waiting for ${LOCK:-no lock} and ${MIN_FREE_MB} MB of available memory"
  deadline=$(( $(date +%s) + 14400 ))
  while true; do
    avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
    if [ "$avail" -ge "$MIN_FREE_MB" ] && { [ -z "$LOCK" ] || mkdir "$LOCK" 2> /dev/null; }; then break; fi
    if [ "$(date +%s)" -ge "$deadline" ]; then log "gave up after 4 h"; exit 2; fi
    sleep 10
  done
  if [ -n "$LOCK" ]; then
    printf '{\n  "project": "governed-data-agent",\n  "pid": %s,\n  "host": "%s",\n  "started": "%s",\n  "job": "compose stack (api+mcp+mlflow, 2 GiB) + load test, about 15 min"\n}\n' \
      "$$" "$(hostname)" "$(date -u +%FT%T+00:00)" > "$LOCK/owner.json"
  fi
  log "lock taken (MemAvailable ${avail} MB)"
fi
cleanup() {
  "${COMPOSE[@]}" down -v --remove-orphans >> "$LOG" 2>&1
  docker rmi -f "$IMG" > /dev/null 2>&1
  docker buildx rm "$BUILDER" > /dev/null 2>&1
  if [ -z "$HAD_BUILDKIT" ]; then docker rmi moby/buildkit:buildx-stable-1 > /dev/null 2>&1; fi
  if [ -z "$HAD_MLFLOW" ]; then docker rmi "$MLFLOW_IMG" > /dev/null 2>&1; fi
  rm -f .env data/state/tokens.json
  if [ -n "$LOCK" ]; then rm -rf "$LOCK"; fi
  log "cleanup done${LOCK:+, lock released}"
}
trap cleanup EXIT

docker buildx create --name "$BUILDER" --driver docker-container \
  --driver-opt memory=2g --driver-opt memory-swap=2g >> "$LOG" 2>&1 || { log "RESULT builder: FAILED"; exit 3; }
start=$(date +%s)
timeout 1800 docker buildx build --builder "$BUILDER" --load -t "$IMG" . >> "$LOG" 2>&1 || { log "RESULT build: FAILED"; exit 4; }
docker buildx rm "$BUILDER" > /dev/null 2>&1
log "RESULT build: ok in $(( $(date +%s) - start )) s"
docker pull -q "$MLFLOW_IMG" >> "$LOG" 2>&1 || { log "RESULT mlflow image: FAILED"; exit 5; }

start=$(date +%s)
"${COMPOSE[@]}" up -d --no-build api mcp mlflow >> "$LOG" 2>&1 || { log "RESULT compose up: FAILED"; exit 6; }
ready=""
for _ in $(seq 1 90); do
  ready=$(curl -sf http://127.0.0.1:8000/readyz 2> /dev/null) && break
  sleep 2
done
log "RESULT readyz after $(( $(date +%s) - start )) s: ${ready:-NOT READY}"
[ -n "$ready" ] || { "${COMPOSE[@]}" logs --tail 60 >> "$LOG" 2>&1; exit 7; }
for _ in $(seq 1 30); do curl -s -o /dev/null http://127.0.0.1:8765/.well-known/oauth-protected-resource/mcp && break; sleep 2; done
docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.CPUPerc}}' $("${COMPOSE[@]}" ps -q) > "$OUT/stats_idle.txt"

python3 scripts/stack_loadtest.py --api http://127.0.0.1:8000 --mcp http://127.0.0.1:8765 \
  --analyst-token "$ANALYST_TOKEN" --viewer-token "$VIEWER_TOKEN" --out "$OUT/loadtest.json" >> "$LOG" 2>&1 \
  && log "RESULT load test: done" || log "RESULT load test: FAILED"
docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.CPUPerc}}' $("${COMPOSE[@]}" ps -q) > "$OUT/stats_after.txt"
"${COMPOSE[@]}" ps --format '{{.Service}} {{.Status}}' > "$OUT/ps.txt"

"${COMPOSE[@]}" exec -T api python -c "
import sqlite3
con = sqlite3.connect('file:/app/data/state/audit.sqlite?mode=ro', uri=True)
print(dict(con.execute('SELECT kind, count(*) FROM audit_events GROUP BY kind').fetchall()))
" > "$OUT/audit_counts.txt" 2>> "$LOG"
"${COMPOSE[@]}" exec -T mlflow python -c "
import mlflow
c = mlflow.MlflowClient('http://127.0.0.1:5000')
e = c.get_experiment_by_name('governed-data-agent')
try:
    traces = c.search_traces(experiment_ids=[e.experiment_id], max_results=100)
except TypeError:
    traces = c.search_traces(locations=[e.experiment_id], max_results=100)
names = sorted({s.name for t in traces for s in t.data.spans})
print({'traces': len(traces), 'span_names': names[:20]})
" > "$OUT/mlflow_traces.txt" 2>> "$LOG"
"${COMPOSE[@]}" logs --no-color api mcp > "$OUT/service_logs.txt" 2>&1
"${COMPOSE[@]}" logs --no-color --tail 60 mlflow > "$OUT/mlflow_logs.txt" 2>&1
docker inspect -f '{{.Name}} oom={{.State.OOMKilled}} restarts={{.RestartCount}} status={{.State.Status}}' $("${COMPOSE[@]}" ps -aq) > "$OUT/inspect.txt" 2>&1
log "RESULT audit: $(cat "$OUT/audit_counts.txt")"
log "RESULT mlflow: $(cat "$OUT/mlflow_traces.txt")"
log "done"
