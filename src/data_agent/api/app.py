"""FastAPI service: ask / resume / inspect threads, health, Prometheus metrics.

    POST /v1/ask                         {"question", "thread_id"?}  -> answer or interrupt
    POST /v1/threads/{id}/resume         {"answers": {request_id: {...}}}
    GET  /v1/threads/{id}                state summary (owner only)
    GET  /v1/threads/{id}/audit          audit events of the thread (owner only)
    GET  /healthz  /readyz  /metrics

Every response carries `X-AI-Generated: true` and the answer body carries a disclosure
(EU AI Act Art. 50). Auth: bearer tokens mapped to (user, role); the role comes from the
token, never from the request. Per-user rate limit; `Idempotency-Key` on POST /v1/ask makes
client retries safe (the same key returns the stored result instead of running again).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from data_agent import __version__
from data_agent.agent.service import AgentService, ThreadAccessError
from data_agent.api.auth import Principal, RateLimiter, TokenStore
from data_agent.config import Settings
from data_agent.observability import metrics as m

log = logging.getLogger(__name__)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")


class ResumeRequest(BaseModel):
    answers: dict[str, dict | bool | str]


class IdempotencyCache:
    def __init__(self, size: int = 1024):
        self.size = size
        self._data: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> dict | None:
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.size:
                self._data.popitem(last=False)


def create_app(
    service: AgentService | None = None, settings: Settings | None = None, shadow=None
) -> FastAPI:
    settings = settings or Settings()
    state: dict = {"service": service, "shadow": shadow}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state["service"] is None:
            from data_agent.runtime import build_runtime

            state["service"] = AgentService(build_runtime(settings))
            state["owned"] = True
            if settings.shadow_config and settings.shadow_rate > 0:
                from data_agent.agent.shadow import ShadowRunner
                from data_agent.config import AgentConfig

                challenger = build_runtime(
                    settings,
                    config=AgentConfig.load(settings.shadow_config),
                    audit_path=settings.state_path / "shadow_audit.sqlite",
                )
                state["shadow"] = ShadowRunner(
                    AgentService(
                        challenger,
                        checkpoint_path=settings.state_path / "shadow_checkpoints.sqlite",
                    ),
                    settings.shadow_rate,
                    settings.state_path / "shadow.jsonl",
                )
        yield
        if state.get("shadow") is not None and state.get("owned"):
            state["shadow"].close(wait=False)
        if state.get("owned"):
            state["service"].runtime.tracer.flush()
            state["service"].close()
            state["service"].runtime.close()

    app = FastAPI(title="Governed Data Agent", version=__version__, lifespan=lifespan)
    tokens = TokenStore.load(settings.tokens_file)
    limiter = RateLimiter(settings.rate_limit_per_minute)
    idem = IdempotencyCache()

    @app.middleware("http")
    async def observe(request: Request, call_next):
        started = time.perf_counter()
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001
            log.exception("unhandled error")
            response = JSONResponse({"detail": "internal error"}, status_code=500)
        route = request.scope.get("route")
        endpoint = getattr(route, "path", "unmatched")
        m.HTTP_REQUESTS.labels(endpoint, request.method, str(response.status_code)).inc()
        m.HTTP_LATENCY.labels(endpoint).observe(time.perf_counter() - started)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-AI-Generated"] = "true"
        return response

    def principal(authorization: str | None = Header(default=None)) -> Principal:
        if settings.auth_disabled:
            return Principal("dev", "analyst")
        token = (
            authorization.split(" ", 1)[1].strip()
            if authorization and authorization.lower().startswith("bearer ")
            else None
        )
        who = tokens.verify(token)
        if who is None:
            raise HTTPException(
                401, "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"}
            )
        if not limiter.allow(who.user):
            m.RATE_LIMITED.inc()
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "10"})
        return who

    def svc() -> AgentService:
        if state["service"] is None:
            raise HTTPException(503, "service not ready")
        return state["service"]

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    def readyz() -> Response:
        s = state["service"]
        if s is None:
            return JSONResponse({"ready": False, "reason": "starting"}, status_code=503)
        return JSONResponse(
            {
                "ready": True,
                "roles": sorted(s.runtime.toolkits),
                "agent": s.runtime.config.name,
                "agent_version": s.runtime.config.version,
            }
        )

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/ask")
    def ask(
        body: AskRequest,
        who: Principal = Depends(principal),
        idempotency_key: str | None = Header(default=None, max_length=128),
    ) -> dict:
        cache_key = f"{who.user}:{idempotency_key}" if idempotency_key else None
        if cache_key and (hit := idem.get(cache_key)) is not None:
            return {**hit, "idempotent_replay": True}
        try:
            turn = svc().ask(body.question, who.user, who.role, body.thread_id)
        except ThreadAccessError as e:
            raise HTTPException(403, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if state.get("shadow") is not None and body.thread_id is None:
            state["shadow"].maybe_submit(
                body.question, who.role, turn
            )  # never affects the response
        result = turn.to_dict()
        if cache_key:
            idem.put(cache_key, result)
        return result

    @app.post("/v1/threads/{thread_id}/resume")
    def resume(thread_id: str, body: ResumeRequest, who: Principal = Depends(principal)) -> dict:
        try:
            return svc().resume(thread_id, who.user, body.answers).to_dict()
        except ThreadAccessError as e:
            raise HTTPException(403, str(e)) from e
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/v1/threads/{thread_id}")
    def thread(thread_id: str, who: Principal = Depends(principal)) -> dict:
        try:
            return svc().thread(thread_id, who.user)
        except ThreadAccessError as e:
            raise HTTPException(403, str(e)) from e
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/v1/threads/{thread_id}/audit")
    def audit(thread_id: str, who: Principal = Depends(principal)) -> dict:
        try:
            svc().thread(thread_id, who.user)  # ownership check
        except ThreadAccessError as e:
            raise HTTPException(403, str(e)) from e
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        return {"thread_id": thread_id, "events": svc().runtime.audit.events(thread_id)}

    return app
