# BIRD: agent vs single-shot, paired (1534 questions)

| | Single-shot correct | Single-shot wrong |
|---|---|---|
| Agent correct | 604 | 254 |
| Agent wrong | 150 | 526 |

Where the agent won, the single-shot answer was: wrong result 219, SQL error or timeout 35.

Where the agent lost: wrong result (status answered) 111, wrong result (status budget_exceeded) 38, no SQL (status answered) 1.

Agent final statuses: answered 1314, budget_exceeded 220.
Model-call budget reached on 220 questions (14.3%); the last executed query was scored and was correct on 26 of them.

| Difficulty | n | Agent EX | Single-shot EX | Agent only | Single-shot only |
|---|---|---|---|---|---|
| simple | 925 | 61.5% | 55.0% | 149 | 89 |
| moderate | 464 | 50.0% | 40.5% | 85 | 41 |
| challenging | 145 | 39.3% | 39.3% | 20 | 20 |

Single-shot EX on this subset 49.1% vs 49.1% on all 1534 questions. Tokens per question: agent 21,808, single-shot 1,086. Model calls per question: agent 5.6, single-shot 1.12; the agent ran 3.02 queries per question. Latency p50 / p95: agent 29.743 s / 101.294 s, single-shot 1.894 s / 8.963 s (wall clock per question; concurrency and model-server load varied between runs).
