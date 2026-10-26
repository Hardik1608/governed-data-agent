"""Measure the governance overheads on the golden workload (no model calls).

For the 220 gold + alternative queries of the golden set, on the analyst replica:
validation (parse, read-only, allowlist, column resolution, PII tagging), EXPLAIN budget,
and execution latency; plus replica build times from their metadata, and per-call model
usage from an eval run. Output: reports/perf/overheads.json and overheads.md
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
from data_agent.governance.policy import Policy  # noqa: E402
from data_agent.safety.guard import ExecutorPool, SqlGuard  # noqa: E402
from data_agent.warehouse.replica import replica_file  # noqa: E402


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * (len(s) - 1)))]


def main(run: str = "local-qwen35-9b-v1") -> int:
    s = Settings()
    cfg = AgentConfig.load(s.agent_config_path)
    policy = Policy.load(s.policy_path)
    pool = ExecutorPool(s.replica_path, timeout_s=60, max_rows=200)
    role = policy.role("analyst")
    guard = SqlGuard(policy, role, pool, cfg.budgets.sql, pool.get(role.replica).tables())
    v_ms, e_ms, x_ms = [], [], []
    for item in load_golden(s.home / "golden" / "v1"):
        for sql in (item.gold_sql, item.alt_sql):
            if not sql:
                continue
            t = time.perf_counter()
            res = guard.validator.validate(sql)
            v_ms.append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            pool.get(role.replica).explain(res.safe_sql)
            e_ms.append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            pool.get(role.replica).execute(res.safe_sql, max_rows=200)
            x_ms.append((time.perf_counter() - t) * 1000)
    pool.close()
    builds = {}
    for name in ("masked", "clear", "aggregates", "masked_injection"):
        meta = replica_file(s.replica_path, name).with_suffix(".json")
        if meta.exists():
            m = json.loads(meta.read_text(encoding="utf-8"))
            builds[name] = {"seconds": m["build_seconds"], "mb": round(m["bytes"] / 1e6, 1)}
    trials = [
        json.loads(line)
        for line in (s.home / "reports" / "eval" / run / "trials.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    calls = sum(t["usage"].get("llm_calls", 0) for t in trials)
    prompt = sum(t["usage"].get("prompt_tokens", 0) for t in trials)
    completion = sum(t["usage"].get("completion_tokens", 0) for t in trials)
    report = {
        "queries": len(v_ms),
        "validate_ms": {
            "median": round(statistics.median(v_ms), 2),
            "p95": round(pct(v_ms, 0.95), 2),
        },
        "explain_ms": {
            "median": round(statistics.median(e_ms), 2),
            "p95": round(pct(e_ms, 0.95), 2),
        },
        "execute_ms": {
            "median": round(statistics.median(x_ms), 2),
            "p95": round(pct(x_ms, 0.95), 2),
        },
        "replica_builds": builds,
        "eval_run": run,
        "model_calls": calls,
        "prompt_tokens_per_call": round(prompt / calls, 1) if calls else None,
        "completion_tokens_per_call": round(completion / calls, 1) if calls else None,
        "model_calls_per_trial": round(calls / len(trials), 2),
    }
    out = s.home / "reports" / "perf"
    out.mkdir(parents=True, exist_ok=True)
    (out / "overheads.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    lines = [
        "# Governance overheads (golden workload, analyst replica)",
        "",
        f"{report['queries']} queries (gold + alternative SQL of the golden set).",
        "",
        "| Step | Median | p95 |",
        "|---|---|---|",
        f"| Validate (parse, read-only, allowlist, columns, PII tags) | {report['validate_ms']['median']} ms | {report['validate_ms']['p95']} ms |",
        f"| EXPLAIN cost estimate | {report['explain_ms']['median']} ms | {report['explain_ms']['p95']} ms |",
        f"| Execute (read-only replica) | {report['execute_ms']['median']} ms | {report['execute_ms']['p95']} ms |",
        "",
        "Replica builds: "
        + ", ".join(f"{k} {v['seconds']} s / {v['mb']} MB" for k, v in builds.items())
        + ".",
        "",
        f"Model usage in eval run `{run}`: {report['model_calls_per_trial']} calls per trial, "
        f"{report['prompt_tokens_per_call']:,} prompt and {report['completion_tokens_per_call']:,} completion tokens per call.",
    ]
    (out / "overheads.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
