"""Release gate rules, MLflow registration, and the SQLite benchmark adapters."""

from __future__ import annotations

import json
import sqlite3

import pytest
import yaml

from data_agent.bench.bird import execution_match, stratified_subset
from data_agent.bench.single_shot import extract_sql, run_single_shot
from data_agent.bench.sqlite_db import SqliteDb, SqliteGuard, catalog_for, sample_values
from data_agent.config import AgentConfig, Settings
from data_agent.llm.fake import ScriptedLLM
from data_agent.release.gate import check_smoke, decide

RULES = yaml.safe_load((Settings().home / "configs" / "release_gate.yaml").read_text())


def _summary(
    pass_1, pass_k, unsafe=0, leaks=0, inj=0.0, tokens=20000.0, p95=50.0, frozen="t0", items=None
):
    return {
        "meta": {
            "run": f"run-{pass_k}",
            "golden": {"frozen_at": frozen, "golden_version": "v1"},
            "agent": {"model": "fake"},
        },
        "metrics": {
            "k": 4,
            "pass_hat_k": {"pass^1": pass_1, "pass^4": pass_k},
            "safety": {
                "unsafe_executed": unsafe,
                "pii_leaks": leaks,
                "injection_attack_success_rate": inj,
            },
            "cost": {"tokens_per_success": tokens},
            "latency_s": {"p95": p95},
            "refusal": {"precision": 0.5},
            "clarification": {"recall": 0.7},
        },
        "items": items or {"a": {"passes": 4, "trials": 4}, "b": {"passes": 2, "trials": 4}},
    }


def test_gate_bootstraps_then_promotes_challenges_or_rejects():
    champ = _summary(0.70, 0.55)
    assert decide(champ, None, RULES)["decision"] == "promote"
    assert decide(_summary(0.80, 0.65), champ, RULES)["decision"] == "promote"
    assert decide(_summary(0.70, 0.555), champ, RULES)["decision"] == "challenger"
    assert (
        decide(_summary(0.70, 0.55, tokens=15000), champ, RULES)["decision"] == "promote"
    )  # cheaper
    assert decide(_summary(0.60, 0.45), champ, RULES)["decision"] == "reject"  # regression
    unsafe = decide(_summary(0.90, 0.80, unsafe=1), champ, RULES)
    assert unsafe["decision"] == "reject" and unsafe["reason"] == "a hard gate failed"
    assert (
        decide(_summary(0.90, 0.80, frozen="t1"), champ, RULES)["decision"] == "reject"
    )  # other golden version
    assert (
        decide(_summary(0.80, 0.65, tokens=40000), champ, RULES)["decision"] == "reject"
    )  # too expensive


def test_gate_paired_delta():
    champ = _summary(
        0.7, 0.5, items={"a": {"passes": 2, "trials": 4}, "b": {"passes": 4, "trials": 4}}
    )
    cand = _summary(
        0.8, 0.6, items={"a": {"passes": 4, "trials": 4}, "b": {"passes": 4, "trials": 4}}
    )
    paired = decide(cand, champ, RULES)["paired"]
    assert paired["tasks"] == 2 and paired["mean_delta"] == 0.25 and paired["better_tasks"] == 1


def test_check_smoke(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {
                "metrics": {
                    "pass_hat_k": {"pass^1": 1.0},
                    "safety": {"unsafe_executed": 0, "pii_leaks": 0},
                }
            }
        )
    )
    assert check_smoke(path)[0]
    path.write_text(
        json.dumps(
            {
                "metrics": {
                    "pass_hat_k": {"pass^1": 0.99},
                    "safety": {"unsafe_executed": 0, "pii_leaks": 0},
                }
            }
        )
    )
    assert not check_smoke(path)[0]


@pytest.mark.slow
def test_mlflow_registration_and_aliases(tmp_path):
    pytest.importorskip("mlflow")
    from data_agent.release.gate import decide_version, register

    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "summary.json").write_text(json.dumps(_summary(0.7, 0.55)))
    (eval_dir / "summary.md").write_text("# s\n")
    cfg = Settings().agent_config_path
    first = register(cfg, eval_dir, uri)
    assert (
        decide_version(
            str(first["version"]),
            Settings().home / "configs" / "release_gate.yaml",
            tmp_path / "gate",
            uri,
        )["decision"]
        == "promote"
    )
    (eval_dir / "summary.json").write_text(json.dumps(_summary(0.8, 0.65)))
    second = register(cfg, eval_dir, uri)
    result = decide_version(
        str(second["version"]),
        Settings().home / "configs" / "release_gate.yaml",
        tmp_path / "gate",
        uri,
    )
    assert result["decision"] == "promote" and result["champion_version"] == str(first["version"])
    import mlflow

    client = mlflow.MlflowClient(tracking_uri=uri)
    assert str(client.get_model_version_by_alias("governed-data-agent", "champion").version) == str(
        second["version"]
    )
    assert str(
        client.get_model_version_by_alias("governed-data-agent", "previous_champion").version
    ) == str(first["version"])


# ---------------------------------------------------------------- SQLite benchmark adapters
@pytest.fixture
def tiny_db(tmp_path):
    path = tmp_path / "shop" / "shop.sqlite"
    path.parent.mkdir()
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE customer (id INTEGER PRIMARY KEY, name TEXT, country TEXT);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL);"
        "INSERT INTO customer VALUES (1, 'Ann', 'France'), (2, 'Bob', 'Spain'), (3, 'Cy', 'France');"
        "INSERT INTO orders VALUES (1, 1, 10.0), (2, 1, 5.0), (3, 2, 7.5), (4, 3, 1.0);"
    )
    con.commit()
    con.close()
    return SqliteDb(
        "shop",
        path,
        {
            "customer": {"id": "INTEGER", "name": "TEXT", "country": "TEXT"},
            "orders": {"id": "INTEGER", "customer_id": "INTEGER", "amount": "REAL"},
        },
        {"customer": ["id"], "orders": ["id"]},
        [("orders", "customer_id", "customer", "id")],
    )


def test_execution_match_uses_result_sets(tiny_db):
    gold = "SELECT name FROM customer WHERE country = 'France'"
    assert execution_match(
        tiny_db, "SELECT name FROM customer WHERE country = 'France' ORDER BY name DESC", gold
    )[0]
    assert not execution_match(
        tiny_db, "SELECT name, country FROM customer WHERE country = 'France'", gold
    )[0]
    ok, why = execution_match(tiny_db, "SELECT nope FROM customer", gold)
    assert not ok and why.startswith("pred failed")
    assert execution_match(tiny_db, "SELECT 'Ann' UNION SELECT 'Cy'", "SELECT 'x'", alt_gold=gold)[
        0
    ]


def test_sqlite_guard_and_catalog(tiny_db):
    guard = SqliteGuard(tiny_db)
    assert (
        guard.check('SELECT name FROM customer WHERE country = "France"').decision == "allow"
    )  # SQLite string quirk
    assert guard.check("DELETE FROM customer").decision == "reject"
    assert guard.check("SELECT * FROM missing").decision == "reject"
    catalog = catalog_for(tiny_db, sample_values(tiny_db))
    doc = catalog.get("customer").to_text()
    assert "Examples: France, Spain" in doc and "primary key" in doc
    assert "FOREIGN KEY (customer_id) REFERENCES customer(id)" in tiny_db.ddl()


def test_single_shot_repairs_once(tiny_db):
    llm = ScriptedLLM(agent=[], judge=[])
    llm.scripts["baseline"] = [
        {"content": "SELECT nam FROM customer"},
        {"content": "```sql\nSELECT name FROM customer;\n```"},
    ]
    result = run_single_shot(llm, tiny_db, SqliteGuard(tiny_db), "names?", "")
    assert (
        result.status == "ok"
        and result.repaired
        and result.llm_calls == 2
        and result.sql == "SELECT name FROM customer"
    )
    unsafe = ScriptedLLM()
    unsafe.scripts["baseline"] = [{"content": "SELECT 1; DROP TABLE customer"}]
    r = run_single_shot(unsafe, tiny_db, SqliteGuard(tiny_db), "x", "")
    assert r.status == "rejected" and r.llm_calls == 1  # never "repairs" an unsafe statement


def test_extract_sql_v1_rules():
    assert extract_sql("SQL: SELECT a FROM t;\nExplanation: ...") == "SELECT a FROM t"
    assert extract_sql("```sql\nSELECT 1\n```\nmore\n```sql\nSELECT 2\n```") == "SELECT 2"
    assert (
        extract_sql("SELECT 1; DROP TABLE t") == "SELECT 1; DROP TABLE t"
    )  # kept for the validator
    assert extract_sql("I cannot. CANNOT_ANSWER") == "CANNOT_ANSWER"


def test_stratified_subset_keeps_the_mix():
    items = [
        {"question_id": i, "difficulty": d, "db_id": db}
        for i, (d, db) in enumerate(
            [
                (d, db)
                for d in ("simple", "moderate", "challenging")
                for db in ("a", "b")
                for _ in range(50 if d == "simple" else 25 if d == "moderate" else 10)
            ]
        )
    ]
    sub = stratified_subset(items, 34)
    assert len(sub) == 34
    simple = sum(1 for s in sub if s["difficulty"] == "simple")
    assert 19 <= simple <= 21  # 100/170 of 34 = 20


# ---------------------------------------------------------------- v2: evidence-based refusal
def test_planner_refusal_outside_the_categories_is_overruled(make_service):
    from data_agent.llm.fake import last_tool_result

    cfg = AgentConfig.load(Settings().home / "configs" / "agents" / "governed-v2.yaml")
    plan = {"action": "refuse", "refuse_category": "", "reason": "no postage data"}
    llm = ScriptedLLM(
        plan=[{"content": json.dumps(plan)}],
        agent=[
            {"tool_calls": [("run_sql", {"sql": "SELECT count(*) AS n FROM dim_date"})]},
            lambda m, t: {
                "content": f"{last_tool_result(m, 'run_sql')['rows'][0][0]:,} days [q1]."
            },
        ],
    )
    result = make_service(llm, cfg).ask("How many days are in the calendar?", "u1", "analyst")
    assert result.status == "answered"
    listed = ScriptedLLM(
        plan=[
            {
                "content": json.dumps(
                    {"action": "refuse", "refuse_category": "marketing", "reason": "no spend"}
                )
            }
        ]
    )
    assert (
        make_service(listed, cfg).ask("What did we spend on ads?", "u1", "analyst").status
        == "refused"
    )


# ---------------------------------------------------------------- Spider2-lite scorer (port)
def test_spider2_scorer_matches_upstream_semantics(tmp_path):
    from data_agent.bench.spider2 import score

    gold = tmp_path / "local001.csv"
    gold.write_text("name,total\nAnn,10.004\nBob,7.5\n")
    std = {"condition_cols": [], "ignore_order": True}
    assert score(
        ["who", "amount", "extra"], [["Bob", 7.5, 1], ["Ann", 10.0, 2]], [gold], std
    )  # extra column, order, 0.01 tol
    assert not score(["who", "amount"], [["Bob", 7.5], ["Ann", 10.2]], [gold], std)
    assert not score(["who", "amount"], [["Bob", 7.5]], [gold], std)  # missing row
    only_name = {"condition_cols": [0], "ignore_order": True}
    assert score(["who"], [["Ann"], ["Bob"]], [gold], only_name)
    ordered = {"condition_cols": [], "ignore_order": False}
    assert not score(["who", "amount"], [["Bob", 7.5], ["Ann", 10.0]], [gold], ordered)
    alt = tmp_path / "local001_b.csv"
    alt.write_text("n\nAnn\nBob\n")
    assert score(
        ["n"], [["Ann"], ["Bob"]], [gold, alt], {"condition_cols": [], "ignore_order": True}
    )  # any variant
