"""Spider2-lite, SQLite subset (the 135 `local*` tasks): single-shot baseline vs the agent.

    data-agent bench spider2 --mode single_shot --run-name spider2-qwen35
    data-agent bench spider2 --mode agent       --run-name spider2-qwen35

Data (downloaded on the laptop, untrusted, under data/bench/spider2/):
  repo/spider2-lite/spider2-lite.jsonl, repo/spider2-lite/evaluation_suite/gold/{exec_result,
  spider2lite_eval.jsonl}, repo/spider2-lite/resource/documents (external knowledge), and the
  SQLite files from local_sqlite.zip (link in the Spider2 README).

Scoring follows Spider2-lite's `compare_pandas_table` (evaluation_suite/evaluate.py,
xlang-ai/Spider2, commit cafb867): every gold column (or the `condition_cols` subset) must
equal some predicted column, numbers within 0.01 absolute, missing values as 0, order ignored
when `ignore_order`; extra predicted columns are allowed; a task with several gold variants
(_a, _b, ...) passes if any variant matches. Re-implemented without pandas: both sides go
through the same CSV round trip, so type inference is symmetric.
"""

from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from data_agent.bench.sqlite_db import SqliteDb, SqliteGuard
from data_agent.eval.harness import TrialStore
from data_agent.safety.executor import ExecutionError, SafeSQLiteExecutor

ROOT = Path("data/bench/spider2")
REPO = ROOT / "repo" / "spider2-lite"
TOL = 1e-2
_EXEC = SafeSQLiteExecutor(timeout_s=60.0, max_rows=None)


def introspect(path: Path, db_id: str) -> SqliteDb:
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        names = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables, pks, fks = {}, {}, []
        for t in names:
            cols = con.execute(f'PRAGMA table_info("{t}")').fetchall()
            tables[t] = {c[1]: (c[2] or "TEXT") for c in cols}
            pk = [c[1] for c in sorted(cols, key=lambda c: c[5]) if c[5]]
            if pk:
                pks[t] = pk
            for fk in con.execute(f'PRAGMA foreign_key_list("{t}")').fetchall():
                fks.append((t, fk[3], fk[2], fk[4] or ""))
    finally:
        con.close()
    return SqliteDb(db_id, Path(path), tables, pks, fks)


def find_db_files(db_root: Path) -> dict[str, Path]:
    return {p.stem: p for p in Path(db_root).rglob("*.sqlite")}


def load_tasks(repo: Path) -> list[dict]:
    rows = [
        json.loads(line)
        for line in (repo / "spider2-lite.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [r for r in rows if r["instance_id"].startswith("local")]


def load_standards(repo: Path) -> dict[str, dict]:
    path = repo / "evaluation_suite" / "gold" / "spider2lite_eval.jsonl"
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            out[item["instance_id"]] = item
    return out


def gold_files(repo: Path, instance_id: str) -> list[Path]:
    folder = repo / "evaluation_suite" / "gold" / "exec_result"
    single = folder / f"{instance_id}.csv"
    if single.exists():
        return [single]
    return sorted(folder.glob(f"{instance_id}_*.csv"))


# ----------------------------------------------------------------------------------
# Comparison (port of Spider2-lite compare_pandas_table / compare_multi_pandas_table)
# ----------------------------------------------------------------------------------


def _cell(v: str):
    if v is None or v == "" or v.lower() in ("nan", "none", "null"):
        return 0  # Spider2: missing values compare as 0
    try:
        return float(v)
    except ValueError:
        return v


def _columns_from_csv(text: str) -> list[list]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    body = rows[1:]
    width = len(rows[0])
    return [[_cell(r[i] if i < len(r) else "") for r in body] for i in range(width)]


def _rows_to_csv(columns: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return buf.getvalue()


def _vectors_match(v1: list, v2: list, ignore_order: bool) -> bool:
    if ignore_order:
        key = lambda x: (x is None, str(x), isinstance(x, int | float))  # noqa: E731 - mirrors upstream
        v1, v2 = sorted(v1, key=key), sorted(v2, key=key)
    if len(v1) != len(v2):
        return False
    for a, b in zip(v1, v2, strict=True):
        if isinstance(a, float) and isinstance(b, float):
            if not math.isclose(a, b, abs_tol=TOL):
                return False
        elif a != b:
            return False
    return True


def compare_tables(
    pred_cols: list[list], gold_cols: list[list], condition_cols=None, ignore_order: bool = False
) -> bool:
    if condition_cols:
        idx = condition_cols if isinstance(condition_cols, list | tuple) else [condition_cols]
        gold_cols = [gold_cols[i] for i in idx if i < len(gold_cols)]
    return all(any(_vectors_match(g, p, ignore_order) for p in pred_cols) for g in gold_cols)


def score(
    pred_columns: list[str], pred_rows: list[list], gold_paths: list[Path], standard: dict
) -> bool:
    pred = _columns_from_csv(_rows_to_csv(pred_columns, pred_rows))
    golds = [_columns_from_csv(p.read_text(encoding="utf-8", errors="replace")) for p in gold_paths]
    conds = standard.get("condition_cols")
    ignore = bool(standard.get("ignore_order", False))
    if len(golds) == 1:
        return compare_tables(pred, golds[0], conds, ignore)
    if conds in (None, [], [[]], [None]):
        conds = [[] for _ in golds]
    elif not all(isinstance(c, list) for c in conds):
        conds = [conds for _ in golds]
    return any(
        compare_tables(pred, g, conds[i] if i < len(conds) else [], ignore)
        for i, g in enumerate(golds)
    )


# ----------------------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------------------


def run_spider2(args) -> int:
    from data_agent.bench.bird import _bird_agent, final_sql_from_answer
    from data_agent.bench.single_shot import run_single_shot
    from data_agent.config import AgentConfig, Settings
    from data_agent.runtime import make_llm

    settings = Settings()
    repo = settings.home / REPO
    db_files = find_db_files(settings.home / ROOT / "databases")
    tasks = load_tasks(repo)
    standards = load_standards(repo)
    missing = sorted({t["db"] for t in tasks} - set(db_files))
    if missing:
        print(f"missing SQLite files for: {missing}", file=sys.stderr)
    tasks = [t for t in tasks if t["db"] in db_files]
    dbs = {name: introspect(db_files[name], name) for name in {t["db"] for t in tasks}}
    config = AgentConfig.load(
        Path(args.config or settings.home / "configs" / "agents" / "bench-bird-v1.yaml")
    )
    llm = make_llm(settings, config)
    out_dir = settings.home / "reports" / "bench" / "spider2" / args.run_name
    store = TrialStore(out_dir / f"{args.mode}.jsonl")
    done = {r["item_id"] for r in store.latest() if not r.get("retryable")}
    todo = [t for t in tasks if t["instance_id"] not in done]
    print(
        f"[spider2 {args.mode}] {len(done)} done, {len(todo)} to run", file=sys.stderr, flush=True
    )
    agent = _bird_agent(settings, config, llm, dbs, out_dir) if args.mode == "agent" else None
    guards = {
        name: SqliteGuard(db, timeout_s=60, max_rows=config.budgets.sql.max_rows)
        for name, db in dbs.items()
    }

    def knowledge(task: dict) -> str:
        name = task.get("external_knowledge")
        if not name:
            return ""
        path = repo / "resource" / "documents" / name
        return path.read_text(encoding="utf-8", errors="replace")[:6000] if path.exists() else ""

    def one(task: dict) -> dict:
        db = dbs[task["db"]]
        started = time.perf_counter()
        rec: dict = {
            "item_id": task["instance_id"],
            "trial": 0,
            "db_id": task["db"],
            "mode": args.mode,
        }
        hint = knowledge(task)
        if args.mode == "single_shot":
            r = run_single_shot(llm, db, guards[task["db"]], task["question"], hint)
            rec.update(
                {
                    "sql": r.sql,
                    "status": r.status,
                    "llm_calls": r.llm_calls,
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "retryable": r.status == "llm_error",
                }
            )
        else:
            q = task["question"] + (f"\nReference document:\n{hint}" if hint else "")
            res = agent.ask(q, user_id="bench", role=task["db"])
            executed = [
                e.get("safe_sql") or e.get("sql")
                for e in res.events
                if e.get("type") == "sql" and e.get("status") == "executed"
            ]
            rec.update(
                {
                    "sql": final_sql_from_answer(res.answer, executed),
                    "status": res.status,
                    "answer": res.answer[:2000],
                    "llm_calls": res.usage.get("llm_calls", 0),
                    "prompt_tokens": res.usage.get("prompt_tokens", 0),
                    "completion_tokens": res.usage.get("completion_tokens", 0),
                    "retryable": res.status == "error" and "unavailable" in (res.answer or ""),
                }
            )
        correct, why = False, None
        if rec["sql"] and rec["sql"] != "CANNOT_ANSWER":
            if guards[task["db"]].check(rec["sql"]).decision != "allow":
                why = "rejected by the validator"
            else:
                try:
                    res_ = _EXEC.execute(db.path, rec["sql"], max_rows=None)
                    paths = gold_files(repo, task["instance_id"])
                    correct = bool(paths) and score(
                        res_.columns, res_.rows, paths, standards.get(task["instance_id"], {})
                    )
                except ExecutionError as e:
                    why = f"pred failed: {str(e)[:160]}"
        else:
            why = "no sql"
        rec.update(
            {
                "correct": correct,
                "why": why,
                "latency_s": round(time.perf_counter() - started, 3),
                "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
        return rec

    with ThreadPoolExecutor(max_workers=min(args.concurrency, 4)) as pool:
        futures = {pool.submit(one, t): t for t in todo}
        for n, fut in enumerate(as_completed(futures), start=1):
            t = futures[fut]
            try:
                rec = fut.result()
            except Exception as e:  # noqa: BLE001
                rec = {
                    "item_id": t["instance_id"],
                    "trial": 0,
                    "db_id": t["db"],
                    "mode": args.mode,
                    "correct": False,
                    "error": f"{type(e).__name__}: {e}",
                    "retryable": True,
                }
            store.append(rec)
            if n % 10 == 0 or n == len(todo):
                recs = store.latest()
                print(
                    f"[spider2 {args.mode}] {n}/{len(todo)} EX so far {sum(r.get('correct', False) for r in recs) / len(recs):.3f}",
                    file=sys.stderr,
                    flush=True,
                )
    if agent is not None:
        agent.close()
        agent.runtime.close()
    write_report(out_dir, total=len(load_tasks(repo)), usable=len(tasks), model=llm.model)
    return 0


def write_report(out_dir: Path, total: int, usable: int, model: str) -> dict:
    import random
    import statistics

    modes = {}
    for mode in ("single_shot", "agent"):
        path = out_dir / f"{mode}.jsonl"
        if path.exists():
            modes[mode] = {r["item_id"]: r for r in TrialStore(path).latest()}
    summary: dict = {
        "run": out_dir.name,
        "model": model,
        "local_tasks": total,
        "tasks_with_database": usable,
        "modes": {},
    }
    for mode, recs in modes.items():
        rows = list(recs.values())
        summary["modes"][mode] = {
            "n": len(rows),
            "ex": round(sum(r["correct"] for r in rows) / len(rows), 4) if rows else None,
            "tokens_per_task": round(
                statistics.fmean(
                    r.get("prompt_tokens", 0) + r.get("completion_tokens", 0) for r in rows
                ),
                1,
            )
            if rows
            else None,
            "llm_calls_per_task": round(statistics.fmean(r.get("llm_calls", 0) for r in rows), 2)
            if rows
            else None,
            "failed_or_no_sql": sum(1 for r in rows if r.get("why")),
        }
    if len(modes) == 2:
        common = sorted(set(modes["agent"]) & set(modes["single_shot"]))
        diff = [
            int(modes["agent"][i]["correct"]) - int(modes["single_shot"][i]["correct"])
            for i in common
        ]
        rng = random.Random(7)
        boots = (
            sorted(statistics.fmean(rng.choices(diff, k=len(diff))) for _ in range(2000))
            if diff
            else []
        )
        summary["paired"] = {
            "n": len(common),
            "agent_minus_single_shot": round(statistics.fmean(diff), 4) if diff else None,
            "ci95": [round(boots[50], 4), round(boots[1949], 4)] if boots else None,
        }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    lines = [
        f"# Spider2-lite (SQLite subset): `{out_dir.name}`",
        "",
        f"Model `{model}`. Local tasks: {total}; with a database file: {usable}.",
        "",
        "| Mode | n | EX | tokens/task | model calls/task |",
        "|---|---|---|---|---|",
    ]
    for mode, m in summary["modes"].items():
        ex = "n/a" if m["ex"] is None else f"{100 * m['ex']:.1f}%"
        lines.append(
            f"| {mode} | {m['n']} | {ex} | {m['tokens_per_task']:,} | {m['llm_calls_per_task']} |"
        )
    if "paired" in summary and summary["paired"]["ci95"]:
        p = summary["paired"]
        lines += [
            "",
            f"Paired on {p['n']} tasks: agent - single-shot = {100 * p['agent_minus_single_shot']:.1f} points "
            f"(95% bootstrap CI {100 * p['ci95'][0]:.1f} to {100 * p['ci95'][1]:.1f}).",
        ]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def register(bsub) -> None:
    s = bsub.add_parser("spider2", help="Spider2-lite SQLite subset: single-shot baseline or agent")
    s.add_argument("--mode", choices=["single_shot", "agent"], required=True)
    s.add_argument("--run-name", required=True)
    s.add_argument("--concurrency", type=int, default=4)
    s.add_argument("--config")
    s.set_defaults(func=run_spider2)
