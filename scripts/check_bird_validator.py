"""Safety-layer component check on BIRD dev gold SQL (as v1 did on Spider).

For every gold query: does the validator (SQLite dialect, schema allowlist) accept it, and
does the re-serialised safe_sql return the same result set as the original?
Also reports the size of the single-shot prompt per database.
Output: reports/bench/bird/validator_check.json
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_agent.bench.bird import BIRD_DIR, load_bird, result_set  # noqa: E402
from data_agent.bench.single_shot import build_messages  # noqa: E402
from data_agent.bench.sqlite_db import SqliteGuard  # noqa: E402
from data_agent.safety.executor import ExecutionError  # noqa: E402


def main() -> int:
    items, dbs, tied = load_bird(ROOT / BIRD_DIR)
    guards = {k: SqliteGuard(v) for k, v in dbs.items()}
    rejected, codes, same, differ, gold_failed = [], Counter(), 0, [], 0
    for it in items:
        g = guards[it["db_id"]]
        d = g.check(it["SQL"])
        if d.decision != "allow":
            rejected.append(
                {
                    "question_id": it["question_id"],
                    "codes": d.validation.codes,
                    "reason": d.reasons[0][:200],
                }
            )
            codes.update(d.validation.codes)
            continue
        try:
            raw = result_set(dbs[it["db_id"]], it["SQL"])
        except ExecutionError:
            gold_failed += 1
            continue
        try:
            safe = result_set(dbs[it["db_id"]], d.safe_sql)
        except ExecutionError as e:
            differ.append({"question_id": it["question_id"], "error": str(e)[:200]})
            continue
        if raw == safe:
            same += 1
        else:
            differ.append({"question_id": it["question_id"], "error": "different result"})
    prompt_chars = {
        db_id: len(build_messages(db, "q", "e")[1]["content"]) for db_id, db in dbs.items()
    }
    report = {
        "gold_queries": len(items),
        "validator_rejections": len(rejected),
        "false_rejection_rate": round(len(rejected) / len(items), 4),
        "rejection_codes": dict(codes),
        "rejected": rejected[:20],
        "gold_queries_that_fail_to_run": gold_failed,
        "safe_sql_same_result": same,
        "safe_sql_different": len(differ),
        "safe_sql_differences": differ[:20],
        "single_shot_prompt_chars_by_db": prompt_chars,
        "single_shot_prompt_tokens_estimate_max": max(prompt_chars.values()) // 3,
    }
    out = ROOT / "reports" / "bench" / "bird"
    out.mkdir(parents=True, exist_ok=True)
    (out / "validator_check.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in ("rejected", "safe_sql_differences")},
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
