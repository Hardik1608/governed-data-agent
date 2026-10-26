"""Spider2-lite (SQLite subset): where each mode fails, paired, and whether the validator was right.

Reads reports/bench/spider2/<run>/{single_shot,agent}.jsonl. For every task it records the
failure type of each mode, and for every query the safety validator rejected it checks
whether SQLite itself would have run it (a rejection of SQL that runs would be a false
rejection by the validator; one that SQLite also refuses is not).
Output: reports/bench/spider2/<run>/analysis.{json,md}
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_agent.bench import spider2  # noqa: E402
from data_agent.safety.executor import ExecutionError, SafeSQLiteExecutor  # noqa: E402

MODES = ("single_shot", "agent")


def load(path: Path) -> dict[str, dict]:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                out[r["item_id"]] = r
    return out


def failure(r: dict) -> str:
    why = r.get("why") or ""
    if r.get("correct"):
        return "correct"
    if why.startswith("rejected"):
        return "rejected by the validator"
    if why.startswith("no sql"):
        return (
            "no SQL (gave up or budget)" if r.get("status") != "cannot_answer" else "CANNOT_ANSWER"
        )
    if why.startswith("pred failed"):
        return "SQL error or timeout"
    return "wrong result"


def main(run: str = "spider2-qwen35-9b") -> int:
    d = ROOT / "reports" / "bench" / "spider2" / run
    recs = {m: load(d / f"{m}.jsonl") for m in MODES}
    files = spider2.find_db_files(ROOT / spider2.ROOT / "databases")
    executor = SafeSQLiteExecutor(timeout_s=30.0, max_rows=None)
    report: dict = {"run": run, "modes": {}}
    report["databases"] = len({r["db_id"] for rows in recs.values() for r in rows.values()})
    for mode, rows in recs.items():
        if not rows:
            continue
        kinds = Counter(failure(r) for r in rows.values())
        rejected = [r for r in rows.values() if failure(r) == "rejected by the validator"]
        sqlite_also_refuses = 0
        for r in rejected:
            try:
                executor.execute(files[r["db_id"]], r["sql"], max_rows=None)
            except (ExecutionError, KeyError):
                sqlite_also_refuses += 1
        lat = sorted(r.get("latency_s", 0.0) for r in rows.values())
        stopped = [r for r in rows.values() if r.get("status") == "budget_exceeded"]
        report["modes"][mode] = {
            "n": len(rows),
            "correct": kinds.get("correct", 0),
            "outcomes": dict(kinds.most_common()),
            "rejected_by_validator": len(rejected),
            "rejected_sql_that_sqlite_also_refuses": sqlite_also_refuses,
            "statuses": dict(Counter(r.get("status", "?") for r in rows.values()).most_common()),
            "budget_stops": len(stopped),
            "budget_stops_still_correct": sum(1 for r in stopped if r.get("correct")),
            "latency_p50_s": lat[len(lat) // 2],
            "latency_p95_s": lat[int(0.95 * (len(lat) - 1))],
        }
    if all(recs.values()):
        common = sorted(set(recs["single_shot"]) & set(recs["agent"]))
        a = {i: recs["agent"][i]["correct"] for i in common}
        b = {i: recs["single_shot"][i]["correct"] for i in common}
        report["paired"] = {
            "n": len(common),
            "both_correct": sum(1 for i in common if a[i] and b[i]),
            "agent_only": sorted(i for i in common if a[i] and not b[i]),
            "single_shot_only": sorted(i for i in common if b[i] and not a[i]),
        }
    (d / "analysis.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    lines = [
        f"# Spider2-lite: where each mode fails (`{run}`)",
        "",
        f"Databases: {report['databases']}.",
        "",
    ]
    for mode, m in report["modes"].items():
        lines += [
            f"**{mode}** ({m['n']} tasks, {m['correct']} correct): "
            + ", ".join(f"{k} {v}" for k, v in m["outcomes"].items())
            + f". Rejected by the validator: {m['rejected_by_validator']}, of which SQLite itself "
            f"also refuses {m['rejected_sql_that_sqlite_also_refuses']}. Model-call budget reached: "
            f"{m['budget_stops']} (still correct: {m['budget_stops_still_correct']}). Latency p50 / "
            f"p95: {m['latency_p50_s']} s / {m['latency_p95_s']} s.",
            "",
        ]
    if "paired" in report:
        p = report["paired"]
        lines += [
            f"Paired on {p['n']} tasks: both correct {p['both_correct']}; agent only "
            f"{len(p['agent_only'])} ({', '.join(p['agent_only']) or '-'}); single-shot only "
            f"{len(p['single_shot_only'])} ({', '.join(p['single_shot_only']) or '-'}).",
        ]
    (d / "analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
