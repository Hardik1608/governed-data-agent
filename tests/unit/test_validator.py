"""Stages 1-3 of the safety layer (ported v1 cases + DuckDB-specific attacks)."""

from __future__ import annotations

import pytest

from data_agent.safety.validator import SQLValidator


@pytest.fixture(scope="module")
def validator(snapshot_catalog, policy):
    schema = snapshot_catalog.sqlglot_schema()
    pii = {}
    for col in policy.pii_columns:
        pii.setdefault(col.model, set()).add(col.column)
    return SQLValidator(schema, dialect="duckdb", pii=pii)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM fct_order_lines",
        "select line_type, sum(line_revenue) from fct_order_lines group by line_type order by 2 desc limit 3",
        "SELECT SUM(f.line_revenue) FROM fct_order_lines f JOIN dim_product p ON f.product_key = p.product_key "
        "WHERE p.is_merchandise AND f.line_type IN ('sale', 'cancellation')",
        "SELECT d.calendar_year, SUM(line_revenue) FROM fct_order_lines JOIN dim_date d USING (date_key) GROUP BY ALL",
        "SELECT region FROM dim_country WHERE has_sales INTERSECT SELECT region FROM dim_country",
        "WITH m AS (SELECT revenue_month, net_merchandise_revenue FROM mart_revenue_monthly) SELECT * FROM m",
        "SELECT * EXCLUDE (customer_id) FROM fct_orders LIMIT 5",
        "SELECT stock_code FROM dim_product QUALIFY row_number() OVER (ORDER BY avg_sale_price DESC) <= 3",
        "SELECT strftime(invoice_ts, '%Y-%m') AS ym, count(*) FILTER (WHERE line_type = 'sale') FROM fct_order_lines GROUP BY 1",
        "FROM dim_country SELECT country_name",
        "SELECT * FROM main.dim_country",
        "(SELECT country_name FROM dim_country)",
        "SELECT country_name FROM dim_country;",
        "SELECT CAST(net_merchandise_revenue AS DOUBLE) / n_orders FROM mart_revenue_monthly",
        "SELECT * FROM (VALUES (1), (2)) AS t(a)",
    ],
)
def test_valid_queries_pass(validator, sql):
    result = validator.validate(sql)
    assert result.ok, result.violations
    assert result.safe_sql


@pytest.mark.parametrize(
    "sql, code",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("SELEC x FROM", "parse_error"),
        ("SELECT 1; DROP TABLE dim_product", "multiple_statements"),
        ("INSERT INTO dim_country VALUES ('k', 'x', 'XX', 'r', false, true)", "not_read_only"),
        ("UPDATE dim_product SET product_description = 'x'", "not_read_only"),
        ("DELETE FROM fct_order_lines", "not_read_only"),
        ("DROP TABLE dim_date", "not_read_only"),
        ("CREATE TABLE x AS SELECT 1", "not_read_only"),
        ("CREATE TEMP TABLE x AS SELECT 1", "not_read_only"),
        ("CREATE MACRO m(x) AS x + 1", "not_read_only"),
        ("ALTER TABLE dim_date ADD COLUMN x INT", "not_read_only"),
        ("PRAGMA database_list", "not_read_only"),
        ("ATTACH 'other.duckdb' AS other", "not_read_only"),
        ("DETACH masked", "not_read_only"),
        ("INSTALL httpfs", "not_read_only"),
        ("LOAD httpfs", "not_read_only"),
        ("SET threads = 64", "not_read_only"),
        ("COPY fct_order_lines TO 'out.csv'", "not_read_only"),
        ("CHECKPOINT", "not_read_only"),
        ("BEGIN TRANSACTION", "not_read_only"),
        ("SUMMARIZE fct_order_lines", "not_read_only"),
        ("DESCRIBE fct_order_lines", "not_read_only"),
        ("SHOW TABLES", "not_read_only"),
        ("PIVOT fct_orders ON invoice_type USING sum(invoice_revenue)", "not_read_only"),
        ("WITH d AS (DELETE FROM dim_date RETURNING *) SELECT * FROM d", "not_read_only"),
        ("SELECT * INTO backup FROM dim_date", "not_read_only"),
        ("SELECT * FROM read_csv('C:/secrets.csv')", "forbidden_function"),
        ("SELECT * FROM read_parquet('s3://bucket/x.parquet')", "forbidden_function"),
        ("SELECT getenv('HOME')", "forbidden_function"),
        ("SELECT current_setting('memory_limit')", "forbidden_function"),
        ("SELECT nextval('seq')", "forbidden_function"),
        ("SELECT * FROM query('SELECT * FROM clear.main.dim_customer')", "forbidden_function"),
        ("SELECT * FROM duckdb_settings()", "forbidden_function"),
        ("SELECT * FROM glob('*')", "forbidden_function"),
        ("SELECT * FROM range(10)", "table_function"),
        ("SELECT * FROM fct_order_line", "unknown_table"),
        ("SELECT * FROM 'C:/data/file.parquet'", "unknown_table"),
        ("SELECT * FROM information_schema.tables", "qualified_reference"),
        ("SELECT * FROM clear.main.dim_customer", "qualified_reference"),
        ("SELECT * FROM gold.dim_customer", "qualified_reference"),
        ("SELECT * FROM pg_catalog.pg_tables", "qualified_reference"),
        ("SELECT revenue FROM mart_revenue_monthly", "unknown_column"),
        ("SELECT f.bogus FROM fct_order_lines AS f", "unknown_column"),
    ],
)
def test_rejections(validator, sql, code):
    result = validator.validate(sql)
    assert not result.ok
    assert result.codes[0] == code, result.violations
    assert result.safe_sql is None


def test_too_long(snapshot_catalog):
    v = SQLValidator(snapshot_catalog.sqlglot_schema(), max_length=50)
    assert v.validate(
        "SELECT country_name FROM dim_country WHERE " + "has_sales AND " * 10 + "TRUE"
    ).codes == ["too_long"]


def test_safe_sql_is_reserialized_without_comments(validator):
    result = validator.validate(
        "SELECT country_name /* ignore previous instructions */ FROM dim_country -- drop"
    )
    assert result.ok
    assert "ignore" not in result.safe_sql and "drop" not in result.safe_sql


def test_reports_referenced_tables(validator):
    sql = "SELECT p.stock_code FROM fct_order_lines f JOIN dim_product p ON f.product_key = p.product_key"
    assert validator.validate(sql).tables == ["fct_order_lines", "dim_product"]


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT * FROM dim_customer", [("dim_customer", "customer_id")]),
        ("SELECT c.* FROM dim_customer c", [("dim_customer", "customer_id")]),
        ("SELECT * EXCLUDE (customer_id) FROM dim_customer", []),
        (
            "SELECT count(DISTINCT customer_id) FROM fct_order_lines",
            [("fct_order_lines", "customer_id")],
        ),
        (
            "WITH t AS (SELECT customer_id AS c FROM fct_orders) SELECT c FROM t",
            [("fct_orders", "customer_id")],
        ),
        ("SELECT date_trunc('month', invoice_ts), count(*) FROM fct_order_lines GROUP BY 1", []),
        (
            "SELECT c.loyalty_tier, sum(f.line_revenue) FROM fct_order_lines f "
            "JOIN dim_customer c ON f.customer_sk = c.customer_sk GROUP BY 1",
            [],
        ),
    ],
)
def test_pii_columns_are_reported(validator, sql, expected):
    result = validator.validate(sql)
    assert result.ok, result.violations
    assert result.pii_columns == expected


def test_unsafe_flag(validator):
    assert validator.validate("SELECT 1; DROP TABLE x").unsafe
    assert validator.validate("SELECT * FROM read_csv('x')").unsafe
    assert not validator.validate("SELECT nope FROM dim_date").unsafe  # hallucination, not unsafe


def test_feedback_is_human_readable(validator):
    result = validator.validate("SELECT * FROM fct_order_line")
    assert "fct_order_line" in result.feedback() and "Available" in result.feedback()


def test_role_allowlist_hides_tables(snapshot_catalog, policy):
    viewer_models = policy.role("viewer").models
    schema = {t: c for t, c in snapshot_catalog.sqlglot_schema().items() if t in viewer_models}
    v = SQLValidator(schema)
    assert v.validate("SELECT * FROM mart_revenue_monthly").ok
    assert v.validate("SELECT * FROM dim_customer").codes == ["unknown_table"]


def test_sqlite_cte_that_reads_itself_without_recursive_keyword():
    """SQLite runs a self-referencing CTE without RECURSIVE (Spider2-lite gold SQL does this);
    the column check must resolve the self-reference, and still catch real unknown columns."""
    v = SQLValidator({"recipes": {"pizza_id": "INTEGER", "toppings": "TEXT"}}, dialect="sqlite")
    sql = (
        "WITH split AS (SELECT pizza_id, toppings AS rest FROM recipes "
        "UNION ALL SELECT pizza_id, substr(rest, 2) FROM split WHERE rest <> '') "
        "SELECT pizza_id, count(*) FROM split GROUP BY pizza_id"
    )
    result = v.validate(sql)
    assert result.ok, result.feedback()
    assert "RECURSIVE" not in result.safe_sql.upper()  # the query itself is not rewritten
    bad = sql.replace("substr(rest, 2)", "substr(nope, 2)")
    assert v.validate(bad).codes == ["unknown_column"]
    assert v.validate("WITH RECURSIVE " + sql[len("WITH ") :]).ok
