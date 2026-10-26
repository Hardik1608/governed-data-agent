"""Masking audit of the analyst replica, on the real warehouse, with a positive control.

1. Every customer-identifying column of the masked replica (customer_id, customer_sk,
   order_line_id in every model) is compared with the set of real customer ids: matches must be
   0, and every value must be a token (cust_ / sk_ / ol_) or the dbt "unknown" member (-1).
2. Re-identification attack on the dbt surrogate key (the snapshot's dbt_scd_id, an md5 of
   customer_id || '|' || the snapshot timestamp, which the mart exposes as valid_from for later
   versions and as first_invoice_at for most first versions): an attacker who sees a row's
   customer_sk and timestamps tries every real customer id with each timestamp.
   Positive control: the same attack on the source warehouse, where the dbt keys are intact.
   Negative result required on the masked replica.

The warehouse is opened read-only; nothing is written except the report.
Output: reports/governance/masking_audit.{json,md}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_agent.config import Settings  # noqa: E402
from data_agent.warehouse.replica import replica_file  # noqa: E402

ID_COLUMNS = ("customer_id", "customer_sk", "order_line_id")
TOKEN_PREFIXES = ("cust_", "sk_", "ol_")
ATTACK = """
    WITH k AS (
        SELECT DISTINCT customer_sk, CAST(ts AS VARCHAR) AS ts
        FROM (SELECT customer_sk, valid_from AS ts FROM {table}
              UNION ALL SELECT customer_sk, first_invoice_at FROM {table})
        WHERE customer_sk IS NOT NULL AND customer_sk <> '-1' AND ts IS NOT NULL
    )
    SELECT
        (SELECT count(DISTINCT customer_sk) FROM k) AS keys_attacked,
        (SELECT count(DISTINCT k.customer_sk) FROM k, ids AS i
         WHERE md5(i.customer_id || '|' || k.ts) = k.customer_sk) AS keys_reidentified
"""


def main() -> int:
    s = Settings()
    masked = replica_file(s.replica_path, "masked")
    con = duckdb.connect(config={"threads": 2})
    try:
        con.execute(f"ATTACH '{_q(s.warehouse_path)}' AS src (READ_ONLY)")
        con.execute(f"ATTACH '{_q(masked)}' AS m (READ_ONLY)")
        con.execute(
            "CREATE TEMP TABLE ids AS SELECT DISTINCT CAST(customer_id AS VARCHAR) AS customer_id "
            "FROM src.gold.dim_customer WHERE customer_id IS NOT NULL AND customer_id <> '-1'"
        )
        n_ids = con.execute("SELECT count(*) FROM ids").fetchone()[0]

        columns = con.execute(
            "SELECT table_name, column_name FROM duckdb_columns() "
            "WHERE database_name = 'm' AND column_name IN ("
            + ", ".join(f"'{c}'" for c in ID_COLUMNS)
            + ")"
        ).fetchall()
        scan = []
        for table, column in sorted(columns):
            ref = f'm.main."{table}"."{column}"'
            raw_hits, not_token, total = con.execute(
                f"SELECT count(*) FILTER (WHERE CAST({ref} AS VARCHAR) IN (SELECT customer_id FROM ids)), "
                f"count(*) FILTER (WHERE {ref} IS NOT NULL AND CAST({ref} AS VARCHAR) <> '-1' AND NOT ("
                + " OR ".join(f"starts_with(CAST({ref} AS VARCHAR), '{p}')" for p in TOKEN_PREFIXES)
                + f')), count({ref}) FROM m.main."{table}"'
            ).fetchone()
            scan.append(
                {
                    "table": table,
                    "column": column,
                    "non_null_values": total,
                    "raw_customer_ids": raw_hits,
                    "values_not_tokenised": not_token,
                }
            )

        source = con.execute(ATTACK.format(table="src.gold.dim_customer")).fetchone()
        target = con.execute(ATTACK.format(table='m.main."dim_customer"')).fetchone()
    finally:
        con.close()

    report = {
        "warehouse": s.warehouse_path.name,
        "replica": masked.name,
        "real_customer_ids": n_ids,
        "identifier_columns": scan,
        "raw_customer_ids_in_replica": sum(c["raw_customer_ids"] for c in scan),
        "values_not_tokenised": sum(c["values_not_tokenised"] for c in scan),
        "md5_key_attack": {
            "formula": "customer_sk = md5(customer_id || '|' || ts), ts in (valid_from, first_invoice_at)",
            "candidates_per_key": n_ids,
            "source_warehouse": {"keys_attacked": source[0], "keys_reidentified": source[1]},
            "masked_replica": {"keys_attacked": target[0], "keys_reidentified": target[1]},
        },
    }
    out = ROOT / "reports" / "governance"
    out.mkdir(parents=True, exist_ok=True)
    (out / "masking_audit.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    a = report["md5_key_attack"]
    lines = [
        "# Masking audit: analyst replica vs real customer ids",
        "",
        f"Warehouse `{report['warehouse']}`; replica `{report['replica']}`; "
        f"{n_ids:,} real customer ids.",
        "",
        "| Model | Column | Non-null values | Raw customer ids | Values not tokenised |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {c['table']} | {c['column']} | {c['non_null_values']:,} | {c['raw_customer_ids']} "
        f"| {c['values_not_tokenised']} |"
        for c in scan
    ]
    lines += [
        "",
        "Re-identification attack on the dbt key `customer_sk = md5(customer_id || '|' || ts)`, "
        f"trying all {n_ids:,} ids with each row's `valid_from` and `first_invoice_at`:",
        "",
        "| Target | Keys attacked | Keys re-identified |",
        "|---|---|---|",
        f"| source warehouse (dbt keys, positive control) | {a['source_warehouse']['keys_attacked']:,} "
        f"| {a['source_warehouse']['keys_reidentified']:,} |",
        f"| analyst replica (re-keyed) | {a['masked_replica']['keys_attacked']:,} "
        f"| {a['masked_replica']['keys_reidentified']:,} |",
    ]
    (out / "masking_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


def _q(path: Path) -> str:
    return str(path).replace("'", "''")


if __name__ == "__main__":
    raise SystemExit(main())
