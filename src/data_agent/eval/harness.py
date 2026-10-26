"""Golden-set evaluation harness.

    data-agent eval run --run-name local-v1 --k 4                    # real model (DATA_AGENT_LLM_*)
    data-agent eval run --run-name ci-smoke --llm oracle --k 1 --expected recompute

For every (item, trial) the harness asks the agent through the same AgentService the API
uses, plays the user (it answers clarifying questions with the item's scripted reply, and
stops ambiguous items as soon as the agent asks) and the approver (deny by default), then
grades the trial. Each finished trial is appended to `trials.jsonl` (flushed and fsynced),
so a crashed or interrupted run resumes where it stopped: `--resume` skips every
(item, trial) already recorded and re-runs only what was in flight. At most `concurrency`
trials run at once (the model client also caps in-flight requests at 4).

Outputs in reports/eval/<run>/: trials.jsonl, summary.json, summary.md.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from data_agent.agent.service import INTERRUPTED, AgentService
from data_agent.eval.golden import GoldenItem, load_expected, load_golden
from data_agent.eval.graders import TrialFacts, grade_trial, is_refusal
from data_agent.governance.pii import PiiRedactor

log = logging.getLogger(__name__)

DEFAULT_REPLY = (
    "Please go ahead with the most standard interpretation, using the certified metric definitions."
)


@dataclass
class EvalConfig:
    run_name: str
    out_dir: Path
    k: int = 4
    concurrency: int = 4
    approver: str = "deny"  # deny | approve
    max_rounds: int = 4
    categories: tuple[str, ...] | None = None
    item_ids: tuple[str, ...] | None = None
    notes: dict = field(default_factory=dict)


class TrialStore:
    """Append-only JSONL. A torn last line (power cut mid-write) is ignored on load."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("skipping a torn line in %s", self.path)
        return out

    def latest(self) -> list[dict]:
        """The last record of every (item, trial): a re-run supersedes a retryable failure."""
        out: dict[tuple[str, int], dict] = {}
        for t in self.load():
            out[(t["item_id"], t["trial"])] = t
        return list(out.values())

    def done(self) -> set[tuple[str, int]]:
        return {(t["item_id"], t["trial"]) for t in self.latest() if not t.get("retryable")}

    def rewrite(self, records: list[dict]) -> None:
        """Replace the whole log atomically (used by regrade, after a run has finished)."""
        with self._lock:
            tmp = self.path.with_name(self.path.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                for record in records:
                    fh.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)

    def append(self, record: dict) -> None:
        line = json.dumps(record, default=str, ensure_ascii=False)
        with self._lock:
            # After a torn write (power cut), start on a fresh line so the new record survives.
            if self.path.exists() and self.path.stat().st_size:
                with self.path.open("rb") as tail:
                    tail.seek(-1, os.SEEK_END)
                    if tail.read(1) != b"\n":
                        line = "\n" + line
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())


def _tool_calls(service: AgentService, thread_id: str) -> list[dict]:
    values = service._values(thread_id)
    calls = []
    for msg in values.get("messages") or []:
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            calls.append({"name": fn.get("name"), "arguments": fn.get("arguments") or ""})
    return calls


def run_trial(
    item: GoldenItem,
    trial: int,
    service: AgentService,
    cfg: EvalConfig,
    expected: dict,
    redactor: PiiRedactor | None,
) -> dict:
    thread_id = f"{cfg.run_name}-{item.id}-t{trial}-{uuid.uuid4().hex[:6]}"
    started = time.perf_counter()
    events: list[dict] = []
    asked = stopped_for_question = False
    questions: list[str] = []
    error = None
    status, answer, usage = "error", "", {}
    try:
        result = service.ask(item.question, user_id="eval", role="analyst", thread_id=thread_id)
        rounds = 0
        while result.status == INTERRUPTED:
            events += result.events
            usage = result.usage or usage
            answers: dict[str, dict] = {}
            for req in result.interrupt.get("requests", []):
                if req.get("kind") in ("clarification", "choose_metric"):
                    asked = True
                    questions.append(req.get("question", ""))
                    answers[req["id"]] = {"reply": item.on_clarification or DEFAULT_REPLY}
                elif req.get("kind") == "approval":
                    answers[req["id"]] = {
                        "approved": cfg.approver == "approve",
                        "by": "eval-approver",
                    }
                else:
                    answers[req["id"]] = {"reply": DEFAULT_REPLY}
            if asked and item.category == "ambiguous":
                stopped_for_question = True  # correct behaviour reached: stop here
                break
            rounds += 1
            if rounds > cfg.max_rounds:
                break
            result = service.resume(thread_id, "eval", answers)
        events += result.events
        usage = result.usage or usage
        status = "needs_clarification" if stopped_for_question else result.status
        answer = result.answer or ""
    except Exception as e:  # noqa: BLE001 - one broken trial must not stop the run
        error = f"{type(e).__name__}: {e}"
        log.exception("trial %s/%s failed", item.id, trial)
    latency = round(time.perf_counter() - started, 3)

    values = service._values(thread_id) if error is None else {}
    calls = _tool_calls(service, thread_id) if error is None else []
    if status == "needs_clarification":
        asked = True
    sql_events = [e for e in events if e.get("type") == "sql"]
    reveal_requests = 0
    for call in calls:
        if call["name"] == "run_sql":
            try:
                if json.loads(call["arguments"] or "{}").get("reveal_pii"):
                    reveal_requests += 1
            except (json.JSONDecodeError, AttributeError):
                pass
    leaks = []
    if redactor is not None and redactor.active:
        allowed = set(redactor.find(item.question))
        leaks = [v for v in redactor.find(answer) if v not in allowed]
    facts = TrialFacts(
        status=status,
        answer=answer,
        asked=asked,
        asked_before_answer=asked and status in ("needs_clarification", INTERRUPTED),
        sql_events=sql_events,
        tool_args=[c["arguments"] for c in calls],
        reveal_pii_requests=reveal_requests,
        pii_leaks=leaks,
        redactions=sum(e.get("count", 0) for e in events if e.get("type") == "pii_redacted"),
        untraced=list((values.get("check") or {}).get("untraced", [])),
    )
    grade = grade_trial(item, expected.get(item.id), facts, item.question)
    if error:
        grade.passed = False
        grade.reasons.insert(0, f"harness error: {error}")
    # The model server being down is not the agent's fault: such trials are re-run on resume.
    retryable = bool(error) or (status == "error" and "unavailable" in answer)
    return {
        "run": cfg.run_name,
        "item_id": item.id,
        "category": item.category,
        "trial": trial,
        "thread_id": thread_id,
        "status": status,
        "answer": answer,
        "asked": asked,
        "clarification_questions": questions,
        "refused": is_refusal(answer, status),
        "sql": [
            {
                k: e.get(k)
                for k in (
                    "sql",
                    "status",
                    "codes",
                    "unsafe",
                    "query_id",
                    "row_count",
                    "estimated_rows",
                )
            }
            for e in sql_events
        ],
        "tools": [c["name"] for c in calls],
        "safety": grade.checks.get("safety", {}),
        "grade": grade.to_dict(),
        "usage": usage,
        "latency_s": latency,
        "untraced": facts.untraced,
        "redactions": facts.redactions,
        "event_types": dict(Counter(e.get("type", "?") for e in events)),
        "facts": {
            "asked_before_answer": facts.asked_before_answer,
            "sql_events": sql_events,
            "tool_args": facts.tool_args,
            "reveal_pii_requests": facts.reveal_pii_requests,
            "pii_leaks": facts.pii_leaks,
        },
        "error": error,
        "retryable": retryable,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def run_eval(
    cfg: EvalConfig,
    items: list[GoldenItem],
    services: dict[str, AgentService],
    expected: dict,
    redactor: PiiRedactor | None,
    resume: bool = True,
    progress=None,
) -> list[dict]:
    store = TrialStore(cfg.out_dir / "trials.jsonl")
    done = store.done() if resume else set()
    if not resume and store.path.exists():
        store.path.unlink()
    todo = [(item, t) for item in items for t in range(cfg.k) if (item.id, t) not in done]
    progress = progress or (lambda msg: print(msg, file=sys.stderr, flush=True))
    progress(
        f"[{cfg.run_name}] {len(done)} trials already recorded, {len(todo)} to run (k={cfg.k}, concurrency={cfg.concurrency})"
    )
    started = time.perf_counter()
    finished = 0
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        futures = {
            pool.submit(run_trial, item, t, services[item.replica], cfg, expected, redactor): (
                item,
                t,
            )
            for item, t in todo
        }
        for fut in as_completed(futures):
            item, t = futures[fut]
            record = fut.result()
            store.append(record)
            finished += 1
            mark = "PASS" if record["grade"]["passed"] else "fail"
            why = (
                ""
                if record["grade"]["passed"]
                else f" ({'; '.join(record['grade']['reasons'])[:120]})"
            )
            elapsed = time.perf_counter() - started
            progress(
                f"[{finished}/{len(todo)} {elapsed:6.0f}s] {item.id} t{t} {mark} {record['latency_s']:.1f}s{why}"
            )
    return store.latest()


def regrade(
    trials: list[dict], items: list[GoldenItem], expected: dict, redactor: PiiRedactor | None
) -> list[dict]:
    """Re-grade stored trials with the current graders (no model calls)."""
    by_id = {i.id: i for i in items}
    out = []
    for t in trials:
        item = by_id.get(t["item_id"])
        if item is None:
            continue
        f = t.get("facts") or {}
        leaks = f.get("pii_leaks", [])
        if redactor is not None and redactor.active:
            allowed = set(redactor.find(item.question))
            leaks = [v for v in redactor.find(t["answer"]) if v not in allowed]
        facts = TrialFacts(
            status=t["status"],
            answer=t["answer"],
            asked=t["asked"],
            asked_before_answer=f.get("asked_before_answer", t["asked"]),
            sql_events=f.get("sql_events", t.get("sql", [])),
            tool_args=f.get("tool_args", []),
            reveal_pii_requests=f.get("reveal_pii_requests", 0),
            pii_leaks=leaks,
            redactions=t.get("redactions", 0),
            untraced=t.get("untraced", []),
        )
        grade = grade_trial(item, expected.get(item.id), facts, item.question)
        if t.get("error"):
            grade.passed = False
            grade.reasons.insert(0, f"harness error: {t['error']}")
        out.append(
            {
                **t,
                "refused": is_refusal(t["answer"], t["status"]),
                "safety": grade.checks.get("safety", {}),
                "grade": grade.to_dict(),
            }
        )
    return out


def item_filter(items: list[GoldenItem], categories, item_ids) -> list[GoldenItem]:
    out = items
    if categories:
        out = [i for i in out if i.category in categories]
    if item_ids:
        wanted = set(item_ids)
        out = [i for i in out if i.id in wanted]
    return out


# ----------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------


def _make_services(args, settings, config, items, llm, tmp: Path) -> tuple[dict, PiiRedactor]:
    from data_agent.eval.golden import INJECTION_REPLICA
    from data_agent.runtime import build_runtime
    from data_agent.warehouse.replica import replica_file

    services: dict[str, AgentService] = {}
    base = build_runtime(settings, config=config, llm=llm, audit_path=tmp / "audit.sqlite")
    services["masked"] = AgentService(base, checkpoint_path=tmp / "checkpoints.sqlite")
    if any(i.category == "injection" for i in items):
        path = replica_file(settings.replica_path, INJECTION_REPLICA)
        if not path.exists():
            raise SystemExit(
                f"injection test replica missing: {path} (data-agent build-replicas --injections golden/v1/injection.yaml)"
            )
        inj = build_runtime(
            settings,
            config=config,
            llm=llm,
            replica_overrides={"masked": path},
            audit_path=tmp / "audit_injection.sqlite",
        )
        services[INJECTION_REPLICA] = AgentService(
            inj, checkpoint_path=tmp / "checkpoints_injection.sqlite"
        )
    return services, base.redactor


def cmd_eval_run(args) -> int:
    from data_agent.config import AgentConfig, Settings
    from data_agent.eval.report import write_reports

    settings = Settings()
    chosen = getattr(args, "eval_agent_config", None) or getattr(args, "agent_config", None)
    config = AgentConfig.load(Path(chosen) if chosen else settings.agent_config_path)
    golden_dir = Path(args.golden) if args.golden else settings.home / "golden" / "v1"
    all_items = load_golden(golden_dir)
    items = item_filter(
        all_items,
        args.categories.split(",") if args.categories else None,
        args.items.split(",") if args.items else None,
    )
    out_dir = Path(args.out or settings.home / "reports" / "eval" / args.run_name)
    tmp = out_dir / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    if args.llm == "oracle":
        from data_agent.eval.oracle import OracleLLM

        llm = OracleLLM(all_items)
    elif args.llm == "naive":
        from data_agent.eval.oracle import NaiveLLM

        llm = NaiveLLM()
    elif args.llm == "compromised":
        from data_agent.eval.oracle import CompromisedLLM

        llm = CompromisedLLM(all_items)
    else:
        llm = None  # the configured model server
    services, redactor = _make_services(args, settings, config, items, llm, tmp)

    if args.expected == "recompute":
        expected, items = _recompute_expected(items, services)
        expected_meta = {"source": "recomputed on the current replicas"}
    else:
        exp = load_expected(golden_dir / "expected.json")
        expected, expected_meta = (
            exp["items"],
            {k: exp[k] for k in ("golden_version", "frozen_at", "replica_fingerprints")},
        )

    cfg = EvalConfig(
        run_name=args.run_name,
        out_dir=out_dir,
        k=args.k,
        concurrency=min(args.concurrency, 4),
        approver=args.approver,
        notes={"llm": args.llm, "expected": expected_meta},
    )
    started = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        trials = run_eval(cfg, items, services, expected, redactor, resume=not args.fresh)
    finally:
        for svc in services.values():
            svc.close()
            svc.runtime.close()
    model_name = getattr(services["masked"].runtime.llm, "model", "?")
    meta = {
        "run": args.run_name,
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "agent": {
            "name": config.name,
            "version": config.version,
            "fingerprint": config.fingerprint,
            "model": model_name,
            "temperature": config.model.temperature,
            "top_p": config.model.top_p,
            "top_k": config.model.top_k,
        },
        "eval": {
            "k": cfg.k,
            "approver": cfg.approver,
            "concurrency": cfg.concurrency,
            "items": len(items),
            "llm": args.llm,
        },
        "golden": expected_meta,
    }
    summary = write_reports(out_dir, trials, items, meta, config)
    print(
        json.dumps(
            {
                "pass^1": summary["metrics"]["pass_hat_k"]["pass^1"],
                f"pass^{cfg.k}": summary["metrics"]["pass_hat_k"][f"pass^{cfg.k}"],
                "unsafe_executed": summary["metrics"]["safety"]["unsafe_executed"],
                "report": str(out_dir / "summary.md"),
            },
            indent=1,
        )
    )
    return 0


def _recompute_expected(items: list[GoldenItem], services: dict) -> tuple[dict, list[GoldenItem]]:
    """Expected values from the gold SQL on the replicas in use (the CI mini warehouse).

    Items whose gold SQL does not produce gradeable values on this data (for example a stock
    code the synthetic warehouse lacks) are dropped from the run.
    """
    from data_agent.eval.golden import GoldenError, freeze

    expected, kept = {}, []
    for item in items:
        if not item.answerable:
            kept.append(item)
            continue
        svc = services[item.replica]
        tk = svc.runtime.toolkits["analyst"]
        try:
            rec = freeze(
                [item],
                lambda r, sql, tk=tk: tk.guard.pool.get("masked").execute(sql, max_rows=1000),
                tk.guard.validator.validate,
            )
        except (GoldenError, Exception) as e:  # noqa: BLE001
            log.info("dropping %s on this warehouse: %s", item.id, e)
            continue
        expected.update(rec)
        kept.append(item)
    return expected, kept


def register_eval(sub) -> None:
    p = sub.add_parser("eval", help="golden-set evaluation")
    esub = p.add_subparsers(dest="eval_command", required=True)
    r = esub.add_parser("run", help="run the golden set (checkpointed, resumable)")
    r.add_argument("--run-name", required=True)
    r.add_argument("--k", type=int, default=4)
    r.add_argument("--concurrency", type=int, default=4)
    r.add_argument("--approver", choices=["deny", "approve"], default="deny")
    r.add_argument("--llm", choices=["server", "oracle", "naive", "compromised"], default="server")
    r.add_argument("--categories", help="comma-separated categories")
    r.add_argument("--items", help="comma-separated item ids")
    r.add_argument("--golden", help="golden set directory (default golden/v1)")
    r.add_argument("--expected", choices=["frozen", "recompute"], default="frozen")
    r.add_argument("--out", help="output directory (default reports/eval/<run-name>)")
    r.add_argument(
        "--fresh", action="store_true", help="discard recorded trials instead of resuming"
    )
    r.add_argument(
        "--config",
        dest="eval_agent_config",
        help="agent configuration YAML (default: --agent-config or configs/agents/governed-v1.yaml)",
    )
    r.set_defaults(func=cmd_eval_run)

    rg = esub.add_parser("regrade", help="re-grade a run from its trials.jsonl (no model calls)")
    rg.add_argument("--run", required=True)
    rg.add_argument("--k", type=int, default=4)
    rg.add_argument("--golden")
    rg.add_argument("--out")
    rg.set_defaults(func=cmd_eval_regrade)

    from data_agent.eval.judge import register_judge

    register_judge(esub)


def cmd_eval_regrade(args) -> int:
    """Re-grade a finished run from trials.jsonl and rewrite its reports (no model calls)."""
    from data_agent.config import AgentConfig, Settings
    from data_agent.eval.report import write_reports
    from data_agent.runtime import PII_FINGERPRINTS

    settings = Settings()
    out_dir = Path(args.out or settings.home / "reports" / "eval" / args.run)
    golden_dir = Path(args.golden) if args.golden else settings.home / "golden" / "v1"
    items = load_golden(golden_dir)
    exp = load_expected(golden_dir / "expected.json")
    store = TrialStore(out_dir / "trials.jsonl")
    records = store.load()
    redactor = PiiRedactor.load(settings.replica_path / PII_FINGERPRINTS, settings.pii_secret)
    known = {i.id for i in items}
    fresh = iter(
        regrade([r for r in records if r["item_id"] in known], items, exp["items"], redactor)
    )
    regraded_at = datetime.now(UTC).isoformat(timespec="seconds")
    rewritten = []
    for record in records:
        if record["item_id"] not in known:
            rewritten.append(record)
            continue
        new = next(fresh)
        new.setdefault("grade_at_run", record.get("grade_at_run", record.get("grade")))
        new["regraded_at"] = regraded_at
        rewritten.append(new)
    store.rewrite(rewritten)  # the trial log and the reports agree on every grade
    regraded = list({(t["item_id"], t["trial"]): t for t in rewritten}.values())
    summary_path = out_dir / "summary.json"
    meta = (
        json.loads(summary_path.read_text(encoding="utf-8"))["meta"]
        if summary_path.exists()
        else {"run": args.run, "eval": {"k": args.k}}
    )
    meta.setdefault("eval", {})["k"] = meta.get("eval", {}).get("k", args.k)
    meta["regraded_at"] = regraded_at
    config = AgentConfig.load(settings.agent_config_path)
    ids = {t["item_id"] for t in regraded}
    write_reports(out_dir, regraded, [i for i in items if i.id in ids], meta, config)
    print(f"regraded {len(regraded)} trials -> {out_dir / 'summary.md'}")
    return 0
