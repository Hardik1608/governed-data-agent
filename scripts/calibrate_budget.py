"""Calibrate the EXPLAIN-based cost budget on real queries.

Runs EXPLAIN (FORMAT JSON) and the query itself for every gold and alternative SQL in the
golden set (220 legitimate analyst queries), plus a set of deliberately expensive queries
(cross products of the fact tables), and reports the estimated rows of the largest plan
operator next to the measured runtime. The approval / reject thresholds in the agent config
are chosen from this report:  python scripts/calibrate_budget.py
Output: reports/budget/calibration.json and calibration.md
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_agent.config import AgentConfig, Settings  # noqa: E402
from data_agent.eval.golden import load_golden  # noqa: E402
from data_agent.safety.executor import QueryTimeoutError, SafeDuckDBExecutor  # noqa: E402
from data_agent.warehouse.replica import replica_file  # noqa: E402

EXPENSIVE = {
    "fact x fact cross product": "SELECT count(*) FROM fct_order_lines a, fct_order_lines b",
    "fact x customer cross product": "SELECT count(*) FROM fct_order_lines a, dim_customer b",
    "orders x orders cross product": "SELECT count(*) FROM fct_orders a, fct_orders b",
    "fact self-join on invoice": "SELECT count(*) FROM fct_order_lines a JOIN fct_order_lines b ON a.invoice_id = b.invoice_id",
    "orders x products cross product": "SELECT count(*) FROM fct_orders a, dim_product b",
}


def main() -> int:
    settings = Settings()
    cfg = AgentConfig.load(settings.agent_config_path)
    ex = SafeDuckDBExecutor(
        replica_file(settings.replica_path, "masked"), timeout_s=30, max_rows=1000
    )
    legit = []
    for item in load_golden(settings.home / "golden" / "v1"):
        for which in ("gold_sql", "alt_sql"):
            sql = getattr(item, which)
            if not sql:
                continue
            est = ex.explain(sql)
            t = time.perf_counter()
            ex.execute(sql, max_rows=1000)
            legit.append(
                {
                    "item": item.id,
                    "which": which,
                    "max_op": est.max_rows,
                    "total": est.total_rows,
                    "runtime_ms": round((time.perf_counter() - t) * 1000, 1),
                }
            )
    expensive = []
    for label, sql in EXPENSIVE.items():
        est = ex.explain(sql)
        t = time.perf_counter()
        try:
            ex.execute(sql, max_rows=10, timeout_s=10)
            outcome = "finished"
        except QueryTimeoutError:
            outcome = "timeout (10 s)"
        expensive.append(
            {
                "query": label,
                "max_op": est.max_rows,
                "total": est.total_rows,
                "outcome": outcome,
                "runtime_ms": round((time.perf_counter() - t) * 1000, 1),
            }
        )
    ex.close()
    max_ops = sorted(r["max_op"] for r in legit)
    runtimes = sorted(r["runtime_ms"] for r in legit)
    budget = cfg.budgets.sql
    report = {
        "legitimate_queries": len(legit),
        "legit_max_op": {
            "median": statistics.median(max_ops),
            "p95": max_ops[int(0.95 * len(max_ops))],
            "max": max_ops[-1],
        },
        "legit_runtime_ms": {
            "median": statistics.median(runtimes),
            "p95": runtimes[int(0.95 * len(runtimes))],
            "max": runtimes[-1],
        },
        "thresholds": {
            "approval_max_op": budget.est_rows_approval,
            "reject_max_op": budget.est_rows_reject,
        },
        "legit_needing_approval": sum(1 for r in legit if r["max_op"] > budget.est_rows_approval),
        "expensive": expensive,
        "top_legit": sorted(legit, key=lambda r: -r["max_op"])[:8],
    }
    out = settings.home / "reports" / "budget"
    out.mkdir(parents=True, exist_ok=True)
    (out / "calibration.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    lines = [
        "# EXPLAIN budget calibration",
        "",
        f"{len(legit)} legitimate queries (gold + alternative SQL of the golden set) on the analyst replica.",
        "",
        "| | Largest operator, estimated rows | Runtime |",
        "|---|---|---|",
        f"| Median | {report['legit_max_op']['median']:,.0f} | {report['legit_runtime_ms']['median']} ms |",
        f"| p95 | {report['legit_max_op']['p95']:,.0f} | {report['legit_runtime_ms']['p95']} ms |",
        f"| Max | {report['legit_max_op']['max']:,.0f} | {report['legit_runtime_ms']['max']} ms |",
        "",
        f"Thresholds: approval above {budget.est_rows_approval:,.0f}, reject above {budget.est_rows_reject:,.0f} "
        f"(largest operator). Legitimate queries that would need approval: {report['legit_needing_approval']}.",
        "",
        "| Expensive query | Largest operator, estimated | Decision | Outcome without the budget |",
        "|---|---|---|---|",
    ]
    for r in expensive:
        decision = (
            "reject"
            if r["max_op"] > budget.est_rows_reject
            else "approval"
            if r["max_op"] > budget.est_rows_approval
            else "allow"
        )
        lines.append(
            f"| {r['query']} | {r['max_op']:,.0f} | {decision} | {r['outcome']} ({r['runtime_ms']:,.0f} ms) |"
        )
    (out / "calibration.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
