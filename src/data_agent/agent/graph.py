"""The agent loop as a LangGraph state graph.

    START -> start -> plan --clarify--> clarify (interrupt) -> retrieve
                         |--refuse---------------------------------------> finalize -> END
                         '--answer--> retrieve -> agent <-> tools (guard; interrupt)
                                                   |
                                                   '--> check --untraced numbers--> agent
                                                          '--------------------------> finalize

Human-in-the-loop (`langgraph.types.interrupt`):
  - `clarify`: the planner judged the question ambiguous;
  - `tools`: a run_sql over the soft cost budget, a request for clear customer identifiers,
    a business term with competing metric definitions, or an `ask_user` call.
A node that interrupts re-runs from its start on resume, so everything before the interrupt
is pure: validation, EXPLAIN and metric resolution only read. Execution, query ids and audit
records happen after the interrupt; audit ids are deterministic, so nothing is recorded twice.

Budgets per turn: LLM calls, total tokens, active seconds (human wait time excluded), SQL
queries per question, and the cost/timeout/row caps inside run_sql.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from data_agent.agent.context import fit_context, shrink_payload
from data_agent.agent.state import (
    ANSWERED,
    BUDGET_EXCEEDED,
    ERROR,
    NEEDS_CLARIFICATION,
    REFUSAL_MARKER,
    REFUSED,
    RUNNING,
    AgentState,
    empty_usage,
)
from data_agent.agent.traceability import check_answer
from data_agent.config import AgentConfig
from data_agent.governance.audit import AuditLog
from data_agent.governance.pii import PiiRedactor
from data_agent.llm.client import ChatModel, ChatResponse, LLMError
from data_agent.observability import metrics as m
from data_agent.observability.tracing import (
    Tracer,
    llm_attributes,
    record_llm_response,
    tool_attributes,
)
from data_agent.safety.executor import ExecutionError, ReadOnlyViolation
from data_agent.safety.guard import NEEDS_APPROVAL, REJECT
from data_agent.tools.schemas import tool_schemas
from data_agent.tools.toolkit import ToolError, ToolKit

DISCLOSURE = (
    "AI-generated answer (EU AI Act Art. 50): produced by an automated agent from the sales "
    "warehouse. Check the cited queries before relying on it."
)
# Markdown images and links to external hosts can exfiltrate data when a client renders the
# answer (the URL carries the data). Answers keep no external URL.
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_URL = re.compile(r"\bhttps?://[^\s)\]>\"']+", re.IGNORECASE)


def neutralise_links(text: str) -> tuple[str, int]:
    n = len(_MD_IMAGE.findall(text)) + len(_URL.findall(text))
    text = _MD_IMAGE.sub("[image removed]", text)
    text = _URL.sub("[link removed]", text)
    return text, n


_QUESTION_LIKE = re.compile(
    r"(could you (please )?(clarify|specify|confirm)|which (one|period|metric|definition)|"
    r"do you mean|please (clarify|specify|confirm)|would you like|did you mean)",
    re.IGNORECASE,
)


@dataclass
class AgentDeps:
    config: AgentConfig
    llm: ChatModel
    toolkits: dict[str, ToolKit]  # role -> toolkit
    audit: AuditLog
    tracer: Tracer = field(default_factory=Tracer)
    redactor: PiiRedactor | None = None
    data_window: str = ""
    metric_list: str = ""
    competing_terms: str = ""

    def toolkit(self, role: str) -> ToolKit:
        try:
            return self.toolkits[role]
        except KeyError:
            raise PermissionError(f"no toolkit for role '{role}'") from None


class BudgetExceeded(Exception):
    def __init__(self, budget: str, message: str):
        super().__init__(message)
        self.budget = budget


def _add_usage(
    usage: dict, resp: ChatResponse | None = None, active_s: float = 0.0, sql: int = 0
) -> dict:
    out = dict(usage or empty_usage())
    if resp is not None:
        out["llm_calls"] += 1
        out["prompt_tokens"] += resp.prompt_tokens
        out["completion_tokens"] += resp.completion_tokens
    out["active_s"] = round(out.get("active_s", 0.0) + active_s, 3)
    out["sql_queries"] = out.get("sql_queries", 0) + sql
    return out


def parse_plan(text: str) -> dict:
    """Lenient JSON extraction; anything unparseable means 'answer' (fail open to the loop)."""
    candidates = [text or ""]
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if match:
        candidates.append(match.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            action = str(data.get("action", "answer")).lower().strip()
            if action not in ("answer", "clarify", "refuse"):
                action = "answer"
            options = data.get("options") or []
            return {
                "action": action,
                "refuse_category": str(data.get("refuse_category") or "").strip().lower(),
                "reason": str(data.get("reason", ""))[:500],
                "clarifying_question": str(data.get("clarifying_question", ""))[:500],
                "options": [str(o)[:200] for o in options][:6] if isinstance(options, list) else [],
                "steps": [str(s)[:300] for s in (data.get("steps") or [])][:6]
                if isinstance(data.get("steps"), list)
                else [],
                "parsed": True,
            }
    return {"action": "answer", "reason": "plan not parseable", "steps": [], "parsed": False}


def classify_final(answer: str) -> str:
    text = (answer or "").strip()
    if text.upper().startswith(REFUSAL_MARKER) or REFUSAL_MARKER in text.upper()[:200]:
        return REFUSED
    if text.endswith("?") and _QUESTION_LIKE.search(text):
        return NEEDS_CLARIFICATION
    return ANSWERED


def _normalise_answers(resume: Any) -> dict[str, dict]:
    """Resume payload -> {request_id: answer}. Accepts {"answers": {...}} or a bare dict."""
    if isinstance(resume, dict) and isinstance(resume.get("answers"), dict):
        resume = resume["answers"]
    if not isinstance(resume, dict):
        return {}
    out = {}
    for key, value in resume.items():
        if isinstance(value, bool):
            value = {"approved": value}
        elif isinstance(value, str):
            value = {"reply": value}
        elif not isinstance(value, dict):
            value = {"reply": str(value)}
        out[str(key)] = value
    return out


def build_graph(deps: AgentDeps, checkpointer=None):
    cfg = deps.config
    budgets = cfg.budgets
    tools_spec = tool_schemas(cfg.tools)
    agent_prompt_template = cfg.prompt_text()
    plan_prompt_template = cfg.prompt_text(cfg.plan_prompt) if cfg.use_planner else ""

    # ------------------------------------------------------------------ helpers
    def system_prompt(role: str) -> str:
        tk = deps.toolkit(role)
        return agent_prompt_template.format(
            role=role,
            data_window=deps.data_window,
            catalog_overview=tk.catalog.overview(),
            metric_list=deps.metric_list,
            max_rows=budgets.sql.max_rows,
        )

    def ensure_budget(state: AgentState, needs_llm: bool = True) -> None:
        usage = state.get("usage") or empty_usage()
        if needs_llm and usage["llm_calls"] >= budgets.max_llm_calls:
            raise BudgetExceeded("llm_calls", f"LLM call budget of {budgets.max_llm_calls} reached")
        if usage["prompt_tokens"] + usage["completion_tokens"] >= budgets.max_total_tokens:
            raise BudgetExceeded("tokens", f"token budget of {budgets.max_total_tokens} reached")
        if usage.get("active_s", 0.0) >= budgets.max_wall_s:
            raise BudgetExceeded("latency", f"time budget of {budgets.max_wall_s:.0f}s reached")

    def call_llm(
        messages: list[dict], purpose: str, tools: list[dict] | None = None, json_mode: bool = False
    ) -> ChatResponse:
        attrs = llm_attributes(deps.llm.model, cfg.model.temperature, cfg.model.max_tokens, purpose)
        with deps.tracer.span(
            f"llm.{purpose}", "CHAT_MODEL", inputs={"messages": messages}, attributes=attrs
        ) as span:
            started = time.perf_counter()
            try:
                resp = deps.llm.chat(messages, tools=tools, purpose=purpose, json_mode=json_mode)
            except LLMError as e:
                m.LLM_CALLS.labels(purpose, e.kind).inc()
                span.set_attribute("error.type", e.kind)
                raise
            m.LLM_CALLS.labels(purpose, "ok").inc()
            m.LLM_LATENCY.labels(purpose).observe(time.perf_counter() - started)
            m.LLM_TOKENS.labels("prompt").inc(resp.prompt_tokens)
            m.LLM_TOKENS.labels("completion").inc(resp.completion_tokens)
            record_llm_response(span, resp)
            return resp

    def audit(state: AgentState, suffix: str, kind: str, **kw) -> None:
        deps.audit.record(
            f"{state['thread_id']}:{state.get('turn', 0)}:{suffix}",
            kind,
            thread_id=state["thread_id"],
            user_id=state.get("user_id"),
            role=state.get("role"),
            **kw,
        )

    # ------------------------------------------------------------------ nodes
    def start(state: AgentState) -> dict:
        turn = int(state.get("turn") or 0) + 1
        question = state["question"]
        deps.audit.record(
            f"{state['thread_id']}:{turn}:question",
            "question",
            thread_id=state["thread_id"],
            user_id=state.get("user_id"),
            role=state.get("role"),
            detail={"question": question, "agent": cfg.name, "version": cfg.version},
        )
        return {
            "turn": turn,
            "status": RUNNING,
            "route": "",
            "plan": {},
            "answer": "",
            "provenance": [],
            "sources": [],
            "check": {},
            "check_retries": 0,
            "pii_clear_approved": False,
            "usage": empty_usage(),
            "queries": state.get("queries") or {},
            "charts": state.get("charts") or [],
            "messages": [{"role": "user", "content": question}],
        }

    def plan(state: AgentState) -> dict:
        started = time.perf_counter()
        tk = deps.toolkit(state["role"])
        prompt = plan_prompt_template.format(
            data_window=deps.data_window,
            catalog_overview=tk.catalog.overview(),
            metric_list=deps.metric_list,
            competing_terms=deps.competing_terms,
            refuse_categories=", ".join(cfg.planner_refuse_categories),
        )
        history = [
            f"{msg['role']}: {msg['content']}"
            for msg in state["messages"][:-1]
            if msg["role"] in ("user", "assistant")
            and msg.get("content")
            and not msg.get("tool_calls")
        ][-6:]
        user = (
            state["question"]
            if not history
            else (
                "Conversation so far:\n"
                + "\n".join(history)
                + f"\n\nNew question: {state['question']}"
            )
        )
        try:
            ensure_budget(state)
            resp = call_llm(
                [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
                "plan",
                json_mode=True,
            )
        except BudgetExceeded as e:
            m.BUDGET_EXCEEDED.labels(e.budget).inc()
            return {"status": BUDGET_EXCEEDED, "answer": str(e), "route": "refuse"}
        except LLMError as e:
            return {
                "status": ERROR,
                "answer": f"The model is unavailable ({e.kind}).",
                "route": "refuse",
            }
        p = parse_plan(resp.content)
        usage = _add_usage(state.get("usage"), resp, time.perf_counter() - started)
        allowed = cfg.planner_refuse_categories
        if p["action"] == "refuse" and allowed and p.get("refuse_category") not in allowed:
            # Evidence-based refusal: an unlisted reason is not enough to refuse without
            # looking; the tool loop checks the catalog and may still answer CANNOT ANSWER.
            p["overruled_refusal"] = p.get("reason", "")
            p["action"] = "answer"
        route = {"clarify": "clarify", "refuse": "refuse"}.get(p["action"], "answer")
        if route == "clarify" and not p.get("clarifying_question"):
            route = "answer"  # nothing to ask: let the loop decide
        update: dict = {"plan": p, "usage": usage, "route": route}
        if route == "refuse":
            reason = p.get("reason") or "the warehouse has no data for this question"
            update["answer"] = f"{REFUSAL_MARKER} {reason}"
            update["status"] = REFUSED
            update["messages"] = [{"role": "assistant", "content": update["answer"]}]
        return update

    def clarify(state: AgentState) -> dict:
        p = state.get("plan") or {}
        request = {
            "id": "clarify",
            "kind": "clarification",
            "question": p.get("clarifying_question", ""),
            "options": p.get("options", []),
            "reason": p.get("reason", ""),
        }
        resume = interrupt({"type": "human_input", "requests": [request]})
        answer = _normalise_answers(resume).get("clarify", {})
        reply = str(answer.get("reply") or answer.get("choice") or "").strip()
        event = {
            "type": "clarification",
            "source": "plan",
            "question": request["question"],
            "reply": reply,
        }
        if not reply or answer.get("cancel"):
            return {
                "status": NEEDS_CLARIFICATION,
                "answer": request["question"],
                "route": "finalize",
                "events": [event],
                "messages": [{"role": "assistant", "content": request["question"]}],
            }
        return {
            "route": "retrieve",
            "events": [event],
            "messages": [
                {"role": "assistant", "content": request["question"]},
                {"role": "user", "content": reply},
            ],
        }

    def retrieve(state: AgentState) -> dict:
        started = time.perf_counter()
        tk = deps.toolkit(state["role"])
        steps = " ".join((state.get("plan") or {}).get("steps", []))
        query = f"{state['question']} {steps}".strip()
        call_id = f"ctx_{state.get('turn', 0)}"
        with deps.tracer.span(
            "retrieval.search_catalog", "RETRIEVER", inputs={"query": query}
        ) as span:
            try:
                result = tk.search_catalog(query, k=cfg.retrieval_k)
            except ToolError as e:
                result = {"error": str(e)}
            span.set_outputs(result)
        call = {
            "id": call_id,
            "type": "function",
            "function": {
                "name": "search_catalog",
                "arguments": json.dumps({"query": query, "k": cfg.retrieval_k}),
            },
        }
        return {
            "messages": [
                {"role": "assistant", "content": "", "tool_calls": [call]},
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": "search_catalog",
                    "content": json.dumps(shrink_payload(result)),
                },
            ],
            "usage": _add_usage(state.get("usage"), active_s=time.perf_counter() - started),
        }

    def agent(state: AgentState) -> dict:
        started = time.perf_counter()
        try:
            ensure_budget(state)
            request = fit_context(
                [{"role": "system", "content": system_prompt(state["role"])}, *state["messages"]],
                cfg.model.max_context_tokens,
            )
            resp = call_llm(request, "agent", tools=tools_spec)
        except BudgetExceeded as e:
            m.BUDGET_EXCEEDED.labels(e.budget).inc()
            return {"status": BUDGET_EXCEEDED, "answer": f"Stopped: {e}.", "route": "finalize"}
        except LLMError as e:
            return {
                "status": ERROR,
                "answer": f"The model is unavailable ({e.kind}); no answer was produced.",
                "route": "finalize",
            }
        usage = _add_usage(state.get("usage"), resp, time.perf_counter() - started)
        msg = resp.assistant_message()
        route = "tools" if resp.tool_calls else "check"
        return {"messages": [msg], "usage": usage, "route": route}

    def tools(state: AgentState) -> dict:
        started = time.perf_counter()
        role = state["role"]
        tk = deps.toolkit(role)
        last = state["messages"][-1]
        calls = last.get("tool_calls") or []
        queries = dict(state.get("queries") or {})
        charts = list(state.get("charts") or [])
        usage = dict(state.get("usage") or empty_usage())

        # ---- phase 1: pure preparation (re-runs on resume) -----------------------
        prepared: list[dict] = []
        requests: list[dict] = []
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            cid = call.get("id") or f"call_{len(prepared)}"
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
            except (json.JSONDecodeError, ValueError) as e:
                prepared.append({"id": cid, "name": name, "error": f"invalid arguments: {e}"})
                continue
            item: dict[str, Any] = {"id": cid, "name": name, "args": args}
            if name not in cfg.tools:
                item["error"] = f"unknown tool '{name}'. Available: {', '.join(cfg.tools)}"
            elif name == "run_sql":
                try:
                    decision = tk.check_sql(
                        args.get("sql", ""), bool(args.get("reveal_pii", False))
                    )
                except ToolError as e:
                    item["error"] = str(e)
                else:
                    item["decision"] = decision
                    if decision.decision == NEEDS_APPROVAL:
                        requests.append(
                            {
                                "id": cid,
                                "kind": "approval",
                                "approval_kinds": decision.approval_kinds,
                                "sql": decision.safe_sql,
                                "reasons": decision.reasons,
                                "estimated_rows": round(decision.estimate.max_rows)
                                if decision.estimate
                                else None,
                                "purpose": args.get("purpose", ""),
                            }
                        )
            elif name == "get_metric":
                res = tk.get_metric(args.get("name", ""))
                item["metric"] = res
                if res["status"] == "competing_definitions":
                    requests.append(
                        {
                            "id": cid,
                            "kind": "choose_metric",
                            "term": res["term"],
                            "question": f"'{res['term']}' has competing definitions. Which one do you mean?",
                            "options": res["options"],
                        }
                    )
            elif name == "ask_user":
                requests.append(
                    {
                        "id": cid,
                        "kind": "clarification",
                        "question": str(args.get("question", ""))[:500],
                        "options": [str(o)[:200] for o in (args.get("options") or [])][:6],
                    }
                )
            prepared.append(item)

        # ---- phase 2: one interrupt for every human decision of this step ----------
        answers: dict[str, dict] = {}
        if requests:
            answers = _normalise_answers(interrupt({"type": "human_input", "requests": requests}))

        # ---- phase 3: execute (runs once, after any interrupt) ----------------------
        # `step` makes audit ids unique even when a provider reuses tool-call ids across
        # messages; it is the same when this node re-runs after an interrupt.
        step = len(state["messages"])
        out_messages, events = [], []
        stop_after = False
        for item in prepared:
            cid, name = item["id"], item["name"]
            with deps.tracer.span(
                f"tool.{name}",
                "TOOL",
                inputs=item.get("args"),
                attributes=tool_attributes(name, cid),
            ) as span:
                payload, event = _execute_tool(
                    item, answers.get(cid), tk, queries, charts, state, usage
                )
                span.set_outputs(payload)
            m.TOOL_CALLS.labels(name or "unknown", event.get("status", "ok")).inc()
            if event:
                events.append(event)
                if event.get("type") == "sql":
                    audit(
                        state,
                        f"sql:{step}:{cid}",
                        "sql",
                        tool="run_sql",
                        sql=event.get("sql"),
                        status=event.get("status"),
                        detail={k: v for k, v in event.items() if k not in ("type", "sql")},
                    )
                    if event.get("approval"):
                        audit(
                            state,
                            f"approval:{step}:{cid}",
                            "approval",
                            tool="run_sql",
                            sql=event.get("sql"),
                            status="approved" if event["approval"]["approved"] else "denied",
                            detail=event["approval"],
                        )
                    if event.get("status") == "executed":
                        usage["sql_queries"] = usage.get("sql_queries", 0) + 1
                if event.get("type") == "clarification" and not event.get("reply"):
                    stop_after = True
            out_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": cid,
                    "name": name,
                    "content": json.dumps(shrink_payload(payload), default=str),
                }
            )
        usage["active_s"] = round(usage.get("active_s", 0.0) + time.perf_counter() - started, 3)
        update: dict = {
            "messages": out_messages,
            "queries": queries,
            "charts": charts,
            "events": events,
            "usage": usage,
            "route": "agent",
        }
        if any(e.get("type") == "sql" and e.get("pii_clear") for e in events):
            update["pii_clear_approved"] = True
        if stop_after:  # the user did not answer the clarifying question: end the turn
            question = next((e["question"] for e in events if e.get("type") == "clarification"), "")
            update.update({"status": NEEDS_CLARIFICATION, "answer": question, "route": "finalize"})
        return update

    def _execute_tool(
        item, answer, tk: ToolKit, queries, charts, state, usage
    ) -> tuple[dict, dict]:
        name = item["name"]
        if "error" in item:
            return {"error": item["error"]}, {"type": "tool", "tool": name, "status": "error"}
        args = item["args"]
        try:
            if name == "search_catalog":
                return tk.search_catalog(args.get("query", ""), args.get("k", 6)), {}
            if name == "describe_model":
                return tk.describe_model(args.get("name", "")), {}
            if name == "get_metric":
                res = item["metric"]
                if res["status"] != "competing_definitions":
                    return res, {}
                choice = (answer or {}).get("choice") or (answer or {}).get("reply") or ""
                event = {
                    "type": "clarification",
                    "source": "get_metric",
                    "question": f"choose metric for {res['term']}",
                    "reply": str(choice),
                }
                names = [o["name"] for o in res["options"]]
                picked = next(
                    (n for n in names if n == choice or n.replace("_", " ") in str(choice).lower()),
                    None,
                )
                if picked:
                    return tk.metric_by_name(picked), event
                if choice:
                    return {
                        "status": "user_reply",
                        "term": res["term"],
                        "user_said": str(choice),
                        "options": res["options"],
                    }, event
                return {
                    "status": "no_choice",
                    "message": "The user did not choose a definition. Do not compute this metric.",
                }, event
            if name == "ask_user":
                reply = str(
                    (answer or {}).get("reply") or (answer or {}).get("choice") or ""
                ).strip()
                event = {
                    "type": "clarification",
                    "source": "ask_user",
                    "question": args.get("question", ""),
                    "reply": reply,
                }
                if not reply:
                    return {"status": "no_reply"}, event
                return {"user_reply": reply}, event
            if name == "make_chart":
                query = queries.get(str(args.get("query_id", "")))
                if query is None:
                    raise ToolError(
                        f"unknown query_id '{args.get('query_id')}'. Known: {', '.join(queries) or 'none'}"
                    )
                spec = tk.make_chart(
                    query,
                    args.get("mark", "bar"),
                    args.get("x", ""),
                    args.get("y", ""),
                    args.get("color"),
                    args.get("title"),
                )
                charts.append({"query_id": query["query_id"], "spec": spec})
                return {"chart_id": f"chart{len(charts)}", "spec": spec}, {
                    "type": "chart",
                    "status": "ok",
                }
            if name == "run_sql":
                return _run_sql(item, answer, tk, queries, state, usage)
        except ToolError as e:
            return {"error": str(e)}, {"type": "tool", "tool": name, "status": "error"}
        return {"error": f"tool '{name}' is not implemented"}, {
            "type": "tool",
            "tool": name,
            "status": "error",
        }

    def _run_sql(item, answer, tk: ToolKit, queries, state, usage) -> tuple[dict, dict]:
        decision = item["decision"]
        sql = item["args"].get("sql", "")
        event: dict[str, Any] = {
            "type": "sql",
            "sql": sql,
            "safe_sql": decision.safe_sql,
            "codes": decision.validation.codes if decision.validation else [],
            "unsafe": decision.unsafe,
            "pii_columns": [
                f"{t}.{c}"
                for t, c in (decision.validation.pii_columns if decision.validation else [])
            ],
            "reveal_pii": decision.reveal_pii,
        }
        if decision.estimate is not None:
            event["estimated_rows"] = round(decision.estimate.max_rows)
        if decision.decision == REJECT:
            for code in event["codes"] or ["policy"]:
                m.SQL_REJECTIONS.labels(code).inc()
            event["status"] = "rejected"
            return {"error": "Query rejected: " + " ".join(decision.reasons)}, event
        if usage.get("sql_queries", 0) >= budgets.sql.max_queries:
            m.BUDGET_EXCEEDED.labels("sql_queries").inc()
            event["status"] = "budget"
            return {
                "error": f"Query budget of {budgets.sql.max_queries} per question reached. Answer with what you have."
            }, event
        approved = False
        if decision.decision == NEEDS_APPROVAL:
            approved = bool((answer or {}).get("approved"))
            event["approval"] = {
                "approved": approved,
                "kinds": decision.approval_kinds,
                "by": (answer or {}).get("by", "human"),
                "comment": (answer or {}).get("comment", ""),
            }
            if not approved:
                event["status"] = "denied"
                return {
                    "error": "A human approver denied this query: "
                    + " ".join(decision.reasons)
                    + " Find a cheaper query or answer without it."
                }, event
        started = time.perf_counter()
        try:
            result = tk.execute(decision, approved=approved)
        except ReadOnlyViolation as e:  # the database refused a write: validator bypass
            event["status"] = "read_only_violation"
            event["unsafe"] = True
            return {"error": f"Refused by the database: {e}"}, event
        except ExecutionError as e:
            event["status"] = e.kind
            if e.kind == "timeout":
                m.BUDGET_EXCEEDED.labels("sql_timeout").inc()
            return {"error": f"{e} Fix the query (or make it cheaper) and try again."}, event
        finally:
            m.SQL_LATENCY.observe(time.perf_counter() - started)
        query_id = f"q{len(queries) + 1}"
        queries[query_id] = {
            "query_id": query_id,
            "sql": decision.safe_sql,
            "columns": result.columns,
            "column_types": result.column_types,
            "rows": result.rows,
            "row_count": result.row_count,
            "truncated": result.truncated,
            "elapsed_ms": result.elapsed_ms,
            "purpose": item["args"].get("purpose", ""),
            "pii_clear": bool(decision.reveal_pii and approved),
            "turn": state.get("turn", 0),
        }
        event.update(
            {
                "status": "executed",
                "query_id": query_id,
                "row_count": result.row_count,
                "pii_clear": bool(decision.reveal_pii and approved),
            }
        )
        return tk.result_payload(query_id, result, decision), event

    def check(state: AgentState) -> dict:
        answer = (state["messages"][-1].get("content") or "").strip()
        if not answer:
            if state.get("check_retries", 0) >= budgets.max_check_retries:
                return {
                    "route": "finalize",
                    "status": ERROR,
                    "answer": "The model returned no answer.",
                }
            return {
                "route": "agent",
                "check_retries": state.get("check_retries", 0) + 1,
                "messages": [
                    {"role": "user", "content": "Please give your final answer to the question."}
                ],
            }
        kind = classify_final(answer)
        if kind != ANSWERED:
            return {"route": "finalize", "answer": answer, "status": kind}
        turn_queries = {k: v for k, v in (state.get("queries") or {}).items()}
        result = check_answer(answer, turn_queries, state["question"])
        update: dict = {"check": result.to_dict(), "answer": answer}
        if result.ok or state.get("check_retries", 0) >= budgets.max_check_retries:
            if not result.ok:
                m.UNTRACED_NUMBERS.inc(len(result.untraced))
            update["route"] = "finalize"
            return update
        feedback = (
            "Verification failed: these numbers do not appear in any query result: "
            + ", ".join(result.untraced)
            + ". Compute every number with run_sql (do all arithmetic in SQL) and cite the query id, "
            "or remove the number. Then give the final answer again."
        )
        update.update(
            {
                "route": "agent",
                "check_retries": state.get("check_retries", 0) + 1,
                "messages": [{"role": "user", "content": feedback}],
                "events": [{"type": "check_failed", "untraced": result.untraced}],
            }
        )
        return update

    def finalize(state: AgentState) -> dict:
        status = state.get("status") or RUNNING
        answer = state.get("answer") or ""
        if status == RUNNING:
            answer = answer or (state["messages"][-1].get("content") or "").strip()
            status = classify_final(answer)
        queries = state.get("queries") or {}
        check_result = state.get("check") or {}
        provenance = check_result.get("traced", [])
        cited = {p["query_id"] for p in provenance} | set(re.findall(r"\[(q\d+)\]", answer))
        sources = [
            {
                "query_id": qid,
                "sql": q["sql"],
                "row_count": q["row_count"],
                "purpose": q.get("purpose", ""),
            }
            for qid, q in queries.items()
            if qid in cited and q.get("turn") == state.get("turn")
        ]
        redacted: list[str] = []
        answer, links_removed = neutralise_links(answer)
        if (
            deps.redactor is not None
            and deps.redactor.active
            and not state.get("pii_clear_approved")
        ):
            res = deps.redactor.redact(answer, allow=state["question"])
            answer, redacted = res.text, res.redacted
            if redacted:
                m.PII_REDACTIONS.inc(len(redacted))
        m.QUESTIONS.labels(status, state.get("role", "?")).inc()
        audit(
            state,
            "answer",
            "answer",
            status=status,
            detail={
                "answer": answer,
                "sources": [s["query_id"] for s in sources],
                "untraced": check_result.get("untraced", []),
                "redacted": len(redacted),
                "usage": state.get("usage"),
            },
        )
        update: dict = {
            "status": status,
            "answer": answer,
            "provenance": provenance,
            "sources": sources,
            "route": "",
        }
        last = state["messages"][-1] if state.get("messages") else {}
        if status in (BUDGET_EXCEEDED, ERROR) and last.get("content") != answer:
            update["messages"] = [{"role": "assistant", "content": answer}]
        events = []
        if redacted:
            events.append({"type": "pii_redacted", "count": len(redacted)})
        if links_removed:
            events.append({"type": "links_removed", "count": links_removed})
        if events:
            update["events"] = events
        return update

    # ------------------------------------------------------------------ wiring
    def traced(name, fn):
        def wrapper(state: AgentState):
            with deps.tracer.span(f"node.{name}", "CHAIN", attributes={"data_agent.node": name}):
                return fn(state)

        wrapper.__name__ = name
        return wrapper

    g = StateGraph(AgentState)
    for name, fn in [
        ("start", start),
        ("plan", plan),
        ("clarify", clarify),
        ("retrieve", retrieve),
        ("agent", agent),
        ("tools", tools),
        ("check", check),
        ("finalize", finalize),
    ]:
        g.add_node(name, traced(name, fn))
    g.add_edge(START, "start")
    g.add_conditional_edges(
        "start", lambda s: "plan" if cfg.use_planner else "retrieve", ["plan", "retrieve"]
    )
    g.add_conditional_edges(
        "plan",
        lambda s: s.get("route", "answer"),
        {"clarify": "clarify", "refuse": "finalize", "answer": "retrieve"},
    )
    g.add_conditional_edges(
        "clarify",
        lambda s: s.get("route", "retrieve"),
        {"retrieve": "retrieve", "finalize": "finalize"},
    )
    g.add_edge("retrieve", "agent")
    g.add_conditional_edges(
        "agent",
        lambda s: s.get("route", "check"),
        {"tools": "tools", "check": "check", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "tools", lambda s: s.get("route", "agent"), {"agent": "agent", "finalize": "finalize"}
    )
    g.add_conditional_edges(
        "check", lambda s: s.get("route", "finalize"), {"agent": "agent", "finalize": "finalize"}
    )
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
