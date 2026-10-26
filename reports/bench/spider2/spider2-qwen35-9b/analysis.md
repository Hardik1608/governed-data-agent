# Spider2-lite: where each mode fails (`spider2-qwen35-9b`)

Databases: 30.

**single_shot** (135 tasks, 6 correct): wrong result 68, rejected by the validator 36, CANNOT_ANSWER 18, SQL error or timeout 7, correct 6. Rejected by the validator: 36, of which SQLite itself also refuses 36. Model-call budget reached: 0 (still correct: 0). Latency p50 / p95: 6.784 s / 46.583 s.

**agent** (135 tasks, 28 correct): wrong result 105, correct 28, no SQL (gave up or budget) 2. Rejected by the validator: 0, of which SQLite itself also refuses 0. Model-call budget reached: 81 (still correct: 6). Latency p50 / p95: 87.986 s / 227.797 s.

Paired on 135 tasks: both correct 4; agent only 24 (local007, local020, local021, local023, local028, local030, local035, local038, local039, local040, local058, local068, local130, local133, local193, local197, local198, local199, local202, local221, local244, local274, local283, local310); single-shot only 2 (local131, local195).
