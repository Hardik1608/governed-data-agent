# Governance overheads (golden workload, analyst replica)

220 queries (gold + alternative SQL of the golden set).

| Step | Median | p95 |
|---|---|---|
| Validate (parse, read-only, allowlist, columns, PII tags) | 14.68 ms | 30.15 ms |
| EXPLAIN cost estimate | 5.18 ms | 12.36 ms |
| Execute (read-only replica) | 27.65 ms | 165.17 ms |

Replica builds: masked 11.81 s / 34.4 MB, clear 7.4 s / 42.5 MB, aggregates 0.71 s / 1.8 MB, masked_injection 5.21 s / 34.6 MB.

Model usage in eval run `local-qwen35-9b-v1`: 3.8 calls per trial, 3,594.0 prompt and 127.0 completion tokens per call.
