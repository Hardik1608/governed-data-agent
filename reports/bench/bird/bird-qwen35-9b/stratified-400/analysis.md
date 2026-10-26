# BIRD: agent vs single-shot, paired (400 questions)

| | Single-shot correct | Single-shot wrong |
|---|---|---|
| Agent correct | 156 | 63 |
| Agent wrong | 36 | 145 |

Where the agent won, the single-shot answer was: wrong result 55, SQL error or timeout 8.

Where the agent lost: wrong result (status answered) 26, wrong result (status budget_exceeded) 9, no SQL (status answered) 1.

Agent final statuses: answered 343, budget_exceeded 57.
Model-call budget reached on 57 questions (14.2%); the last executed query was scored and was correct on 9 of them.

| Difficulty | n | Agent EX | Single-shot EX | Agent only | Single-shot only |
|---|---|---|---|---|---|
| simple | 241 | 59.3% | 50.6% | 41 | 20 |
| moderate | 122 | 49.2% | 42.6% | 19 | 11 |
| challenging | 37 | 43.2% | 48.6% | 3 | 5 |

Single-shot EX on this subset 48.0% vs 49.1% on all 1534 questions. Tokens per question: agent 22,137, single-shot 1,097. Model calls per question: agent 5.68, single-shot 1.13; the agent ran 3.07 queries per question. Latency p50 / p95: agent 28.087 s / 86.349 s, single-shot 1.853 s / 8.452 s (4 concurrent requests each).
