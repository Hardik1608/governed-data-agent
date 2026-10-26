#!/usr/bin/env bash
# Run every real-model evaluation in order. Each step is resumable: re-running the script
# skips finished work (per-trial / per-question checkpoints), so it survives power cuts.
#   DATA_AGENT_LLM_ENV_FILE=../_shared/local_llm.env scripts/run_all_evals.sh
# Concurrency is 4 requests per step and the steps run one after another.
set -u
cd "$(dirname "$0")/.."
CLI="${CLI:-.venv/Scripts/data-agent.exe}"
[ -x "$CLI" ] || CLI=".venv/bin/data-agent"
log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

running() {  # is another data-agent process (e.g. a BIRD run started separately) still going?
  if command -v pgrep > /dev/null 2>&1; then pgrep -f "data-agent" > /dev/null 2>&1; return $?; fi
  tasklist 2>/dev/null | grep -qi "data-agent"
}

wait_for_bird_agent() {
  while running; do sleep 60; done
}

step() {
  log "START $*"
  "$CLI" "$@"
  log "END ($?) $*"
}

[ "${SKIP_WAIT:-0}" = "1" ] || wait_for_bird_agent
step bench bird-report --run-name bird-qwen35-9b --total 400 --stratified
step eval run --run-name local-qwen35-9b-v2 --k 4 --config configs/agents/governed-v2.yaml
step eval judge --run local-qwen35-9b-v1
step bench spider2 --mode single_shot --run-name spider2-qwen35-9b
step bench spider2 --mode agent --run-name spider2-qwen35-9b
log "ALL DONE"
