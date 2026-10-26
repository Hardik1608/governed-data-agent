"""Shadow traffic for a challenger agent configuration (the MLOps project's pattern).

After the champion answers a question, a sampled share of questions is replayed in a
background thread against the challenger configuration (DATA_AGENT_SHADOW_CONFIG). The
shadow run never reaches the user: approvals are auto-denied, clarifying questions end its
turn, and its answer is only compared with the champion's (same decision? same numbers?) and
logged (data/state/shadow.jsonl) with its tokens and latency, plus Prometheus counters.
This is how a challenger that passed the offline gate is measured on live questions before
anyone promotes it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from prometheus_client import Counter, Histogram

from data_agent.agent.service import INTERRUPTED, AgentService, TurnResult
from data_agent.agent.traceability import extract_numbers

log = logging.getLogger(__name__)

SHADOW_RUNS = Counter("data_agent_shadow_runs_total", "Shadow (challenger) runs", ["outcome"])
SHADOW_LATENCY = Histogram(
    "data_agent_shadow_seconds", "Shadow run latency", buckets=(1, 2, 5, 10, 20, 30, 60, 120, 300)
)


def _numbers(text: str) -> set[float]:
    return {round(m.magnitude, 2) for m in extract_numbers(text or "")}


def compare(champion: TurnResult, shadow: TurnResult) -> dict:
    a, b = _numbers(champion.answer), _numbers(shadow.answer)
    overlap = len(a & b) / len(a | b) if (a | b) else 1.0
    return {
        "same_status": champion.status == shadow.status,
        "number_jaccard": round(overlap, 3),
        "agree": champion.status == shadow.status and overlap >= 0.5,
    }


class ShadowRunner:
    def __init__(
        self,
        service: AgentService,
        rate: float,
        log_path: Path,
        max_workers: int = 1,
        seed: int | None = None,
    ):
        self.service = service
        self.rate = max(0.0, min(1.0, rate))
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="shadow")
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

    def maybe_submit(self, question: str, role: str, champion: TurnResult) -> bool:
        if champion.status == INTERRUPTED or self._rng.random() >= self.rate:
            return False
        self._pool.submit(self._run, question, role, champion)
        return True

    def _run(self, question: str, role: str, champion: TurnResult) -> None:
        started = time.perf_counter()
        outcome = "error"
        record: dict = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "question_sha": hashlib.sha256(question.encode()).hexdigest()[:16],
            "role": role,
            "champion_thread": champion.thread_id,
            "champion_status": champion.status,
            "challenger": f"{self.service.runtime.config.name} v{self.service.runtime.config.version}",
        }
        try:
            result = self.service.ask(question, user_id="shadow", role=role)
            rounds = 0
            while result.status == INTERRUPTED and rounds < 3:  # never wait for a human
                answers = {
                    r["id"]: ({"approved": False} if r["kind"] == "approval" else {"reply": ""})
                    for r in result.interrupt["requests"]
                }
                result = self.service.resume(result.thread_id, "shadow", answers)
                rounds += 1
            cmp = compare(champion, result)
            outcome = "agree" if cmp["agree"] else "disagree"
            record.update(
                {
                    "shadow_status": result.status,
                    "shadow_tokens": result.usage.get("prompt_tokens", 0)
                    + result.usage.get("completion_tokens", 0),
                    "champion_tokens": champion.usage.get("prompt_tokens", 0)
                    + champion.usage.get("completion_tokens", 0),
                    **cmp,
                }
            )
        except Exception as e:  # noqa: BLE001 - the shadow must never affect serving
            record["error"] = f"{type(e).__name__}: {e}"
            log.warning("shadow run failed: %s", e)
        elapsed = time.perf_counter() - started
        record["shadow_latency_s"] = round(elapsed, 3)
        SHADOW_RUNS.labels(outcome).inc()
        SHADOW_LATENCY.observe(elapsed)
        with self._lock, self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def close(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)
