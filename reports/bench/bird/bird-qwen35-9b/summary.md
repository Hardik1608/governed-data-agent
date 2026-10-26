# BIRD dev: `bird-qwen35-9b`

Model `local-agent`, temperature 0.0. Questions in this run's set: 1534. Each mode's n is in the table.

| Mode | n | EX | simple | moderate | challenging | tokens/question | model calls/question | p50 / p95 latency |
|---|---|---|---|---|---|---|---|---|
| single_shot | 1534 | 49.1% | 55.0% | 40.5% | 39.3% | 1,085.9 | 1.12 | 1.894 s / 8.963 s |
| agent | 1534 | 55.9% | 61.5% | 50.0% | 39.3% | 21,807.8 | 5.6 | 29.743 s / 101.294 s |

Paired on the 1534 questions both modes ran: agent 55.9% vs single-shot 49.1%, difference 6.8% (95% bootstrap CI 4.3% to 9.4%); agent-only correct 254, single-shot-only correct 150.
