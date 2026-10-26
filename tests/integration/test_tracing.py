"""MLflow Tracing: one trace per question with GenAI semantic-convention attributes."""

from __future__ import annotations

import pytest

from data_agent.agent.service import AgentService
from data_agent.llm.fake import ScriptedLLM, last_tool_result


def test_unreachable_tracking_server_disables_tracing_quickly(monkeypatch):
    pytest.importorskip("mlflow")
    import time

    from data_agent.observability.tracing import Tracer

    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:9")  # nothing listens here
    started = time.monotonic()
    tracer = Tracer(True, "unreachable", wait_s=1)
    assert not tracer.enabled  # serving continues without traces
    assert time.monotonic() - started < 30
    import os

    assert "MLFLOW_HTTP_REQUEST_MAX_RETRIES" not in os.environ  # fast-fail settings restored


@pytest.mark.slow
def test_trace_has_agent_llm_and_tool_spans(make_runtime, tmp_path, monkeypatch):
    mlflow = pytest.importorskip("mlflow")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}")
    mlflow.set_tracking_uri(f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}")
    from data_agent.observability.tracing import Tracer

    tracer = Tracer(True, "trace-test")
    assert tracer.enabled

    def answer(messages, tools):
        return {"content": f"{last_tool_result(messages, 'run_sql')['rows'][0][0]:,} days [q1]."}

    llm = ScriptedLLM(
        agent=[{"tool_calls": [("run_sql", {"sql": "SELECT count(*) AS n FROM dim_date"})]}, answer]
    )
    service = AgentService(make_runtime(llm, tracer=tracer), tmp_path / "ckpt.sqlite")
    try:
        result = service.ask("How many days are in the calendar?", "u1", "analyst")
    finally:
        service.close()
    assert result.status == "answered" and result.trace_id
    tracer.flush()
    trace = mlflow.get_trace(result.trace_id)
    spans = {s.name: s for s in trace.data.spans}
    root = spans["agent.ask"]
    assert root.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert root.attributes["gen_ai.conversation.id"] == result.thread_id
    llm_spans = [s for s in trace.data.spans if s.name.startswith("llm.")]
    assert len(llm_spans) == 3  # plan + 2 agent steps
    usage = llm_spans[-1].attributes["mlflow.chat.tokenUsage"]
    assert (
        usage["input_tokens"] > 0
        and llm_spans[-1].attributes["gen_ai.usage.input_tokens"] == usage["input_tokens"]
    )
    tool = spans["tool.run_sql"]
    assert tool.attributes["gen_ai.operation.name"] == "execute_tool"
    assert tool.attributes["gen_ai.tool.name"] == "run_sql"
    assert {
        "node.plan",
        "node.retrieve",
        "node.agent",
        "node.tools",
        "node.check",
        "node.finalize",
    } <= set(spans)
    assert "retrieval.search_catalog" in spans
