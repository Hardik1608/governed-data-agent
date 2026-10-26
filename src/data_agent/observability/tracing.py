"""MLflow Tracing with OpenTelemetry GenAI semantic-convention attributes.

One trace per question:
    agent.ask (AGENT, gen_ai.operation.name=invoke_agent)
      ├─ node.plan / node.retrieve / node.agent / node.tools / node.check / node.finalize
      │    ├─ llm.chat (CHAT_MODEL): model, tokens in/out, latency, finish reason
      │    ├─ tool.<name> (TOOL, gen_ai.operation.name=execute_tool, gen_ai.tool.name, call id)
      │    └─ retrieval.search_catalog (RETRIEVER)
Token usage is written both as MLflow's `mlflow.chat.tokenUsage` (the UI sums it per trace)
and as `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens`. With
`MLFLOW_ENABLE_OTEL_GENAI_SEMCONV=true` and `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` set, MLflow
also exports the spans over OTLP in the GenAI semantic conventions (MLflow >= 3.11).

Tracing is optional: `DATA_AGENT_TRACING=off` (the default in tests) makes every call a
no-op, and a tracing failure never fails a request.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Iterator
from typing import Any

log = logging.getLogger(__name__)

GEN_AI_PROVIDER = "openai"  # the wire protocol: any OpenAI-compatible server
_CONTROL_FLOW = frozenset({"GraphInterrupt", "NodeInterrupt", "ParentCommand", "GraphBubbleUp"})


class _NoopSpan:
    def set_inputs(self, *_: Any, **__: Any) -> None: ...
    def set_outputs(self, *_: Any, **__: Any) -> None: ...
    def set_attribute(self, *_: Any, **__: Any) -> None: ...
    def set_attributes(self, *_: Any, **__: Any) -> None: ...
    def set_status(self, *_: Any, **__: Any) -> None: ...
    def record_exception(self, *_: Any, **__: Any) -> None: ...

    @property
    def trace_id(self) -> str | None:
        return None


_FAST_FAIL = {"MLFLOW_HTTP_REQUEST_MAX_RETRIES": "0", "MLFLOW_HTTP_REQUEST_TIMEOUT": "5"}


def _set_experiment(mlflow, experiment: str, wait_s: float) -> None:
    """Point MLflow at the experiment, waiting at most `wait_s` for the tracking server.

    MLflow's own HTTP retries back off for minutes; a tracking server that is still starting
    (or down) must not hold up the service, so each attempt fails fast and the whole wait is
    bounded. The caller disables tracing when this raises.
    """
    saved = {k: os.environ.get(k) for k in _FAST_FAIL}
    os.environ.update(_FAST_FAIL)
    deadline = time.monotonic() + wait_s
    try:
        while True:
            try:
                mlflow.set_experiment(experiment)
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class Tracer:
    def __init__(
        self, enabled: bool = False, experiment: str | None = None, wait_s: float | None = None
    ):
        self.enabled = False
        self._mlflow = None
        if not enabled:
            return
        if wait_s is None:
            wait_s = float(os.environ.get("DATA_AGENT_TRACING_WAIT_S", "45"))
        try:
            os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
            import mlflow

            if experiment:
                _set_experiment(mlflow, experiment, wait_s)
            self._mlflow = mlflow
            self.enabled = True
        except Exception as e:  # noqa: BLE001 - tracing must never break serving
            log.warning("tracing disabled (tracking server unavailable): %s", str(e)[:200])

    @contextlib.contextmanager
    def span(
        self,
        name: str,
        span_type: str = "CHAIN",
        inputs: Any = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        if not self.enabled:
            yield _NoopSpan()
            return
        try:
            cm = self._mlflow.start_span(
                name=name, span_type=span_type, attributes=attributes or {}
            )
            span = cm.__enter__()
        except Exception as e:  # noqa: BLE001
            log.debug("span start failed: %s", e)
            yield _NoopSpan()
            return
        if inputs is not None:
            _safe(span.set_inputs, inputs)
        try:
            yield span
        except BaseException as exc:
            if type(exc).__name__ in _CONTROL_FLOW:  # a LangGraph interrupt is not an error
                _safe(span.set_attribute, "data_agent.interrupted", True)
                cm.__exit__(None, None, None)
            else:
                cm.__exit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            cm.__exit__(None, None, None)

    def update_trace(
        self, tags: dict[str, str] | None = None, metadata: dict[str, str] | None = None
    ) -> None:
        if not self.enabled:
            return
        _safe(self._mlflow.update_current_trace, tags=tags or {}, metadata=metadata or {})

    def last_trace_id(self) -> str | None:
        if not self.enabled:
            return None
        try:
            return self._mlflow.get_last_active_trace_id()
        except Exception:  # noqa: BLE001
            return None

    def flush(self) -> None:
        if self.enabled:
            with contextlib.suppress(Exception):
                self._mlflow.flush_trace_async_logging()


def _safe(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        log.debug("tracing call failed: %s", e)


def llm_attributes(model: str, temperature: float, max_tokens: int, purpose: str) -> dict:
    return {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": GEN_AI_PROVIDER,
        "gen_ai.request.model": model,
        "gen_ai.request.temperature": temperature,
        "gen_ai.request.max_tokens": max_tokens,
        "mlflow.llm.model": model,
        "mlflow.llm.provider": GEN_AI_PROVIDER,
        "data_agent.llm.purpose": purpose,
    }


def record_llm_response(span: Any, response: Any) -> None:
    usage = {
        "input_tokens": response.prompt_tokens,
        "output_tokens": response.completion_tokens,
        "total_tokens": response.prompt_tokens + response.completion_tokens,
    }
    span.set_attributes(
        {
            "mlflow.chat.tokenUsage": usage,
            "gen_ai.usage.input_tokens": response.prompt_tokens,
            "gen_ai.usage.output_tokens": response.completion_tokens,
            "gen_ai.response.model": response.model,
            "gen_ai.response.finish_reasons": [response.finish_reason or ""],
            "data_agent.llm.latency_ms": response.latency_ms,
            "data_agent.llm.tool_calls": len(response.tool_calls),
        }
    )
    span.set_outputs(response.assistant_message())


def tool_attributes(name: str, call_id: str) -> dict:
    return {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": name,
        "gen_ai.tool.call.id": call_id,
        "gen_ai.tool.type": "function",
    }


def make_tracer(setting: str, experiment: str) -> Tracer:
    setting = (setting or "auto").lower()
    if setting == "off":
        return Tracer(False)
    if setting == "auto" and not os.environ.get("MLFLOW_TRACKING_URI"):
        return Tracer(False)
    return Tracer(True, experiment)
