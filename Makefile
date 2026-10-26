# Thin wrappers around the `data-agent` CLI (Linux/macOS; on Windows run the commands directly
# with .venv/Scripts/...). Each target is one documented step.
PY ?= .venv/bin/python
CLI ?= .venv/bin/data-agent
RUN ?= local-v1
K ?= 4

.PHONY: venv data replicas index golden-freeze test lint format smoke eval judge label-sheet gate-register gate-decide bird-baseline bird-agent serve mcp docker up

venv:
	python3.11 -m venv .venv && $(PY) -m pip install -r requirements.txt -r requirements-dev.txt && $(PY) -m pip install --no-deps -e .

data:            ## copy the warehouse + dbt artefacts from sales-analytics-platform into data/
	$(PY) scripts/make_data.py

replicas:        ## per-role read-only replicas + PII fingerprints + the injection TEST copy
	$(CLI) build-replicas --injections golden/v1/injection.yaml

index:           ## embed the catalog with Ollama nomic-embed-text
	$(CLI) build-index

golden-freeze:   ## run gold + alternative SQL, check agreement, write golden/v1/expected.json
	$(CLI) golden freeze

test:
	$(PY) -m pytest -q

lint:
	.venv/bin/ruff check src tests scripts && .venv/bin/ruff format --check src tests scripts

format:
	.venv/bin/ruff format src tests scripts && .venv/bin/ruff check --fix src tests scripts

smoke:           ## CI smoke eval: oracle fake model on the synthetic warehouse
	$(CLI) build-mini --out data/mini
	DATA_AGENT_WAREHOUSE=data/mini/warehouse/mini.duckdb DATA_AGENT_REPLICA_DIR=data/mini/replicas \
	DATA_AGENT_DBT_DIR=data/mini/no-dbt DATA_AGENT_EMBEDDINGS=fake \
	$(CLI) eval run --run-name ci-smoke --llm oracle --k 1 --expected recompute --out reports/ci-smoke --fresh
	$(CLI) gate check-smoke --summary reports/ci-smoke/summary.json

eval:            ## golden set, k trials, resumable (DATA_AGENT_LLM_* or DATA_AGENT_LLM_ENV_FILE)
	$(CLI) eval run --run-name $(RUN) --k $(K)

judge:
	$(CLI) eval judge --run $(RUN)

label-sheet:
	$(CLI) eval label-sheet --run $(RUN)

gate-register:
	$(CLI) gate register --config configs/agents/governed-v1.yaml --eval reports/eval/$(RUN)

gate-decide:
	$(CLI) gate decide --version $(VERSION)

bird-baseline:
	$(CLI) bench bird --mode single_shot --run-name bird-$(RUN)

bird-agent:
	$(CLI) bench bird --mode agent --run-name bird-$(RUN)

serve:
	$(CLI) serve

mcp:
	$(CLI) mcp --transport stdio

docker:
	docker build -t governed-data-agent:local .

up:
	docker compose up -d --build
