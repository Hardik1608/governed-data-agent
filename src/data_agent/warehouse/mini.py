"""A small synthetic warehouse with the exact schema of the real one (for tests and CI).

Built from `golden/catalog_snapshot.json` (column names and types of the 8 reporting models)
and filled with deterministic synthetic rows: 2009-12 .. 2011-12, five countries, 40
customers with SCD2 versions, ~4,000 order lines including cancellations, write-offs,
guest checkouts and postage. Facts, the order aggregate and both marts are derived with the
same logic as the dbt models, so the golden set's gold SQL binds and runs on it unchanged.
No real data is involved.
"""

from __future__ import annotations

import csv
import hashlib
import random
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from data_agent.warehouse.catalog import Catalog

COUNTRIES = [
    ("United Kingdom", "GB", "UK & Ireland", True),
    ("France", "FR", "Western Europe", False),
    ("Germany", "DE", "Western Europe", False),
    ("EIRE", "IE", "UK & Ireland", False),
    ("USA", "US", "Americas", False),
]
TIERS = [(20000, "platinum"), (5000, "gold"), (1000, "silver"), (0, "bronze")]


def _md5(*parts: str) -> str:
    return hashlib.md5("|".join(parts).encode()).hexdigest()  # noqa: S324 - mirrors dbt keys


def _bulk_insert(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple]) -> None:
    """Fast insert through a temp CSV (executemany costs ~5 ms per row in DuckDB)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rows.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            for row in rows:
                writer.writerow(["__NULL__" if v is None else v for v in row])
        cols = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'gold' AND table_name = ? ORDER BY ordinal_position",
            [table],
        ).fetchall()
        types = ", ".join(f"'{c}': '{t}'" for c, t in cols)
        quoted = str(path).replace("'", "''")
        con.execute(
            f"INSERT INTO gold.{table} SELECT * FROM read_csv('{quoted}', header = false, "
            f"nullstr = '__NULL__', columns = {{{types}}})"
        )


def build_mini_warehouse(path: Path, catalog: Catalog, seed: int = 7) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    rng = random.Random(seed)
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE SCHEMA gold")
        for model in catalog.models.values():
            cols = ", ".join(f'"{c.name}" {c.data_type}' for c in model.columns)
            con.execute(f'CREATE TABLE gold."{model.name}" ({cols})')
        _dims(con, rng)
        _facts(con, rng)
        _derived(con)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    return path


def _dims(con: duckdb.DuckDBPyConnection, rng: random.Random) -> None:
    con.execute(
        """
        INSERT INTO gold.dim_date
        SELECT CAST(strftime(d, '%Y%m%d') AS INTEGER), d::DATE, year(d), quarter(d), month(d),
               monthname(d), strftime(d, '%Y-%m'), date_trunc('month', d)::DATE, day(d),
               isodow(d), dayname(d), isodow(d) IN (6, 7)
        FROM range(DATE '2009-01-01', DATE '2013-01-01', INTERVAL 1 DAY) t(d)
        """
    )
    _bulk_insert(
        con,
        "dim_country",
        [(_md5(n), n, iso, region, dom, True) for n, iso, region, dom in COUNTRIES],
    )
    products = []
    for i in range(30):
        code = f"{20000 + i * 37}{'A' if i % 5 == 0 else ''}"
        products.append(
            (
                _md5(code),
                code,
                f"MINI PRODUCT {i:02d}",
                "merchandise",
                True,
                round(0.5 + i * 0.4, 3),
            )
        )
    products.append((_md5("POST"), "POST", "POSTAGE", "postage", False, 18.0))
    products.append((_md5("M"), "M", "Manual", "manual_adjustment", False, 5.0))
    _bulk_insert(
        con, "dim_product", [(*p, "2009-12-01 00:00:00", "2011-12-09 00:00:00") for p in products]
    )


def _facts(con: duckdb.DuckDBPyConnection, rng: random.Random) -> None:
    products = con.execute(
        "SELECT product_key, stock_code, avg_sale_price, is_merchandise FROM gold.dim_product"
    ).fetchall()
    merch = [p for p in products if p[3]]
    post = next(p for p in products if p[1] == "POST")
    countries = con.execute("SELECT country_key, country_name FROM gold.dim_country").fetchall()
    customers = []
    for i in range(40):
        cid = str(12346 + i)
        country = countries[0] if i < 28 else countries[1 + i % 4]
        customers.append((cid, country))
    start = datetime(2009, 12, 1, 8, 0)
    end = datetime(2011, 12, 9, 12, 0)
    span_minutes = int((end - start).total_seconds() // 60)
    invoices = []
    for _ in range(900):
        ts = start + timedelta(minutes=rng.randrange(span_minutes))
        ts = ts.replace(second=0, microsecond=0)
        guest = rng.random() < 0.2
        cust = None if guest else rng.choice(customers)
        country = cust[1] if cust else rng.choice(countries)
        invoices.append((ts, cust[0] if cust else None, country))
    invoices.sort()
    lines = []
    spend: dict[str, float] = {}
    versions: dict[str, list[tuple[datetime, str]]] = {}
    for n, (ts, cid, country) in enumerate(invoices):
        kind = rng.random()
        prefix = "C" if kind < 0.05 else ""
        invoice_id = f"{prefix}{536000 + n}"
        n_lines = rng.randint(1, 8)
        for j in range(n_lines):
            prod = rng.choice(merch)
            qty = rng.randint(1, 24)
            if prefix == "C":
                qty = -qty
            line_type = "cancellation" if prefix == "C" else "sale"
            if prefix == "" and kind > 0.985 and j == 0:
                line_type, qty, price = "stock_write_off", -qty, 0.0
            else:
                price = float(prod[2])
            lines.append((invoice_id, ts, cid, country[0], prod[0], line_type, qty, price))
        if prefix == "" and rng.random() < 0.3:
            lines.append((invoice_id, ts, cid, country[0], post[0], "sale", 1, 18.0))
        if cid and prefix == "":
            amount = sum(
                q * p
                for inv, _, _, _, _, lt, q, p in lines[-n_lines - 1 :]
                if inv == invoice_id and lt == "sale"
            )
            spend[cid] = spend.get(cid, 0.0) + amount
            tier = next(name for threshold, name in TIERS if spend[cid] >= threshold)
            hist = versions.setdefault(cid, [])
            if not hist or hist[-1][1] != tier:
                hist.append((ts, tier))
    # SCD2 customer dimension (version 1 back-dated to 1900-01-01, like dbt)
    country_of = {cid: country[1] for cid, country in customers}
    rows = []
    sk_lookup: list[tuple[str, datetime, datetime, str]] = []
    for cid, hist in versions.items():
        for v, (since, tier) in enumerate(hist, start=1):
            valid_from = datetime(1900, 1, 1) if v == 1 else since
            valid_to = hist[v][0] if v < len(hist) else datetime(9999, 12, 31)
            sk = _md5(cid, str(since))
            rows.append(
                (
                    sk,
                    cid,
                    country_of[cid],
                    tier,
                    v,
                    valid_from,
                    valid_to,
                    v == len(hist),
                    hist[0][0],
                )
            )
            sk_lookup.append((cid, valid_from, valid_to, sk))
    rows.append(
        (
            "-1",
            "-1",
            "Unknown",
            "unknown",
            1,
            datetime(1900, 1, 1),
            datetime(9999, 12, 31),
            True,
            None,
        )
    )
    _bulk_insert(con, "dim_customer", rows)
    fact = []
    for k, (invoice_id, ts, cid, country_key, product_key, line_type, qty, price) in enumerate(
        lines
    ):
        sk = "-1"
        if cid:
            sk = next((s for c, vf, vt, s in sk_lookup if c == cid and vf <= ts < vt), "-1")
        revenue = round(qty * price, 3)
        fact.append(
            (
                _md5(str(k), invoice_id, product_key),
                invoice_id,
                int(ts.strftime("%Y%m%d")),
                sk,
                cid,
                product_key,
                country_key,
                ts,
                line_type,
                qty,
                price,
                revenue,
                abs(qty) >= 10000,
                f"batch_{ts:%Y_%m}",
                ts + timedelta(days=1),
            )
        )
    _bulk_insert(con, "fct_order_lines", fact)


def _derived(con: duckdb.DuckDBPyConnection) -> None:
    # fct_orders: same logic as models/marts/core/fct_orders.sql
    con.execute(
        """
        INSERT INTO gold.fct_orders
        SELECT invoice_id, min(customer_sk), min(customer_id), min(country_key), min(date_key),
               min(invoice_ts),
               CASE WHEN invoice_id LIKE 'C%' THEN 'cancellation'
                    WHEN invoice_id LIKE 'A%' THEN 'bad_debt_adjustment'
                    WHEN max(quantity) < 0 THEN 'stock_write_off' ELSE 'sale' END,
               count(*)::INTEGER, count(DISTINCT f.product_key)::INTEGER, sum(quantity)::BIGINT,
               sum(line_revenue)::DECIMAL(18, 3),
               sum(CASE WHEN p.is_merchandise THEN line_revenue ELSE 0 END)::DECIMAL(18, 3),
               max(CASE WHEN is_quantity_outlier THEN 1 ELSE 0 END) = 1
        FROM gold.fct_order_lines f JOIN gold.dim_product p ON f.product_key = p.product_key
        GROUP BY invoice_id
        """
    )
    # mart_revenue_monthly: same logic as models/marts/analytics/mart_revenue_monthly.sql
    con.execute(
        """
        INSERT INTO gold.mart_revenue_monthly
        WITH lines AS (
            SELECT f.invoice_id, f.customer_id, f.line_type, f.line_revenue, p.is_merchandise,
                   d.first_day_of_month
            FROM gold.fct_order_lines f
            JOIN gold.dim_product p ON f.product_key = p.product_key
            JOIN gold.dim_date d ON f.date_key = d.date_key
        ), bounds AS (SELECT max(invoice_ts) AS data_end_ts FROM gold.fct_order_lines),
        monthly AS (
            SELECT first_day_of_month AS revenue_month,
                   count(DISTINCT CASE WHEN line_type = 'sale' THEN invoice_id END) AS n_orders,
                   count(DISTINCT CASE WHEN line_type = 'sale' THEN customer_id END) AS n_active_customers,
                   sum(CASE WHEN line_type = 'sale' AND is_merchandise THEN line_revenue ELSE 0 END) AS gross,
                   sum(CASE WHEN line_type = 'cancellation' AND is_merchandise THEN line_revenue ELSE 0 END) AS cancelled,
                   sum(CASE WHEN line_type IN ('sale', 'cancellation') AND is_merchandise THEN line_revenue ELSE 0 END) AS net,
                   sum(CASE WHEN NOT is_merchandise THEN line_revenue ELSE 0 END) AS non_merch,
                   sum(line_revenue) AS total
            FROM lines GROUP BY first_day_of_month
        ), g AS (
            SELECT *, lag(net) OVER (ORDER BY revenue_month) AS prev,
                   lag(net, 12) OVER (ORDER BY revenue_month) AS ly,
                   sum(net) OVER (PARTITION BY year(revenue_month) ORDER BY revenue_month
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS ytd
            FROM monthly
        )
        SELECT revenue_month, n_orders, n_active_customers, gross::DECIMAL(18, 2),
               cancelled::DECIMAL(18, 2), net::DECIMAL(18, 2), non_merch::DECIMAL(18, 2),
               total::DECIMAL(18, 3), (net / nullif(n_orders, 0))::DECIMAL(18, 2),
               round(((net - prev) / nullif(prev, 0))::DOUBLE, 4),
               round(((net - ly) / nullif(ly, 0))::DOUBLE, 4), ytd::DECIMAL(18, 2),
               (revenue_month + INTERVAL 1 MONTH)::TIMESTAMP > (SELECT data_end_ts FROM bounds)
        FROM g
        """
    )
    # mart_cohort_retention: same logic as models/marts/analytics/mart_cohort_retention.sql
    con.execute(
        """
        INSERT INTO gold.mart_cohort_retention
        WITH activity AS (
            SELECT DISTINCT customer_id, date_trunc('month', invoice_ts)::DATE AS activity_month
            FROM gold.fct_order_lines WHERE customer_id IS NOT NULL AND line_type = 'sale'
        ), cohorts AS (SELECT customer_id, min(activity_month) AS cohort_month FROM activity GROUP BY 1),
        sizes AS (SELECT cohort_month, count(*) AS cohort_size FROM cohorts GROUP BY 1),
        retention AS (
            SELECT c.cohort_month, datediff('month', c.cohort_month, a.activity_month)::INTEGER AS m,
                   count(DISTINCT a.customer_id) AS active
            FROM activity a JOIN cohorts c ON a.customer_id = c.customer_id GROUP BY 1, 2
        )
        SELECT r.cohort_month, r.m, s.cohort_size, r.active,
               round(r.active::DOUBLE / s.cohort_size, 4),
               r.cohort_month = (SELECT min(cohort_month) FROM cohorts)
        FROM retention r JOIN sizes s ON r.cohort_month = s.cohort_month
        """
    )
