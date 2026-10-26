"""Graph state. Plain JSON-serialisable data only, so the SQLite checkpointer can store it."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

# Final statuses of a turn
ANSWERED = "answered"
REFUSED = "refused"
NEEDS_CLARIFICATION = "needs_clarification"
BUDGET_EXCEEDED = "budget_exceeded"
ERROR = "error"
RUNNING = "running"

REFUSAL_MARKER = "CANNOT ANSWER:"


class AgentState(TypedDict, total=False):
    # identity (fixed for the thread)
    thread_id: str
    user_id: str
    role: str
    # the current turn
    question: str
    turn: int
    status: str
    route: str
    plan: dict[str, Any]
    answer: str
    provenance: list[dict]
    sources: list[dict]
    check: dict[str, Any]
    check_retries: int
    usage: dict[str, Any]
    pii_clear_approved: bool
    # accumulated over the thread
    messages: Annotated[list[dict], operator.add]
    queries: dict[str, dict]
    charts: list[dict]
    events: Annotated[list[dict], operator.add]


def empty_usage() -> dict[str, Any]:
    return {
        "llm_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "active_s": 0.0,
        "sql_queries": 0,
    }
