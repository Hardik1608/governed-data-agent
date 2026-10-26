"""BIRD dev: the v1 single-shot pipeline vs the agent, same model, same safety layer.

    data-agent bench bird --mode single_shot --run-name bird-qwen35 [--limit N] [--stratified]
    data-agent bench bird --mode agent       --run-name bird-qwen35
    data-agent bench bird-report             --run-name bird-qwen35

Data: BIRD dev (dev_20240627: 1,534 questions on 11 SQLite databases, with "evidence" hints),
downloaded to data/bench/bird/raw by `scripts/get_bird.sh` style steps (see README).

Execution accuracy (EX) follows BIRD's evaluator: the predicted and gold SQL are executed
and their result *sets* compared (order and duplicates ignored); a prediction that fails to
run scores 0. For the 42 questions in `dev_tied_append.json` (ties under ORDER BY ... LIMIT),
a match with the alternative gold result also counts, as the BIRD site specifies.
Each question is recorded in a JSONL checkpoint as soon as it is graded; re-running the same
command resumes. Results: reports/bench/bird/<run>/.
"""

from __future__ import annotations

import json
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from data_agent.bench.sqlite_db import (
    SqliteDb,
    SqliteGuard,
    catalog_for,
    load_spider_style_schemas,
    sample_values,
)
from data_agent.eval.harness import TrialStore
from data_agent.safety.executor import ExecutionError, SafeSQLiteExecutor

BIRD_DIR = Path("data/bench/bird/raw/dev_20240627")


def load_bird(root: Path) -> tuple[list[dict], dict[str, SqliteDb], dict[int, str]]:
    items = json.loads((root / "dev.json").read_text(encoding="utf-8"))
    dbs = load_spider_style_schemas(root / "dev_tables.json", root / "dev_databases")
    tied = {}
    tied_path = root / "dev_tied_append.json"
    if tied_path.exists():
        tied = {
            t["question_id"]: t["SQL"] for t in json.loads(tied_path.read_text(encoding="utf-8"))
        }
    return items, dbs, tied


def stratified_subset(items: list[dict], n: int, seed: int = 7) -> list[dict]:
    """Same difficulty x database mix as the full set (largest-remainder allocation)."""
    rng = random.Random(seed)
    groups: dict[tuple, list[dict]] = {}
    for it in items:
        groups.setdefault((it["difficulty"], it["db_id"]), []).append(it)
    total = len(items)
    quotas = {k: n * len(v) / total for k, v in groups.items()}
    alloc = {k: int(q) for k, q in quotas.items()}
    for k in sorted(quotas, key=lambda k: quotas[k] - alloc[k], reverse=True)[
        : n - sum(alloc.values())
    ]:
        alloc[k] += 1
    out = []
    for k, v in sorted(groups.items()):
        out += rng.sample(v, alloc[k])
    return sorted(out, key=lambda it: it["question_id"])


# ----------------------------------------------------------------------------------
# Execution accuracy
# ----------------------------------------------------------------------------------

_EXEC = SafeSQLiteExecutor(timeout_s=30.0, max_rows=None)
_gold_cache: dict[tuple[str, str], frozenset | None] = {}
_gold_lock = threading.Lock()


def result_set(db: SqliteDb, sql: str) -> frozenset:
    res = _EXEC.execute(db.path, sql, max_rows=None)
    return frozenset(tuple(row) for row in res.rows)


def gold_set(db: SqliteDb, sql: str) -> frozenset | None:
    key = (db.db_id, sql)
    with _gold_lock:
        if key in _gold_cache:
            return _gold_cache[key]
    try:
        value = result_set(db, sql)
    except ExecutionError:
        value = None
    with _gold_lock:
        _gold_cache[key] = value
    return value


def execution_match(
    db: SqliteDb, pred_sql: str, gold_sql: str, alt_gold: str | None = None
) -> tuple[bool, str | None]:
    if not pred_sql or pred_sql == "CANNOT_ANSWER":
        return False, "no sql"
    try:
        pred = result_set(db, pred_sql)
    except ExecutionError as e:
        return False, f"pred failed: {str(e)[:160]}"
    for gold_sql_ in [gold_sql] + ([alt_gold] if alt_gold else []):
        gold = gold_set(db, gold_sql_)
        if gold is not None and pred == gold:
            return True, None
    return False, None


# ----------------------------------------------------------------------------------
# Runners
# ----------------------------------------------------------------------------------


def question_text(item: dict) -> str:
    q = item["question"].strip()
    return f"{q}\nHint: {item['evidence'].strip()}" if item.get("evidence") else q


def final_sql_from_answer(answer: str, executed: list[str]) -> str:
    from data_agent.bench.single_shot import extract_sql

    if "```" in (answer or ""):
        sql = extract_sql(answer)
        if sql and sql != "CANNOT_ANSWER":
            return sql
    return executed[-1] if executed else ""


def run_bird(args) -> int:
    from data_agent.bench.single_shot import run_single_shot
    from data_agent.config import AgentConfig, Settings
    from data_agent.runtime import make_llm

    settings = Settings()
    root = Path(args.bird_dir or settings.home / BIRD_DIR)
    items, dbs, tied = load_bird(root)
    if args.limit:
        items = stratified_subset(items, args.limit) if args.stratified else items[: args.limit]
    config = AgentConfig.load(
        Path(args.config or settings.home / "configs" / "agents" / "bench-bird-v1.yaml")
    )
    llm = make_llm(settings, config)
    out_dir = settings.home / "reports" / "bench" / "bird" / args.run_name
    store = TrialStore(out_dir / f"{args.mode}.jsonl")
    done = {(r["item_id"], 0) for r in store.latest() if not r.get("retryable")}
    todo = [it for it in items if (str(it["question_id"]), 0) not in done]
    print(f"[bird {args.mode}] {len(done)} done, {len(todo)} to run", file=sys.stderr, flush=True)

    agent = None
    if args.mode == "agent":
        agent = _bird_agent(settings, config, llm, dbs, out_dir)
    guards = {
        db_id: SqliteGuard(db, timeout_s=30, max_rows=config.budgets.sql.max_rows)
        for db_id, db in dbs.items()
    }

    def one(item: dict) -> dict:
        db = dbs[item["db_id"]]
        started = time.perf_counter()
        rec: dict = {
            "item_id": str(item["question_id"]),
            "trial": 0,
            "db_id": item["db_id"],
            "difficulty": item["difficulty"],
            "mode": args.mode,
        }
        if args.mode == "single_shot":
            r = run_single_shot(
                llm, db, guards[item["db_id"]], item["question"], item.get("evidence", "")
            )
            rec.update(
                {
                    "sql": r.sql,
                    "status": r.status,
                    "llm_calls": r.llm_calls,
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "repaired": r.repaired,
                    "error": r.error,
                }
            )
            rec["retryable"] = r.status == "llm_error"
        else:
            res = agent.ask(question_text(item), user_id="bench", role=item["db_id"])
            executed = [
                e.get("safe_sql") or e.get("sql")
                for e in res.events
                if e.get("type") == "sql" and e.get("status") == "executed"
            ]
            sql = final_sql_from_answer(res.answer, executed)
            rec.update(
                {
                    "sql": sql,
                    "status": res.status,
                    "answer": res.answer[:2000],
                    "llm_calls": res.usage.get("llm_calls", 0),
                    "prompt_tokens": res.usage.get("prompt_tokens", 0),
                    "completion_tokens": res.usage.get("completion_tokens", 0),
                    "queries_run": len(executed),
                    "thread_id": res.thread_id,
                }
            )
            rec["retryable"] = res.status == "error" and "unavailable" in (res.answer or "")
            if sql and guards[item["db_id"]].check(sql).decision != "allow":
                rec["sql_rejected"] = True  # the final SQL must pass the same safety validator
        ok, why = execution_match(db, rec["sql"], item["SQL"], tied.get(item["question_id"]))
        rec.update(
            {
                "correct": ok,
                "why": why,
                "latency_s": round(time.perf_counter() - started, 3),
                "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
        if rec.get("sql_rejected"):
            rec["correct"] = False
        return rec

    finished, started = 0, time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(args.concurrency, 4)) as pool:
        futures = {pool.submit(one, it): it for it in todo}
        for fut in as_completed(futures):
            it = futures[fut]
            try:
                rec = fut.result()
            except Exception as e:  # noqa: BLE001
                rec = {
                    "item_id": str(it["question_id"]),
                    "trial": 0,
                    "db_id": it["db_id"],
                    "difficulty": it["difficulty"],
                    "mode": args.mode,
                    "correct": False,
                    "error": f"{type(e).__name__}: {e}",
                    "retryable": True,
                }
            store.append(rec)
            finished += 1
            if finished % 10 == 0 or finished == len(todo):
                recs = store.latest()
                acc = sum(r.get("correct", False) for r in recs) / max(1, len(recs))
                print(
                    f"[bird {args.mode}] {finished}/{len(todo)} in {time.perf_counter() - started:.0f}s, EX so far {acc:.3f}",
                    file=sys.stderr,
                    flush=True,
                )
    if agent is not None:
        agent.close()
        agent.runtime.close()
    write_bird_report(
        out_dir,
        items_total=len(items),
        subset=bool(args.limit),
        stratified=bool(args.stratified),
        config=config,
        llm_model=llm.model,
    )
    return 0


def _bird_agent(settings, config, llm, dbs: dict[str, SqliteDb], out_dir: Path):
    """An AgentService whose 'roles' are the BIRD databases (one toolkit per database)."""
    from data_agent.agent.service import AgentService
    from data_agent.governance.audit import AuditLog
    from data_agent.observability.tracing import Tracer
    from data_agent.retrieval.index import CatalogIndex, build_documents
    from data_agent.runtime import Runtime, make_embedder
    from data_agent.tools.toolkit import ToolKit
    from data_agent.warehouse.metrics import MetricsLayer

    embedder = make_embedder(settings)
    toolkits = {}
    index_dir = settings.home / "data" / "bench" / "bird" / "index"
    for db_id, db in dbs.items():
        catalog = catalog_for(db, sample_values(db))
        index = CatalogIndex.build(
            build_documents(catalog, None),
            embedder,
            index_dir / f"{db_id}.duckdb" if embedder else None,
        )
        guard = SqliteGuard(db, timeout_s=30, max_rows=config.budgets.sql.max_rows)
        toolkits[db_id] = ToolKit(None, guard.role, catalog, MetricsLayer({}, {}), index, guard)
    tmp = out_dir / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    runtime = Runtime(
        settings=settings,
        config=config,
        policy=None,
        metrics=MetricsLayer({}, {}),
        catalog=None,
        index=None,
        pool=_NoPool(),
        toolkits=toolkits,
        audit=AuditLog(tmp / "audit.sqlite"),
        llm=llm,
        tracer=Tracer(False),
        redactor=None,
    )
    # Spider2-lite tasks can carry a reference document (up to 6,000 characters) with the
    # question; the service default of 2,000 characters is for interactive use.
    return AgentService(
        runtime, checkpoint_path=tmp / "checkpoints.sqlite", max_question_chars=10_000
    )


class _NoPool:
    def close(self) -> None:
        return None


def write_bird_report(
    out_dir: Path, items_total: int, subset: bool, stratified: bool, config, llm_model: str
) -> dict:
    modes = {}
    for mode in ("single_shot", "agent"):
        path = out_dir / f"{mode}.jsonl"
        if path.exists():
            modes[mode] = {r["item_id"]: r for r in TrialStore(path).latest()}
    summary: dict = {
        "run": out_dir.name,
        "model": llm_model,
        "questions_in_set": items_total,
        "subset": subset,
        "stratified": stratified,
        "temperature": config.model.temperature,
        "modes": {},
    }
    for mode, recs in modes.items():
        rows = list(recs.values())
        by_diff = {}
        for diff in ("simple", "moderate", "challenging"):
            sub = [r for r in rows if r["difficulty"] == diff]
            if sub:
                by_diff[diff] = {
                    "n": len(sub),
                    "ex": round(sum(r["correct"] for r in sub) / len(sub), 4),
                }
        lat = sorted(r.get("latency_s", 0) for r in rows)
        summary["modes"][mode] = {
            "n": len(rows),
            "ex": round(sum(r["correct"] for r in rows) / len(rows), 4) if rows else None,
            "by_difficulty": by_diff,
            "tokens_per_question": round(
                statistics.fmean(
                    r.get("prompt_tokens", 0) + r.get("completion_tokens", 0) for r in rows
                ),
                1,
            )
            if rows
            else None,
            "llm_calls_per_question": round(
                statistics.fmean(r.get("llm_calls", 0) for r in rows), 2
            )
            if rows
            else None,
            "latency_p50_s": lat[len(lat) // 2] if lat else None,
            "latency_p95_s": lat[int(0.95 * (len(lat) - 1))] if lat else None,
            "no_sql_or_failed": sum(
                1 for r in rows if (r.get("why") or "").startswith(("no sql", "pred failed"))
            ),
            "statuses": _count(r.get("status", "?") for r in rows),
        }
    if len(modes) == 2:
        common = sorted(set(modes["single_shot"]) & set(modes["agent"]))
        a = [int(modes["agent"][i]["correct"]) for i in common]
        b = [int(modes["single_shot"][i]["correct"]) for i in common]
        diff = [x - y for x, y in zip(a, b, strict=True)]
        rng = random.Random(7)
        boots = (
            sorted(statistics.fmean(rng.choices(diff, k=len(diff))) for _ in range(2000))
            if diff
            else []
        )
        summary["paired"] = {
            "n": len(common),
            "agent_ex": round(statistics.fmean(a), 4) if a else None,
            "single_shot_ex": round(statistics.fmean(b), 4) if b else None,
            "agent_minus_single_shot": round(statistics.fmean(diff), 4) if diff else None,
            "ci95": [round(boots[50], 4), round(boots[1949], 4)] if boots else None,
            "agent_only_correct": sum(1 for x, y in zip(a, b, strict=True) if x and not y),
            "single_shot_only_correct": sum(1 for x, y in zip(a, b, strict=True) if y and not x),
        }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    lines = [
        f"# BIRD dev: `{out_dir.name}`",
        "",
        f"Model `{llm_model}`, temperature {config.model.temperature}. "
        f"Questions in this run's set: {items_total}{' (stratified subset of the 1,534; same difficulty x database mix)' if stratified else ' (subset)' if subset else ''}. Each mode's n is in the table.",
        "",
        "| Mode | n | EX | simple | moderate | challenging | tokens/question | model calls/question | p50 / p95 latency |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for mode, m in summary["modes"].items():
        d = m["by_difficulty"]
        lines.append(
            f"| {mode} | {m['n']} | {_pct(m['ex'])} | {_pct(d.get('simple', {}).get('ex'))} | {_pct(d.get('moderate', {}).get('ex'))} | "
            f"{_pct(d.get('challenging', {}).get('ex'))} | {m['tokens_per_question']:,} | {m['llm_calls_per_question']} | {m['latency_p50_s']} s / {m['latency_p95_s']} s |"
        )
    if "paired" in summary:
        p = summary["paired"]
        lines += [
            "",
            f"Paired on the {p['n']} questions both modes ran: agent {_pct(p['agent_ex'])} vs single-shot "
            f"{_pct(p['single_shot_ex'])}, difference {_pct(p['agent_minus_single_shot'])} "
            f"(95% bootstrap CI {_pct(p['ci95'][0])} to {_pct(p['ci95'][1])}); agent-only correct "
            f"{p['agent_only_correct']}, single-shot-only correct {p['single_shot_only_correct']}.",
        ]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def _pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _count(values) -> dict:
    out: dict = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def cmd_bird_report(args) -> int:
    from data_agent.config import AgentConfig, Settings

    settings = Settings()
    out_dir = settings.home / "reports" / "bench" / "bird" / args.run_name
    config = AgentConfig.load(settings.home / "configs" / "agents" / "bench-bird-v1.yaml")
    summary = write_bird_report(
        out_dir,
        items_total=args.total,
        subset=args.total < 1534,
        stratified=args.stratified,
        config=config,
        llm_model=args.model,
    )
    print(json.dumps(summary, indent=1)[:3000])
    return 0


def register(sub) -> None:
    p = sub.add_parser("bench", help="public benchmarks (BIRD dev, Spider2-lite)")
    bsub = p.add_subparsers(dest="bench_command", required=True)
    b = bsub.add_parser("bird", help="BIRD dev: single-shot baseline or agent")
    b.add_argument("--mode", choices=["single_shot", "agent"], required=True)
    b.add_argument("--run-name", required=True)
    b.add_argument("--limit", type=int)
    b.add_argument("--stratified", action="store_true")
    b.add_argument("--concurrency", type=int, default=4)
    b.add_argument("--bird-dir")
    b.add_argument("--config")
    b.set_defaults(func=run_bird)
    r = bsub.add_parser("bird-report", help="rebuild the BIRD report from the checkpoints")
    r.add_argument("--run-name", required=True)
    r.add_argument("--total", type=int, default=1534)
    r.add_argument("--stratified", action="store_true")
    r.add_argument("--model", default="local-agent")
    r.set_defaults(func=cmd_bird_report)

    from data_agent.bench import spider2

    spider2.register(bsub)
