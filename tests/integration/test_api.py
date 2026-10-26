"""FastAPI service: auth, ask/resume, ownership, idempotency, rate limit, metrics."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from data_agent.agent.service import AgentService
from data_agent.api.app import create_app
from data_agent.api.auth import make_token
from data_agent.config import AgentConfig, Settings
from data_agent.llm.fake import ScriptedLLM, last_tool_result


def _answer(messages, tools):
    return {"content": f"{last_tool_result(messages, 'run_sql')['rows'][0][0]:,} days [q1]."}


@pytest.fixture
def api(make_runtime, mini_env, tmp_path):
    alice, alice_entry = make_token("alice", "analyst")
    bob, bob_entry = make_token("bob", "viewer")
    tokens_file = tmp_path / "tokens.json"
    tokens_file.write_text(json.dumps({"tokens": [alice_entry, bob_entry]}))
    cfg = AgentConfig.load(Settings().agent_config_path)
    data = cfg.model_dump()
    data["budgets"]["sql"]["est_rows_approval"] = 100
    cfg = AgentConfig.model_validate(data)
    llm = ScriptedLLM(
        agent=[
            {"tool_calls": [("run_sql", {"sql": "SELECT count(*) AS n FROM dim_date"})]},
            _answer,
            {"tool_calls": [("run_sql", {"sql": "SELECT * FROM fct_order_lines"})]},
            {"content": "Denied, so no answer."},
        ]
        + [{"content": "ok"}] * 5
    )
    service = AgentService(make_runtime(llm, cfg), tmp_path / "api_ckpt.sqlite")
    settings = Settings(home=mini_env.home, tokens_file=tokens_file, rate_limit_per_minute=6)
    with TestClient(create_app(service, settings)) as client:
        yield (
            client,
            {
                "alice": {"Authorization": f"Bearer {alice}"},
                "bob": {"Authorization": f"Bearer {bob}"},
            },
        )
    service.close()


def test_auth_is_required(api):
    client, _ = api
    assert client.post("/v1/ask", json={"question": "hi"}).status_code == 401
    assert (
        client.post(
            "/v1/ask", json={"question": "hi"}, headers={"Authorization": "Bearer nope"}
        ).status_code
        == 401
    )
    assert client.get("/healthz").status_code == 200


def test_ask_resume_and_ownership(api):
    client, h = api
    r = client.post(
        "/v1/ask", json={"question": "How many days are in the calendar?"}, headers=h["alice"]
    )
    assert r.status_code == 200 and r.headers["X-AI-Generated"] == "true"
    body = r.json()
    assert (
        body["status"] == "answered"
        and "1,461" in body["answer"]
        and "EU AI Act" in body["disclosure"]
    )
    r2 = client.post(
        "/v1/ask",
        json={"question": "List every order line", "thread_id": body["thread_id"]},
        headers=h["alice"],
    )
    pending = r2.json()
    assert pending["status"] == "interrupted"
    req_id = pending["interrupt"]["requests"][0]["id"]
    assert (
        client.post(
            f"/v1/threads/{body['thread_id']}/resume",
            json={"answers": {req_id: {"approved": False}}},
            headers=h["bob"],
        ).status_code
        == 403
    )
    done = client.post(
        f"/v1/threads/{body['thread_id']}/resume",
        json={"answers": {req_id: {"approved": False}}},
        headers=h["alice"],
    )
    assert done.json()["status"] == "answered"
    again = client.post(
        f"/v1/threads/{body['thread_id']}/resume", json={"answers": {}}, headers=h["alice"]
    )
    assert again.status_code == 409
    assert client.get("/v1/threads/does-not-exist", headers=h["alice"]).status_code == 404
    audit = client.get(f"/v1/threads/{body['thread_id']}/audit", headers=h["alice"]).json()[
        "events"
    ]
    assert {"question", "sql", "approval", "answer"} <= {e["kind"] for e in audit}


def test_idempotency_key_replays_without_rerunning(api):
    client, h = api
    headers = {**h["alice"], "Idempotency-Key": "abc-1"}
    first = client.post(
        "/v1/ask", json={"question": "How many days are in the calendar?"}, headers=headers
    ).json()
    second = client.post(
        "/v1/ask", json={"question": "How many days are in the calendar?"}, headers=headers
    ).json()
    assert second["idempotent_replay"] and second["thread_id"] == first["thread_id"]


def test_rate_limit(api):
    client, h = api
    codes = [client.get("/v1/threads/x", headers=h["bob"]).status_code for _ in range(10)]
    assert 429 in codes


def test_metrics_endpoint(api):
    client, h = api
    client.post(
        "/v1/ask", json={"question": "How many days are in the calendar?"}, headers=h["alice"]
    )
    text = client.get("/metrics").text
    for name in (
        "data_agent_questions_total",
        "data_agent_llm_calls_total",
        "data_agent_tool_calls_total",
        "data_agent_http_requests_total",
    ):
        assert name in text


def test_bad_requests(api):
    client, h = api
    assert client.post("/v1/ask", json={"question": ""}, headers=h["alice"]).status_code == 422
    assert (
        client.post(
            "/v1/ask", json={"question": "x", "thread_id": "../etc"}, headers=h["alice"]
        ).status_code
        == 422
    )


def test_shadow_challenger_runs_in_the_background(make_runtime, mini_env, tmp_path):
    from data_agent.agent.shadow import ShadowRunner

    alice, entry = make_token("alice", "analyst")
    tokens_file = tmp_path / "tokens.json"
    tokens_file.write_text(json.dumps({"tokens": [entry]}))
    champion_llm = ScriptedLLM(
        agent=[
            {"tool_calls": [("run_sql", {"sql": "SELECT count(*) AS n FROM dim_date"})]},
            _answer,
        ]
    )
    challenger_llm = ScriptedLLM(
        agent=[
            {"tool_calls": [("run_sql", {"sql": "SELECT count(*) AS n FROM dim_date"})]},
            _answer,
        ]
    )
    champion = AgentService(make_runtime(champion_llm), tmp_path / "c.sqlite")
    shadow = ShadowRunner(
        AgentService(make_runtime(challenger_llm), tmp_path / "s.sqlite"),
        rate=1.0,
        log_path=tmp_path / "shadow.jsonl",
        seed=1,
    )
    settings = Settings(home=mini_env.home, tokens_file=tokens_file)
    with TestClient(create_app(champion, settings, shadow=shadow)) as client:
        r = client.post(
            "/v1/ask",
            json={"question": "How many days are in the calendar?"},
            headers={"Authorization": f"Bearer {alice}"},
        )
        assert r.json()["status"] == "answered"
    shadow.close(wait=True)
    record = json.loads((tmp_path / "shadow.jsonl").read_text().splitlines()[0])
    assert (
        record["shadow_status"] == "answered"
        and record["agree"]
        and record["number_jaccard"] == 1.0
    )
    champion.close()
    shadow.service.close()


def test_token_command_stores_only_the_hash(tmp_path, capsys):
    from data_agent.api.auth import TokenStore
    from data_agent.cli import main

    path = tmp_path / "state" / "tokens.json"
    assert main(["token", "--user", "carol", "--role", "finance", "--file", str(path)]) == 0
    token = capsys.readouterr().out.strip()
    assert token.startswith("gda_") and token not in path.read_text(encoding="utf-8")
    principal = TokenStore.load(path).verify(token)
    assert principal is not None and (principal.user, principal.role) == ("carol", "finance")
    assert main(["token", "--user", "eve", "--role", "admin", "--file", str(path)]) == 2
    assert len(json.loads(path.read_text(encoding="utf-8"))["tokens"]) == 1
