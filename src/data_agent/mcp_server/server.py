"""MCP server (official Python SDK v2, protocol revision 2026-07-28).

Exposes the governed tools to any MCP host (Claude Desktop, IDEs, other agents):
  search_catalog, describe_model, get_metric, run_sql, make_chart  - the agent's own tools,
      with the same role policy, validator, replica, budgets and audit log;
  ask_data_agent / resume_data_agent - the full LangGraph agent. Its human-in-the-loop
      interrupts (approvals, metric choice, clarification) become MCP elicitation requests:
      on a 2026-07-28 connection the tool returns an `InputRequiredResult` (multi-round-trip:
      the client asks the user and retries the call with the answers; the thread id travels
      in the SDK-sealed `request_state`); on a legacy connection the server elicits over the
      back-channel; a client without elicitation gets `input_required` and a thread id to
      resume. `ask_user` is therefore MCP-native here.
  resources: catalog://models and catalog://models/{name}

Transports:
  - stdio: a local process the host launches; it runs as the local user, so the role comes
    from DATA_AGENT_MCP_ROLE (default analyst). stdio has no transport authentication.
  - Streamable HTTP: an OAuth 2.1 *resource server*. Bearer tokens are verified by
    `StaticTokenVerifier` (hashes in DATA_AGENT_TOKENS_FILE -> user + role scope) and the
    server publishes RFC 9728 protected-resource metadata pointing at the authorisation
    server (DATA_AGENT_MCP_ISSUER_URL). Running a full authorisation server (dynamic client
    registration, consent, token issuance) is out of scope; in production the verifier
    would validate JWTs from the company IdP.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections import OrderedDict
from typing import Any

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    ToolAnnotations,
)
from pydantic import BaseModel, Field

from data_agent import __version__
from data_agent.agent.context import shrink_payload
from data_agent.agent.service import INTERRUPTED, AgentService
from data_agent.api.auth import Principal, TokenStore
from data_agent.safety.executor import ExecutionError
from data_agent.safety.guard import NEEDS_APPROVAL, REJECT
from data_agent.tools.toolkit import ToolError, ToolKit

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
INSTRUCTIONS = (
    "Governed access to an online retailer's sales warehouse (UK, GBP, Dec 2009 - Dec 2011). "
    "Use search_catalog and describe_model to find data, get_metric for certified metric "
    "definitions, run_sql for read-only DuckDB SELECTs (customers appear as tokens), or "
    "ask_data_agent to let the governed agent answer a business question end to end."
)


class Approval(BaseModel):
    approved: bool = Field(description="Approve this request?")
    comment: str = Field(default="", description="Optional note for the audit log")


class Reply(BaseModel):
    reply: str = Field(description="Your answer")


MRTR_REVISION = "2026-07-28"  # protocol revision with multi-round-trip requests

_APPROVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean", "description": "Approve this request?"},
        "comment": {"type": "string", "description": "Optional note for the audit log"},
    },
    "required": ["approved"],
}
_REPLY_SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "string", "description": "Your answer"}},
    "required": ["reply"],
}


def _mrtr(ctx: Context) -> bool:
    return (ctx.protocol_version or "") >= MRTR_REVISION


def _request_text(req: dict) -> str:
    if req.get("kind") == "approval":
        return f"Approve this query? {' '.join(req.get('reasons', []))}\n\n{req.get('sql')}"
    options = req.get("options") or []
    labels = [o["name"] if isinstance(o, dict) else str(o) for o in options]
    return req.get("question", "Please clarify") + (
        f" Options: {', '.join(labels)}" if labels else ""
    )


def _elicit_request(req: dict) -> ElicitRequest:
    schema = _APPROVAL_SCHEMA if req.get("kind") == "approval" else _REPLY_SCHEMA
    return ElicitRequest(
        params=ElicitRequestFormParams(message=_request_text(req), requested_schema=schema)
    )


def _answers_from(requests: list[dict], responses: dict, user: str) -> dict[str, dict]:
    answers: dict[str, dict] = {}
    for req in requests:
        res = (responses or {}).get(req["id"])
        accepted = (
            isinstance(res, ElicitResult) and res.action == "accept" and res.content is not None
        )
        content = res.content if accepted else {}
        if req.get("kind") == "approval":
            answers[req["id"]] = {
                "approved": bool(content.get("approved")),
                "by": f"{user} (mcp)",
                "comment": str(content.get("comment") or ""),
            }
        else:
            reply = str(content.get("reply") or "")
            answers[req["id"]] = {"reply": reply, "choice": reply}
    return answers


class StaticTokenVerifier:
    """Resource-server token check against hashed bearer tokens (dev / single-tenant)."""

    def __init__(self, store: TokenStore, resource: str):
        self.store = store
        self.resource = resource

    async def verify_token(self, token: str) -> AccessToken | None:
        who = self.store.verify(token)
        if who is None:
            return None
        return AccessToken(
            token=token,
            client_id=who.user,
            subject=who.user,
            scopes=[f"role:{who.role}"],
            resource=self.resource,
        )


class ResultCache:
    """Recent run_sql results, so make_chart can reference them by query id."""

    def __init__(self, size: int = 256):
        self.size = size
        self._data: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, owner: str, result: dict) -> str:
        qid = f"m{uuid.uuid4().hex[:10]}"
        with self._lock:
            self._data[qid] = {**result, "query_id": qid, "_owner": owner}
            while len(self._data) > self.size:
                self._data.popitem(last=False)
        return qid

    def get(self, owner: str, qid: str) -> dict | None:
        with self._lock:
            item = self._data.get(qid)
        return item if item and item["_owner"] == owner else None


def build_server(
    service: AgentService,
    stdio_role: str = "analyst",
    tokens: TokenStore | None = None,
    resource_url: str | None = None,
    issuer_url: str | None = None,
    allowed_hosts: list[str] | None = None,
) -> MCPServer:
    runtime = service.runtime
    cache = ResultCache()
    http_auth = tokens is not None and resource_url is not None
    kwargs: dict[str, Any] = {}
    if http_auth:
        kwargs["token_verifier"] = StaticTokenVerifier(tokens, resource_url)
        kwargs["auth"] = AuthSettings(
            issuer_url=issuer_url or "https://auth.example.invalid",
            resource_server_url=resource_url,
            validate_token_resource=True,
        )
    mcp = MCPServer(
        "governed-data-agent",
        title="Governed Data Agent",
        instructions=INSTRUCTIONS,
        version=__version__,
        **kwargs,
    )
    mcp._data_agent_hosts = allowed_hosts or []  # used by run()

    def principal() -> Principal:
        token = get_access_token()
        if token is None:  # stdio (or in-process): the local user, role from config
            return Principal(os.environ.get("DATA_AGENT_MCP_USER", "mcp-local"), stdio_role)
        role = next((s.split(":", 1)[1] for s in token.scopes if s.startswith("role:")), "viewer")
        return Principal(token.subject or token.client_id, role)

    def toolkit(who: Principal) -> ToolKit:
        tk = runtime.toolkits.get(who.role)
        if tk is None:
            raise ToolError(f"role '{who.role}' is not available on this server")
        return tk

    def audit(who: Principal, kind: str, **kw) -> None:
        runtime.audit.record(
            f"mcp:{uuid.uuid4().hex}",
            kind,
            user_id=who.user,
            role=who.role,
            tool=kw.pop("tool", None),
            **kw,
        )

    async def elicit_supported(ctx: Context) -> bool:
        caps = ctx.client_capabilities
        return bool(caps and getattr(caps, "elicitation", None))

    # ------------------------------------------------------------------ tools
    @mcp.tool(annotations=READ_ONLY)
    def search_catalog(query: str, k: int = 6) -> dict:
        """Search the warehouse catalog (dbt docs, columns, certified metrics), hybrid BM25 + embeddings."""
        return toolkit(principal()).search_catalog(query, k)

    @mcp.tool(annotations=READ_ONLY)
    def describe_model(name: str) -> dict:
        """Documentation of one model: grain, columns, types, tests, PII handling."""
        return toolkit(principal()).describe_model(name)

    @mcp.tool(annotations=READ_ONLY)
    def get_metric(name: str) -> dict:
        """Certified definition and SQL of a business metric or term (e.g. 'revenue', 'aov')."""
        return toolkit(principal()).get_metric(name)

    @mcp.tool(annotations=READ_ONLY)
    async def run_sql(sql: str, ctx: Context, purpose: str = "") -> dict | InputRequiredResult:
        """Run one read-only DuckDB SELECT under the caller's role. Expensive queries need approval."""
        who = principal()
        tk = toolkit(who)
        decision = await anyio.to_thread.run_sync(tk.check_sql, sql)
        if decision.decision == REJECT:
            audit(
                who,
                "sql",
                tool="run_sql",
                sql=sql,
                status="rejected",
                detail={"reasons": decision.reasons, "unsafe": decision.unsafe},
            )
            return {"error": "Query rejected: " + " ".join(decision.reasons)}
        approved = False
        if decision.decision == NEEDS_APPROVAL:
            req = {
                "id": "approve",
                "kind": "approval",
                "reasons": decision.reasons,
                "sql": decision.safe_sql,
            }
            if _mrtr(ctx):
                if (
                    ctx.request_state is None
                ):  # first round: ask, the client retries with the answer
                    return InputRequiredResult(
                        input_requests={"approve": _elicit_request(req)}, request_state="run_sql"
                    )
                approved = _answers_from([req], ctx.input_responses or {}, who.user)["approve"][
                    "approved"
                ]
            elif await elicit_supported(ctx):
                answer = await ctx.elicit(_request_text(req), Approval)
                approved = answer.action == "accept" and bool(answer.data.approved)
            else:
                return {
                    "error": "This query needs human approval: "
                    + " ".join(decision.reasons)
                    + " Use ask_data_agent, or make it cheaper."
                }
            audit(
                who,
                "approval",
                tool="run_sql",
                sql=decision.safe_sql,
                status="approved" if approved else "denied",
                detail={"via": "mcp_elicitation"},
            )
            if not approved:
                return {"error": "The approver denied this query."}
        try:
            result = await anyio.to_thread.run_sync(lambda: tk.execute(decision, approved=approved))
        except ExecutionError as e:
            audit(who, "sql", tool="run_sql", sql=decision.safe_sql, status=e.kind)
            return {"error": str(e)}
        payload = tk.result_payload("", result, decision)
        qid = cache.put(
            who.user,
            {"columns": result.columns, "rows": result.rows, "column_types": result.column_types},
        )
        payload["query_id"] = qid
        audit(
            who,
            "sql",
            tool="run_sql",
            sql=decision.safe_sql,
            status="executed",
            detail={"query_id": qid, "rows": result.row_count, "purpose": purpose},
        )
        return shrink_payload(payload)

    @mcp.tool(annotations=READ_ONLY)
    def make_chart(
        query_id: str, mark: str, x: str, y: str, color: str | None = None, title: str | None = None
    ) -> dict:
        """Vega-Lite spec from an earlier run_sql result (inline data only)."""
        who = principal()
        query = cache.get(who.user, query_id)
        if query is None:
            raise ToolError(f"unknown query_id '{query_id}' (results are kept for this user only)")
        return toolkit(who).make_chart(query, mark, x, y, color, title)

    def _out(result) -> dict:
        out = {
            k: v
            for k, v in result.to_dict().items()
            if k
            in (
                "thread_id",
                "status",
                "answer",
                "sources",
                "provenance",
                "untraced",
                "usage",
                "disclosure",
            )
        }
        if result.status == INTERRUPTED:
            out["status"] = "input_required"
            out["requests"] = result.interrupt.get("requests", [])
            out["how_to_resume"] = "call resume_data_agent(thread_id, answers={request_id: {...}})"
        return out

    async def _drive(result, who: Principal, ctx: Context) -> dict | InputRequiredResult:
        """Turn the agent's interrupts into elicitation, in the way the connection supports."""
        if result.status == INTERRUPTED and _mrtr(ctx):
            requests = result.interrupt.get("requests", [])
            return InputRequiredResult(
                input_requests={r["id"]: _elicit_request(r) for r in requests},
                request_state=json.dumps({"thread_id": result.thread_id}),  # sealed by the SDK
            )
        rounds = 0
        while result.status == INTERRUPTED and await elicit_supported(ctx) and rounds < 5:
            rounds += 1  # legacy connection: elicit over the back-channel
            answers: dict[str, dict] = {}
            for req in result.interrupt.get("requests", []):
                if req["kind"] == "approval":
                    ans = await ctx.elicit(_request_text(req), Approval)
                    answers[req["id"]] = {
                        "approved": ans.action == "accept" and bool(ans.data.approved),
                        "by": f"{who.user} (mcp)",
                    }
                else:
                    ans = await ctx.elicit(_request_text(req), Reply)
                    reply = ans.data.reply if ans.action == "accept" else ""
                    answers[req["id"]] = {"reply": reply, "choice": reply}
            tid = result.thread_id
            result = await anyio.to_thread.run_sync(
                lambda a=answers, t=tid: service.resume(t, who.user, a)
            )
        return _out(result)

    async def _continue(ctx: Context, who: Principal):
        """A retry carrying elicitation answers: resume the thread named in request_state."""
        state = json.loads(ctx.request_state)
        tid = state["thread_id"]
        pending = await anyio.to_thread.run_sync(lambda: service.pending_interrupt(tid))
        if pending is None:
            return None
        answers = _answers_from(pending.get("requests", []), ctx.input_responses or {}, who.user)
        return await anyio.to_thread.run_sync(lambda: service.resume(tid, who.user, answers))

    @mcp.tool()
    async def ask_data_agent(
        question: str, ctx: Context, thread_id: str | None = None
    ) -> dict | InputRequiredResult:
        """Ask the governed data agent a business question (multi-step, cited, audited)."""
        who = principal()
        result = await _continue(ctx, who) if ctx.request_state else None
        if result is None:
            result = await anyio.to_thread.run_sync(
                lambda: service.ask(question, who.user, who.role, thread_id)
            )
        return await _drive(result, who, ctx)

    @mcp.tool()
    async def resume_data_agent(
        thread_id: str, answers: dict[str, Any], ctx: Context
    ) -> dict | InputRequiredResult:
        """Resume an agent thread that returned input_required, e.g. {"call_1": {"approved": true}}."""
        who = principal()
        result = await _continue(ctx, who) if ctx.request_state else None
        if result is None:
            result = await anyio.to_thread.run_sync(
                lambda: service.resume(thread_id, who.user, answers)
            )
        return await _drive(result, who, ctx)

    # ------------------------------------------------------------------ resources
    @mcp.resource("catalog://models")
    def list_models() -> str:
        """The models available to the caller's role, one line each."""
        return toolkit(principal()).catalog.overview()

    @mcp.resource("catalog://models/{name}")
    def model_doc(name: str) -> str:
        """Full documentation of one model."""
        return toolkit(principal()).describe_model(name)["documentation"]

    return mcp


def run(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765) -> None:
    from data_agent.config import Settings
    from data_agent.runtime import build_runtime

    settings = Settings()
    service = AgentService(build_runtime(settings))
    try:
        if transport == "stdio":
            server = build_server(
                service, stdio_role=os.environ.get("DATA_AGENT_MCP_ROLE", "analyst")
            )
            server.run("stdio")
            return
        public = os.environ.get("DATA_AGENT_MCP_PUBLIC_URL", f"http://{host}:{port}/mcp")
        tokens = TokenStore.load(settings.tokens_file)
        if not len(tokens):
            raise SystemExit("HTTP transport needs bearer tokens: set DATA_AGENT_TOKENS_FILE")
        hosts = [
            h.strip()
            for h in os.environ.get("DATA_AGENT_MCP_ALLOWED_HOSTS", "").split(",")
            if h.strip()
        ]
        server = build_server(
            service,
            tokens=tokens,
            resource_url=public,
            issuer_url=os.environ.get("DATA_AGENT_MCP_ISSUER_URL"),
            allowed_hosts=hosts,
        )
        security = None
        if hosts:
            security = TransportSecuritySettings(
                allowed_hosts=hosts, allowed_origins=[f"http://{h}" for h in hosts]
            )
        server.run("streamable-http", host=host, port=port, transport_security=security)
    finally:
        service.close()
        service.runtime.close()
