# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-20

First release. Every number below comes from a run saved under `reports/`.

### Added
- **Agent**: LangGraph state graph (plan, clarify, retrieve, agent, tools, check, finalize)
  with a SQLite checkpointer and `interrupt()` for over-budget queries, clear PII, competing
  metric definitions and clarifying questions. Six tools: `search_catalog` (BM25 +
  nomic-embed-text, reciprocal rank fusion), `describe_model`, `get_metric`, `run_sql`,
  `make_chart` (Vega-Lite, inline data only), `ask_user`.
- **Safety**: the text-to-SQL v1 four-stage validator ported to DuckDB (and SQLite for the
  benchmarks); read-only replicas with external access off and the configuration locked;
  `EXPLAIN` cost budget calibrated on 220 golden queries; statement timeout; row cap;
  per-question budgets on model calls, tokens, active time and queries.
- **Governance**: per-role replicas (`viewer`, `analyst`, `finance`) with keyed-hash customer
  tokens; the re-identifiable dbt surrogate keys are re-keyed; egress filter; append-only audit
  log; EU AI Act disclosure on every API response.
- **Traceability**: every number in an answer is matched to a query result cell; numbers that
  stay untraced after two repairs are returned in `untraced` and logged.
- **Interfaces**: FastAPI (bearer tokens, rate limit, idempotency keys, Prometheus metrics),
  MCP server (protocol 2026-07-28) over stdio and Streamable HTTP as an OAuth resource server,
  with multi-round-trip elicitation; `data-agent token` to issue tokens (only hashes stored).
- **Observability**: MLflow Tracing with OpenTelemetry GenAI attributes; Prometheus alert rules
  and a runbook.
- **Evaluation**: a 140-item golden set (gold SQL plus an independent second formulation,
  frozen expected values); graders, tau-bench pass^k with bootstrap CIs, resumable harness with
  regrade; oracle, naive and compromised fake models; LLM judge with a blind human-labelling
  sheet; BIRD dev and Spider2-lite adapters with the v1 single-shot baseline.
- **Release**: agent configurations registered in MLflow (LoggedModel, champion / challenger
  aliases), gate rules in `configs/release_gate.yaml`, shadow traffic for a challenger.
- **Delivery**: Dockerfile (multi-stage, non-root, health check), Docker Compose (API, MCP,
  MLflow, Prometheus), GitHub Actions (lint, tests, golden set, smoke eval + gate, red team,
  dependency audit, image build), `scripts/docker_check.sh` and `scripts/stack_check.sh`.

### Results at release
- Golden set, Qwen3.5-9B int4: agent v1 pass^1 70.2%, pass^4 54.3%; 0 unsafe statements and 0
  raw customer identifiers in 1,120 trials (v1 and v2). v2 was rejected by the release gate.
- Execution accuracy against the v1 single-shot baseline, same model: BIRD dev (all 1,534)
  55.9% vs 49.1% (+6.8 points, 95% CI +4.3 to +9.4); Spider2-lite (135 SQLite tasks) 20.7% vs
  4.4% (+16.3 points). Compose stack on real replicas: see `reports/stack/stack_check.md`.

### Fixed during evaluation
- Grader: a percentage shown with at least one decimal now matches an expected share that
  rounds to it; both golden runs were re-graded (original grades kept per trial).
- Validator: SQLite CTEs that read themselves without `RECURSIVE` were rejected as unknown
  columns.
- Benchmark adapter: Spider2-lite questions with reference documents exceeded the 2,000
  character input limit; the limit is now a service argument.
- Reports are written as UTF-8 on every platform.
- Compose stack (found by `scripts/stack_check.sh` on real replicas): the API no longer blocks at
  startup while MLflow is unreachable (bounded 45 s wait, then no tracing); MLflow runs with
  `--workers 1`, `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false` (its job executor was OOM-killed at
  1.5 GiB) and `--allowed-hosts` for the Compose service name.
