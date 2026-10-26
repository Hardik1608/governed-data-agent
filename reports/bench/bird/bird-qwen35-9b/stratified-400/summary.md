# BIRD dev: `bird-qwen35-9b`

Model `local-agent`, temperature 0.0. Questions in this run's set: 400 (stratified subset of the 1,534; same difficulty x database mix). Each mode's n is in the table.

| Mode | n | EX | simple | moderate | challenging | tokens/question | model calls/question | p50 / p95 latency |
|---|---|---|---|---|---|---|---|---|
| single_shot | 1534 | 49.1% | 55.0% | 40.5% | 39.3% | 1,085.9 | 1.12 | 1.894 s / 8.963 s |
| agent | 400 | 54.8% | 59.3% | 49.2% | 43.2% | 22,136.8 | 5.68 | 28.087 s / 86.349 s |

Paired on the 400 questions both modes ran: agent 54.8% vs single-shot 48.0%, difference 6.8% (95% bootstrap CI 2.0% to 11.2%); agent-only correct 63, single-shot-only correct 36.
