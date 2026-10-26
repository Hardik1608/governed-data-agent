"""Scorer check for the Spider2-lite SQLite subset: does the published gold SQL score as correct?

Spider2-lite publishes gold SQL for only some tasks (gold/sql/<instance_id>.sql); every task has
gold result CSVs (gold/exec_result). For each local task that has gold SQL, run it read-only on
the task's SQLite file and score its result with the same port of `compare_pandas_table` the
benchmark runs use. A task that fails here is either a scorer bug or a dataset artefact (gold
CSV computed on a different database version); the per-task detail says which columns differ.
Also checks the safety validator: the gold SQL must be accepted (no false rejection).
Output: reports/bench/spider2/scorer_check.{json,md}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_agent.bench import spider2  # noqa: E402
from data_agent.bench.sqlite_db import SqliteGuard  # noqa: E402
from data_agent.safety.executor import ExecutionError, SafeSQLiteExecutor  # noqa: E402


def main() -> int:
    repo = ROOT / spider2.REPO
    db_files = spider2.find_db_files(ROOT / spider2.ROOT / "databases")
    tasks = spider2.load_tasks(repo)
    standards = spider2.load_standards(repo)
    executor = SafeSQLiteExecutor(timeout_s=120.0, max_rows=None)
    sql_dir = repo / "evaluation_suite" / "gold" / "sql"
    rows = []
    for task in tasks:
        iid = task["instance_id"]
        path = sql_dir / f"{iid}.sql"
        if not path.exists():
            continue
        row: dict = {"instance_id": iid, "db": task["db"]}
        if task["db"] not in db_files:
            row["result"] = "no database file"
            rows.append(row)
            continue
        db = spider2.introspect(db_files[task["db"]], task["db"])
        sql = path.read_text(encoding="utf-8").strip().rstrip(";")
        decision = SqliteGuard(db, timeout_s=120, max_rows=None).check(sql)
        row["validator"] = decision.decision
        row["validator_codes"] = decision.validation.codes
        try:
            res = executor.execute(db.path, sql, max_rows=None)
        except ExecutionError as e:
            row["result"] = "gold SQL failed"
            row["error"] = str(e)[:200]
            rows.append(row)
            continue
        golds = spider2.gold_files(repo, iid)
        ok = bool(golds) and spider2.score(res.columns, res.rows, golds, standards.get(iid, {}))
        row["result"] = "scored correct" if ok else "scored wrong"
        if not ok and golds:
            pred = spider2._columns_from_csv(spider2._rows_to_csv(res.columns, res.rows))
            gold = spider2._columns_from_csv(golds[0].read_text(encoding="utf-8", errors="replace"))
            row["detail"] = {
                "pred_rows": len(res.rows),
                "gold_rows": len(gold[0]) if gold else 0,
                "pred_cols": len(pred),
                "gold_cols": len(gold),
                "gold_variants": len(golds),
            }
        rows.append(row)

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    report = {
        "local_tasks": len(tasks),
        "tasks_with_gold_sql": len(rows),
        "results": counts,
        "validator_rejections": sum(1 for r in rows if r.get("validator") not in (None, "allow")),
        "tasks": rows,
    }
    out = ROOT / "reports" / "bench" / "spider2"
    out.mkdir(parents=True, exist_ok=True)
    (out / "scorer_check.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    lines = [
        "# Spider2-lite scorer check (published gold SQL vs published gold results)",
        "",
        f"Local tasks: {len(tasks)}; with published gold SQL: {len(rows)}. Each gold query was run "
        "read-only on the task's SQLite file and scored with this project's port of "
        "`compare_pandas_table`.",
        "",
        "| Outcome | Tasks |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in sorted(counts.items())]
    lines += [
        "",
        f"Gold queries rejected by the safety validator: {report['validator_rejections']}.",
        "",
        "| Task | Database | Outcome | Detail |",
        "|---|---|---|---|",
    ]
    for r in rows:
        detail = r.get("error") or (
            ", ".join(f"{k} {v}" for k, v in r["detail"].items()) if r.get("detail") else ""
        )
        lines.append(f"| {r['instance_id']} | {r['db']} | {r['result']} | {detail} |")
    (out / "scorer_check.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:12]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
