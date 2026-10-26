"""MCP server: in-process client, a real stdio subprocess, and Streamable HTTP with bearer auth."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import anyio
import httpx
import pytest
from mcp import Client, StdioServerParameters

from data_agent.agent.service import AgentService
from data_agent.api.auth import make_token
from data_agent.llm.fake import ScriptedLLM, last_tool_result
from data_agent.mcp_server.server import build_server

ROOT = Path(__file__).resolve().parents[2]


def _text(result) -> dict:
    if result.structured_content is not None:
        data = result.structured_content
        return (
            data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
        )
    return json.loads(result.content[0].text)


@pytest.fixture
def server(make_runtime, tmp_path):
    def answer(messages, tools):
        rows = last_tool_result(messages, "run_sql")["rows"]
        return {"content": f"There are {rows[0][0]:,} days in the calendar [q1]."}

    llm = ScriptedLLM(
        agent=[{"tool_calls": [("run_sql", {"sql": "SELECT count(*) AS n FROM dim_date"})]}, answer]
    )
    service = AgentService(make_runtime(llm), tmp_path / "mcp_ckpt.sqlite")
    yield build_server(service, stdio_role="analyst")
    service.close()


def test_in_process_tools(server):
    async def main():
        async with Client(server, raise_exceptions=True) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert {
                "search_catalog",
                "describe_model",
                "get_metric",
                "run_sql",
                "make_chart",
                "ask_data_agent",
                "resume_data_agent",
            } <= tools
            hits = _text(await client.call_tool("search_catalog", {"query": "loyalty tier"}))
            assert hits["results"]
            ok = _text(
                await client.call_tool(
                    "run_sql",
                    {"sql": "SELECT region, count(*) AS n FROM dim_country GROUP BY 1 ORDER BY 1"},
                )
            )
            assert ok["columns"] == ["region", "n"] and ok["query_id"].startswith("m")
            chart = _text(
                await client.call_tool(
                    "make_chart",
                    {"query_id": ok["query_id"], "mark": "bar", "x": "region", "y": "n"},
                )
            )
            assert chart["data"]["values"]
            bad = _text(await client.call_tool("run_sql", {"sql": "DROP TABLE dim_date"}))
            assert "rejected" in bad["error"].lower()
            answer = _text(
                await client.call_tool(
                    "ask_data_agent", {"question": "How many days are in the calendar?"}
                )
            )
            assert answer["status"] == "answered" and "1,461" in answer["answer"]
            doc = await client.read_resource("catalog://models/dim_date")
            assert "date_key" in doc.contents[0].text

    anyio.run(main)


def _env(mini_env, **extra) -> dict:
    env = dict(os.environ)
    env.update(
        {
            "DATA_AGENT_HOME": str(ROOT),
            "DATA_AGENT_WAREHOUSE": str(mini_env.warehouse_path),
            "DATA_AGENT_REPLICA_DIR": str(mini_env.replica_path),
            "DATA_AGENT_DBT_DIR": str(mini_env.dbt_path),
            "DATA_AGENT_STATE_DIR": str(mini_env.state_path / "subprocess"),
            "DATA_AGENT_PII_SECRET": "test-secret",
            "DATA_AGENT_EMBEDDINGS": "fake",
            "DATA_AGENT_TRACING": "off",
            "DATA_AGENT_LLM_BASE_URL": "http://127.0.0.1:9/v1",  # never called by these tests
            "PYTHONIOENCODING": "utf-8",
        }
    )
    env.update(extra)
    return env


@pytest.mark.slow
def test_stdio_subprocess(mini_env):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "data_agent.cli", "mcp", "--transport", "stdio"],
        env=_env(mini_env, DATA_AGENT_MCP_ROLE="viewer"),
        cwd=str(ROOT),
    )

    async def main():
        async with Client(params, read_timeout_seconds=120) as client:
            res = _text(
                await client.call_tool(
                    "run_sql", {"sql": "SELECT count(*) AS n FROM mart_revenue_monthly"}
                )
            )
            assert res["rows"] == [[25]]
            denied = _text(
                await client.call_tool("run_sql", {"sql": "SELECT count(*) FROM dim_customer"})
            )
            assert "not available" in denied["error"]  # viewer role has no customer tables

    anyio.run(main)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.slow
def test_streamable_http_requires_a_bearer_token(mini_env, tmp_path):
    token, entry = make_token("alice", "viewer")
    tokens_file = tmp_path / "tokens.json"
    tokens_file.write_text(json.dumps({"tokens": [entry]}))
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "data_agent.cli", "mcp", "--transport", "http", "--port", str(port)],
        env=_env(
            mini_env,
            DATA_AGENT_TOKENS_FILE=str(tokens_file),
            DATA_AGENT_MCP_ISSUER_URL="https://idp.example.com",
        ),
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            try:
                httpx.get(f"{base}/mcp", timeout=1)
                break
            except httpx.TransportError:
                time.sleep(0.5)
        anon = httpx.post(
            f"{base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=10
        )
        assert anon.status_code == 401
        assert "resource_metadata" in anon.headers.get("www-authenticate", "")
        meta = httpx.get(f"{base}/.well-known/oauth-protected-resource/mcp", timeout=10).json()
        assert meta["authorization_servers"][0].startswith("https://idp.example.com")
        assert meta["resource"] == f"{base}/mcp"

        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        async def main():
            http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60)
            async with Client(streamable_http_client(f"{base}/mcp", http_client=http)) as client:
                res = _text(
                    await client.call_tool(
                        "run_sql", {"sql": "SELECT count(*) AS n FROM dim_country"}
                    )
                )
                assert res["rows"] == [[5]]
                denied = _text(
                    await client.call_tool(
                        "run_sql", {"sql": "SELECT count(*) FROM fct_order_lines"}
                    )
                )
                assert "not available" in denied["error"]  # the token's role is viewer
                assert client.protocol_version == "2026-07-28"

        anyio.run(main)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_interrupts_become_mcp_elicitation(make_runtime, tmp_path):
    """An over-budget query inside ask_data_agent is approved through MCP elicitation."""
    from mcp import types

    from data_agent.config import AgentConfig, Settings

    cfg = AgentConfig.load(Settings().agent_config_path)
    data = cfg.model_dump()
    data["budgets"]["sql"]["est_rows_approval"] = 10
    cfg = AgentConfig.model_validate(data)

    def answer(messages, tools):
        res = last_tool_result(messages, "run_sql")
        return {"content": f"{res['rows'][0][0]:,} lines [q1]." if "rows" in res else "Denied."}

    llm = ScriptedLLM(
        agent=[
            {
                "tool_calls": [
                    ("run_sql", {"sql": "SELECT sum(quantity) AS n FROM fct_order_lines"})
                ]
            },
            answer,
        ]
    )
    service = AgentService(make_runtime(llm, cfg), tmp_path / "elicit.sqlite")
    server = build_server(service, stdio_role="analyst")
    asked: list[str] = []

    async def approve(context, params) -> types.ElicitResult:
        asked.append(params.message)
        return types.ElicitResult(
            action="accept", content={"approved": True, "comment": "ok for the test"}
        )

    async def main():
        async with Client(server, raise_exceptions=True, elicitation_callback=approve) as client:
            out = _text(
                await client.call_tool(
                    "ask_data_agent", {"question": "How many units are on all order lines?"}
                )
            )
            assert out["status"] == "answered" and "lines" in out["answer"]

    anyio.run(main)
    assert asked and "Approve this query" in asked[0]
    audit = service.runtime.audit.events()
    assert any(e["kind"] == "approval" and e["status"] == "approved" for e in audit)
    service.close()
