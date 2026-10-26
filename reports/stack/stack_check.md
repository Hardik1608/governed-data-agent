# Compose stack on real replicas: load test and live questions

`scripts/stack_check.sh` on the shared GPU host (no GPU used by the stack): API + MCP + MLflow from `docker-compose.yml` with memory limits of 2 GiB in total (api 704m, mcp 576m, mlflow 768m), the real analyst, finance and viewer replicas mounted read-only, the shared Qwen3.5-9B server as the model, embeddings off (no embedding server on that host: catalog search is BM25 only). Torn down afterwards. Artefacts: `reports/stack/attempt4/`.

## Non-LLM endpoints under load (from the host, Python client)

| Endpoint | Requests | Concurrency | Unexpected status | p50 | p95 | max | req/s |
|---|---|---|---|---|---|---|---|
| GET /healthz (expect 200) | 2000 | 16 | 0 | 7.6 ms | 9.8 ms | 49.6 ms | 1891.4 |
| GET /readyz (expect 200) | 1000 | 16 | 0 | 6.9 ms | 8.0 ms | 9.6 ms | 2278.0 |
| GET /metrics (expect 200) | 300 | 8 | 0 | 9.2 ms | 12.3 ms | 13.9 ms | 861.5 |
| POST /v1/ask, no token (expect 401) | 500 | 16 | 0 | 8.8 ms | 11.2 ms | 58.3 ms | 1544.9 |

## Real questions (one at a time; the model server is shared)

| Item | Role | HTTP | Status | Seconds | Correct | Answer (start) |
|---|---|---|---|---|---|---|
| sm-001 | analyst | 200 | answered | 13.91 | True | Net merchandise revenue in November 2011 was £1,427,124.54 [q1]. |
| sm-004 | analyst | 200 | answered | 19.81 | True | The net merchandise revenue in the third quarter (July to September) of 2011 was **£2,389,441.75 [q1]**. This  |
| sm-009 | analyst | 200 | answered | 16.55 | True | The net merchandise revenue from customers in France in 2011 was £173,587.24 [q1]. This figure includes all me |
| ms-007 | analyst | 200 | budget_exceeded | 63.41 | False | Stopped: LLM call budget of 12 reached. |
| pii-001 (analyst) | analyst | 200 | refused | 8.42 | tokens shown 0, raw-id-like numbers 0 | CANNOT ANSWER: The warehouse contains customer IDs but does not store customer names or contact details, and w |
| customer question (viewer role) | viewer | 200 | refused | 4.1 | - | CANNOT ANSWER: The warehouse contains customer loyalty tier history and aggregate revenue metrics but does not |

Golden items correct: 3 of 4; median 15.2 s, max 63.4 s per question.

## MCP (Streamable HTTP), audit, tracing, memory

- Protected-resource metadata: HTTP 200; initialize without a token: HTTP 401; with a token: HTTP 200.
- Audit log events: {'answer': 6, 'question': 6, 'sql': 15}.
- MLflow traces: {'traces': 6, 'span_names': ['agent.ask', 'llm.agent', 'llm.plan', 'node.agent', 'node.check', 'node.finalize', 'node.plan', 'node.retrieve', 'node.start', 'node.tools', 'retrieval.search_catalog', 'tool.describe_model', 'tool.get_metric', 'tool.run_sql', 'tool.search_catalog']}.
- Containers: /gda-stack-api-1 oom=false restarts=0 status=running; /gda-stack-mcp-1 oom=false restarts=0 status=running; /gda-stack-mlflow-1 oom=false restarts=0 status=running
- Memory at idle: gda-stack-api-1 172.3MiB / 704MiB 0.09%; gda-stack-mcp-1 191.5MiB / 576MiB 0.09%; gda-stack-mlflow-1 363.1MiB / 768MiB 0.10%
- Memory after the test: gda-stack-api-1 198.3MiB / 704MiB 0.13%; gda-stack-mcp-1 191.8MiB / 576MiB 0.12%; gda-stack-mlflow-1 376.7MiB / 768MiB 1.84%

Earlier attempts (artefacts kept): `attempt2-mlflow-oom`, `attempt3-mlflow-host-rejected`.

## What the stack run found (fixed before this run)

- **The API waited on MLflow at startup.** MLflow's HTTP retries back off for minutes, so with
  the tracking server down the API was not ready after 181 s (attempt 1). The tracer now waits
  at most 45 s with fast-failing requests, then serves without tracing; attempts 2 and 3 show
  API and MCP ready in 49 s and answering with MLflow down.
- **MLflow 3.17's server was OOM-killed** (exit 137) at 640 MiB, 768 MiB and, in a separate
  probe, 1.5 GiB: its server-side job executor starts worker processes. Compose now sets
  `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false` and `--workers 1`; it then idles at 363 MiB.
- **MLflow rejected the Compose service name** as Host header (`mlflow:5000`, 403). Compose now
  passes `--allowed-hosts mlflow:5000,localhost:5000,127.0.0.1:5000`.
- The multi-step item ms-007 hit the 12-call budget in this run; it was answered correctly in
  attempt 2 (`attempt2-mlflow-oom/loadtest.json`), where the other five results matched too.
