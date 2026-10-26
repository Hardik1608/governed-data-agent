"""Stage 4 (database-enforced read-only execution) and the per-role replicas."""

from __future__ import annotations

import hashlib
import json

import duckdb
import pytest

from data_agent.config import SqlBudget
from data_agent.safety.executor import (
    ExecutionError,
    QueryTimeoutError,
    ReadOnlyViolation,
    SafeDuckDBExecutor,
    SafeSQLiteExecutor,
    parse_explain_json,
    to_jsonable,
)
from data_agent.safety.guard import ALLOW, NEEDS_APPROVAL, REJECT, ExecutorPool, SqlGuard
from data_agent.warehouse.replica import is_stale, replica_file, token


@pytest.fixture
def masked(mini_env):
    ex = SafeDuckDBExecutor(replica_file(mini_env.replica_path, "masked"), timeout_s=5, max_rows=50)
    yield ex
    ex.close()


# ---------------------------------------------------------------- database lockdown
@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE x AS SELECT 1",
        "INSERT INTO dim_country VALUES ('k', 'x', 'XX', 'r', false, true)",
        "DELETE FROM dim_date",
        "DROP TABLE dim_date",
        "CREATE MACRO m(x) AS x + 1",
    ],
)
def test_database_refuses_writes_even_without_the_validator(masked, sql):
    with pytest.raises(ReadOnlyViolation):
        masked.execute(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('C:/Windows/win.ini')",
        "COPY (SELECT 1) TO 'leak.csv'",
        "ATTACH 'other.duckdb' AS o",
        "SET enable_external_access = true",
        "SET memory_limit = '64GB'",
        "LOAD httpfs",
        "SELECT * FROM glob('*')",
    ],
)
def test_database_refuses_external_access_and_settings(masked, sql):
    with pytest.raises(ExecutionError):
        masked.execute(sql)


def test_row_cap_and_types(masked):
    res = masked.execute(
        "SELECT date_day, calendar_year, is_weekend FROM dim_date ORDER BY date_key", max_rows=10
    )
    assert res.row_count == 10 and res.truncated
    assert isinstance(res.rows[0][0], str) and res.rows[0][0].startswith("2009-01-01")
    full = masked.execute("SELECT count(*) AS n FROM dim_date", max_rows=None)
    assert full.rows == [[1461]] and not full.truncated


def test_timeout_interrupts_the_query(masked):
    with pytest.raises(QueryTimeoutError):
        masked.execute(
            "SELECT count(*) FROM fct_order_lines a, fct_order_lines b, fct_order_lines c "
            "WHERE a.quantity + b.quantity + c.quantity = -7",
            timeout_s=0.5,
        )
    assert masked.execute("SELECT 1 AS ok").rows == [[1]]  # connection still usable


def test_explain_estimates_and_cross_products(masked):
    single = masked.explain("SELECT * FROM fct_order_lines")
    cross = masked.explain("SELECT count(*) FROM fct_order_lines a, fct_order_lines b")
    assert single.total_rows > 0
    assert cross.max_rows >= single.max_rows**2 * 0.9  # cross product estimated as a product


def test_parse_explain_json_handles_missing_estimates():
    plan = json.dumps([{"name": "CROSS_PRODUCT", "children": [
        {"name": "SEQ_SCAN", "children": [], "extra_info": {"Estimated Cardinality": "1000"}},
        {"name": "SEQ_SCAN", "children": [], "extra_info": {"Estimated Cardinality": "2,000"}},
    ]}])  # fmt: skip
    est = parse_explain_json(plan)
    assert est.max_rows == 2_000_000 and est.total_rows == 2_003_000


def test_to_jsonable():
    import datetime as dt
    import decimal

    assert to_jsonable(decimal.Decimal("1.50")) == 1.5
    assert to_jsonable(dt.date(2011, 11, 1)) == "2011-11-01"
    assert to_jsonable(float("nan")) is None
    assert to_jsonable([b"\x01", {"a": decimal.Decimal(2)}]) == ["01", {"a": 2.0}]


def test_sqlite_executor_is_read_only(tmp_path):
    import sqlite3

    db = tmp_path / "t.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (a INT)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(10)])
    con.commit()
    con.close()
    ex = SafeSQLiteExecutor(timeout_s=2, max_rows=3)
    res = ex.execute(db, "SELECT a FROM t ORDER BY a")
    assert res.rows == [[0], [1], [2]] and res.truncated
    with pytest.raises(ExecutionError):
        ex.execute(db, "DELETE FROM t")


# ---------------------------------------------------------------- replicas
def test_masked_replica_holds_no_raw_identifier(mini_env):
    raw = duckdb.connect(str(mini_env.warehouse_path), read_only=True)
    ids = {
        r[0]
        for r in raw.execute(
            "SELECT DISTINCT customer_id FROM gold.dim_customer WHERE customer_id <> '-1'"
        ).fetchall()
    }
    rows = raw.execute("SELECT valid_from, first_invoice_at FROM gold.dim_customer").fetchall()
    raw.close()
    m = duckdb.connect(str(replica_file(mini_env.replica_path, "masked")), read_only=True)
    try:
        stored = {
            r[0]
            for r in m.execute(
                "SELECT customer_id FROM dim_customer UNION SELECT customer_id FROM fct_order_lines "
                "UNION SELECT customer_id FROM fct_orders UNION SELECT customer_sk FROM dim_customer"
            ).fetchall()
        }
        sks = {r[0] for r in m.execute("SELECT customer_sk FROM dim_customer").fetchall()}
    finally:
        m.close()
    assert ids and not (ids & stored)
    assert all(v is None or v == "-1" or v.startswith(("cust_", "sk_")) for v in stored)
    # red team: the dbt key md5(customer_id || '|' || valid_from) must not match any stored key
    candidates = {
        hashlib.md5(f"{i}|{t}".encode()).hexdigest() for i in ids for row in rows for t in row if t
    }  # noqa: S324
    assert not (candidates & sks)


def test_tokens_are_stable_and_match_python(mini_env):
    m = duckdb.connect(str(replica_file(mini_env.replica_path, "masked")), read_only=True)
    c = duckdb.connect(str(replica_file(mini_env.replica_path, "clear")), read_only=True)
    try:
        masked_ids = {
            r[0] for r in m.execute("SELECT DISTINCT customer_id FROM dim_customer").fetchall()
        }
        clear_ids = [
            r[0]
            for r in c.execute(
                "SELECT DISTINCT customer_id FROM dim_customer WHERE customer_id <> '-1'"
            ).fetchall()
        ]
    finally:
        m.close()
        c.close()
    assert {token("test-secret", "customer_id", cid, "cust_") for cid in clear_ids} <= masked_ids


def test_aggregates_replica_has_no_customer_tables(mini_env):
    con = duckdb.connect(str(replica_file(mini_env.replica_path, "aggregates")), read_only=True)
    try:
        tables = {
            r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()
        }
    finally:
        con.close()
    assert tables == {
        "mart_revenue_monthly",
        "mart_cohort_retention",
        "dim_date",
        "dim_country",
        "dim_product",
    }


def test_replica_staleness(mini_env, policy):
    path = replica_file(mini_env.replica_path, "masked")
    assert is_stale(path, mini_env.warehouse_path, policy, "test-secret") is None
    assert is_stale(path, mini_env.warehouse_path, policy, "another-secret") == "PII secret changed"


# ---------------------------------------------------------------- guard decisions
@pytest.fixture
def guard_for(mini_env, policy):
    pool = ExecutorPool(mini_env.replica_path, timeout_s=5, max_rows=50)

    def _make(role: str, **budget):
        role_policy = policy.role(role)
        schema = pool.get(role_policy.replica).tables()
        return SqlGuard(policy, role_policy, pool, SqlBudget(**budget), schema)

    yield _make
    pool.close()


def test_guard_allows_and_executes(guard_for):
    guard = guard_for("analyst")
    d = guard.check("SELECT count(*) AS n FROM fct_order_lines")
    assert d.decision == ALLOW and d.estimate is not None
    assert guard.execute(d).rows[0][0] > 0


def test_guard_budget_thresholds(guard_for):
    guard = guard_for("analyst", est_rows_approval=1_000, est_rows_reject=1e12)
    d = guard.check("SELECT * FROM fct_order_lines")
    assert d.decision == NEEDS_APPROVAL and d.approval_kinds == ["budget"]
    with pytest.raises(PermissionError):
        guard.execute(d)  # never runs without an approval
    assert guard.execute(d, approved=True).row_count > 0
    hard = guard_for("analyst", est_rows_reject=1e6).check(
        "SELECT count(*) FROM fct_order_lines a, fct_order_lines b"
    )
    assert hard.decision == REJECT and "hard budget" in hard.reasons[0]


def test_pii_clear_request_by_role(guard_for):
    sql = (
        "SELECT customer_id, sum(line_revenue) AS r FROM fct_order_lines "
        "WHERE customer_id IS NOT NULL GROUP BY 1 ORDER BY r DESC LIMIT 3"
    )
    analyst = guard_for("analyst").check(sql, reveal_pii=True)
    assert analyst.decision == REJECT and "may not see clear" in analyst.reasons[0]
    finance = guard_for("finance")
    d = finance.check(sql, reveal_pii=True)
    assert d.decision == NEEDS_APPROVAL and d.approval_kinds == ["pii"]
    clear_rows = finance.execute(d, approved=True).rows
    masked_rows = finance.execute(finance.check(sql)).rows
    assert not str(clear_rows[0][0]).startswith("cust_")
    assert str(masked_rows[0][0]).startswith("cust_")


def test_viewer_cannot_reach_customer_tables(guard_for):
    d = guard_for("viewer").check("SELECT * FROM fct_order_lines")
    assert d.decision == REJECT and "not available" in d.reasons[0]
