# EXPLAIN budget calibration

220 legitimate queries (gold + alternative SQL of the golden set) on the analyst replica.

| | Largest operator, estimated rows | Runtime |
|---|---|---|
| Median | 206,607 | 23.4 ms |
| p95 | 1,033,036 | 158.6 ms |
| Max | 647,498,916 | 268.0 ms |

Thresholds: approval above 1,000,000,000, reject above 100,000,000,000 (largest operator). Legitimate queries that would need approval: 0.

| Expensive query | Largest operator, estimated | Decision | Outcome without the budget |
|---|---|---|---|
| fact x fact cross product | 1,067,163,377,296 | reject | timeout (10 s) (10,030 ms) |
| fact x customer cross product | 9,200,218,616 | approval | finished (2,262 ms) |
| orders x orders cross product | 2,875,962,384 | approval | finished (2,125 ms) |
| fact self-join on invoice | 193,396,770 | allow | finished (1,370 ms) |
| orders x products cross product | 275,165,268 | allow | finished (216 ms) |
