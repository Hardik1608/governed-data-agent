"""The v1 single-shot text-to-SQL pipeline, as the baseline the agent is compared with.

Ported from text-to-sql-llm-platform v1 (MIT, same author): `src/generation/prompts.py`
(system prompt and "prompt" mode layout), `src/generation/postprocess.py` (SQL extraction)
and `src/pipeline.py` (validate -> one repair retry with the error -> execute). Same model and
same safety layer as the agent; only the control flow differs: one generation (plus at most
one repair), no tools, no exploration of the data.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from data_agent.bench.sqlite_db import SqliteDb, SqliteGuard
from data_agent.llm.client import ChatModel, LLMError
from data_agent.safety.executor import ExecutionError

CANNOT_ANSWER = "CANNOT_ANSWER"
SYSTEM_PROMPT = """You are an expert SQL generator. Given a natural language question and the relevant database schema (CREATE TABLE statements), generate a valid, executable SQLite query.

Rules:
- Output ONLY the SQL query. No explanation, no markdown code fences.
- Use only tables and columns that appear in the provided schema.
- Generate read-only SELECT queries. Never use INSERT, UPDATE, DELETE, DROP, or CREATE.
- If you cannot answer the question from the schema, output: CANNOT_ANSWER
"""

_FENCE = re.compile(r"```(?:sql|sqlite)?\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL)
_PREFIX = re.compile(r"^\s*(?:sql\s*query|sqlquery|sql|answer|query)\s*:\s*", re.IGNORECASE)
_SQL_START = re.compile(r"\b(SELECT|WITH)\b", re.IGNORECASE)
_PROSE_LINE = re.compile(
    r"^\s*(explanation|note|this query|the query|here is|here's|in this query)\b", re.IGNORECASE
)


def extract_sql(text: str) -> str:
    """v1: best-effort extraction of one SQL statement; never splits on ';' (an injected
    second statement must stay visible so the validator rejects it)."""
    if not text:
        return ""
    fenced = _FENCE.findall(text)
    candidate = fenced[-1] if fenced else text
    candidate = _PREFIX.sub("", candidate.strip())
    start = _SQL_START.search(candidate)
    if start is None:
        return CANNOT_ANSWER if CANNOT_ANSWER in text else candidate.strip()
    candidate = candidate[start.start() :]
    lines = []
    for line in candidate.splitlines():
        if not line.strip() or _PROSE_LINE.match(line):
            break
        lines.append(line.rstrip())
    sql = "\n".join(lines).strip().rstrip("`").strip()
    while sql.endswith(";"):
        sql = sql[:-1].rstrip()
    return sql


def build_messages(db: SqliteDb, question: str, evidence: str) -> list[dict]:
    hint = f"\n\n-- External knowledge: {evidence}" if evidence else ""
    user = f"Database Schema:\n{db.ddl()}{hint}\n\nQuestion: {question}\nSQL:"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


@dataclass
class SingleShotResult:
    sql: str
    status: str  # ok | rejected | exec_error | llm_error | cannot_answer
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    repaired: bool = False
    latency_s: float = 0.0
    error: str | None = None


def run_single_shot(
    llm: ChatModel,
    db: SqliteDb,
    guard: SqliteGuard,
    question: str,
    evidence: str,
    max_repairs: int = 1,
    max_tokens: int = 512,
) -> SingleShotResult:
    started = time.perf_counter()
    messages = build_messages(db, question, evidence)
    result = SingleShotResult(sql="", status="llm_error")
    attempts = 0
    while True:
        try:
            gen = llm.chat(messages, purpose="baseline", temperature=0.0, max_tokens=max_tokens)
        except LLMError as e:
            result.status, result.error = "llm_error", str(e)
            break
        result.llm_calls += 1
        result.prompt_tokens += gen.prompt_tokens
        result.completion_tokens += gen.completion_tokens
        sql = extract_sql(gen.content)
        result.sql = sql
        if sql == CANNOT_ANSWER:
            result.status = "cannot_answer"
            break
        decision = guard.check(sql)
        problem = None
        if decision.decision != "allow":
            problem = " ".join(decision.reasons)
            result.status = "rejected"
            if decision.unsafe:
                break  # v1: never "repair" an unsafe statement
        else:
            try:
                guard.execute(decision, max_rows=5)
                result.status = "ok"
                break
            except ExecutionError as e:
                problem, result.status = str(e), "exec_error"
        if attempts >= max_repairs or problem is None:
            break
        attempts += 1
        result.repaired = True
        messages = messages + [
            {"role": "assistant", "content": gen.content},
            {
                "role": "user",
                "content": f"The SQL query above is invalid: {problem}\nUsing only the tables and columns in the schema, write a corrected SQLite query. Output ONLY the SQL.",
            },
        ]
    result.latency_s = round(time.perf_counter() - started, 3)
    return result
