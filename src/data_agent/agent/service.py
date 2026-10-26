"""AgentService: ask / resume / inspect threads. The one entry point for CLI, API, MCP, eval.

Threads are persisted by LangGraph's SQLite checkpointer, so a thread interrupted for a human
approval can be resumed after a process restart. A thread belongs to the user (and role)
that created it. Requests on one thread are serialised with a per-thread lock.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from data_agent.agent.graph import DISCLOSURE, AgentDeps, build_graph
from data_agent.agent.state import ERROR
from data_agent.observability import metrics as m
from data_agent.runtime import Runtime

INTERRUPTED = "interrupted"


class ThreadAccessError(PermissionError):
    pass


@dataclass
class TurnResult:
    thread_id: str
    status: str
    answer: str = ""
    interrupt: dict | None = None
    provenance: list[dict] = field(default_factory=list)
    untraced: list[str] = field(default_factory=list)  # numbers no query result backs
    sources: list[dict] = field(default_factory=list)
    charts: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    trace_id: str | None = None
    latency_s: float = 0.0
    disclosure: str = DISCLOSURE

    def to_dict(self) -> dict:
        return asdict(self)


def _competing_terms(runtime: Runtime) -> str:
    return "; ".join(
        f"{term}: {' vs '.join(spec['competing'])}"
        for term, spec in runtime.metrics.terms.items()
        if "competing" in spec
    )


class AgentService:
    def __init__(
        self,
        runtime: Runtime,
        checkpoint_path: Path | str | None = None,
        max_question_chars: int = 2_000,
    ):
        self.runtime = runtime
        self.max_question_chars = max_question_chars  # longer input: refused before any model call
        path = checkpoint_path or runtime.settings.state_path / "checkpoints.sqlite"
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.checkpointer = SqliteSaver(self._conn)
        self.checkpointer.setup()
        metric_list = "; ".join(
            f"{m_.name} ({m_.label})" for m_ in runtime.metrics.metrics.values()
        )
        self.deps = AgentDeps(
            config=runtime.config,
            llm=runtime.llm,
            toolkits=runtime.toolkits,
            audit=runtime.audit,
            tracer=runtime.tracer,
            redactor=runtime.redactor,
            data_window=runtime.metrics.data_window,
            metric_list=metric_list,
            competing_terms=_competing_terms(runtime),
        )
        self.graph = build_graph(self.deps, checkpointer=self.checkpointer)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ API
    def ask(
        self, question: str, user_id: str, role: str, thread_id: str | None = None
    ) -> TurnResult:
        question = (question or "").strip()
        if not question:
            raise ValueError("question is empty")
        limit = self.max_question_chars
        if len(question) > limit:
            raise ValueError(f"question is longer than {limit:,} characters")
        if role not in self.runtime.toolkits:
            raise ThreadAccessError(f"role '{role}' is not available on this server")
        thread_id = thread_id or f"t-{uuid.uuid4().hex[:16]}"
        with self._lock(thread_id):
            existing = self._values(thread_id)
            if existing:
                self._authorise(existing, user_id, role)
            payload: dict[str, Any] = {"question": question}
            if not existing:
                payload.update({"thread_id": thread_id, "user_id": user_id, "role": role})
            return self._run(thread_id, payload, "agent.ask", question, user_id, role)

    def resume(self, thread_id: str, user_id: str, answers: dict) -> TurnResult:
        with self._lock(thread_id):
            values = self._values(thread_id)
            if not values:
                raise KeyError(f"unknown thread '{thread_id}'")
            self._authorise(values, user_id, values.get("role"))
            if not self.pending_interrupt(thread_id):
                raise ValueError(f"thread '{thread_id}' is not waiting for input")
            return self._run(
                thread_id,
                Command(resume={"answers": answers}),
                "agent.resume",
                values.get("question", ""),
                user_id,
                values.get("role", ""),
            )

    def pending_interrupt(self, thread_id: str) -> dict | None:
        snapshot = self.graph.get_state({"configurable": {"thread_id": thread_id}})
        for task in snapshot.tasks or ():
            for intr in getattr(task, "interrupts", ()) or ():
                return {
                    "id": intr.id,
                    **(intr.value if isinstance(intr.value, dict) else {"value": intr.value}),
                }
        return None

    def thread(self, thread_id: str, user_id: str) -> dict:
        values = self._values(thread_id)
        if not values:
            raise KeyError(f"unknown thread '{thread_id}'")
        self._authorise(values, user_id, values.get("role"))
        return {
            "thread_id": thread_id,
            "role": values.get("role"),
            "turn": values.get("turn"),
            "status": values.get("status"),
            "question": values.get("question"),
            "answer": values.get("answer"),
            "queries": [
                {k: q[k] for k in ("query_id", "sql", "row_count", "truncated", "turn")}
                for q in (values.get("queries") or {}).values()
            ],
            "pending": self.pending_interrupt(thread_id),
        }

    # ------------------------------------------------------------------ internals
    def _lock(self, thread_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(thread_id, threading.Lock())

    def _values(self, thread_id: str) -> dict:
        snapshot = self.graph.get_state({"configurable": {"thread_id": thread_id}})
        return dict(snapshot.values or {})

    @staticmethod
    def _authorise(values: dict, user_id: str, role: str | None) -> None:
        if values.get("user_id") != user_id:
            raise ThreadAccessError("this thread belongs to another user")
        if role is not None and values.get("role") != role:
            raise ThreadAccessError("a thread keeps the role it was created with")

    def _run(
        self, thread_id: str, payload, span_name: str, question: str, user_id: str, role: str
    ) -> TurnResult:
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 80}
        tracer = self.runtime.tracer
        before = len(self._values(thread_id).get("events") or [])
        started = time.perf_counter()
        m.INFLIGHT.inc()
        try:
            with tracer.span(
                span_name,
                "AGENT",
                inputs={"question": question, "thread_id": thread_id},
                attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": self.runtime.config.name,
                    "gen_ai.agent.version": self.runtime.config.version,
                    "gen_ai.conversation.id": thread_id,
                    "data_agent.role": role,
                    "data_agent.config_fingerprint": self.runtime.config.fingerprint,
                },
            ) as span:
                tracer.update_trace(
                    metadata={"mlflow.trace.session": thread_id, "mlflow.trace.user": user_id},
                    tags={
                        "agent": self.runtime.config.name,
                        "agent_version": self.runtime.config.version,
                    },
                )
                try:
                    out = self.graph.invoke(payload, config)
                except Exception as e:  # noqa: BLE001 - surface as a failed turn, keep the thread
                    span.set_attribute("error.type", type(e).__name__)
                    result = TurnResult(
                        thread_id, ERROR, answer=f"Internal error: {type(e).__name__}: {e}"
                    )
                    result.latency_s = round(time.perf_counter() - started, 3)
                    return result
                result = self._result(thread_id, out, before)
                span.set_outputs(
                    {
                        "status": result.status,
                        "answer": result.answer,
                        "interrupt": result.interrupt,
                    }
                )
                span.set_attributes(
                    {
                        "data_agent.status": result.status,
                        "gen_ai.usage.input_tokens": result.usage.get("prompt_tokens", 0),
                        "gen_ai.usage.output_tokens": result.usage.get("completion_tokens", 0),
                    }
                )
            result.trace_id = tracer.last_trace_id()
        finally:
            m.INFLIGHT.dec()
        result.latency_s = round(time.perf_counter() - started, 3)
        m.QUESTION_LATENCY.observe(result.latency_s)
        return result

    def _result(self, thread_id: str, out: dict, events_before: int) -> TurnResult:
        values = self._values(thread_id)
        events = (values.get("events") or [])[events_before:]
        interrupts = out.get("__interrupt__") if isinstance(out, dict) else None
        if interrupts:
            intr = interrupts[0]
            payload = intr.value if isinstance(intr.value, dict) else {"value": intr.value}
            for req in payload.get("requests", []):
                m.INTERRUPTS.labels(req.get("kind", "unknown")).inc()
            return TurnResult(
                thread_id,
                INTERRUPTED,
                interrupt={"id": intr.id, **payload},
                usage=values.get("usage") or {},
                events=events,
            )
        turn = values.get("turn")
        charts = [
            c
            for c in values.get("charts") or []
            if (values.get("queries") or {}).get(c.get("query_id"), {}).get("turn") == turn
        ]
        return TurnResult(
            thread_id,
            values.get("status") or ERROR,
            answer=values.get("answer") or "",
            provenance=values.get("provenance") or [],
            untraced=list((values.get("check") or {}).get("untraced") or []),
            sources=values.get("sources") or [],
            charts=charts,
            usage=values.get("usage") or {},
            events=events,
        )
