"""SQLite benchmark databases (BIRD, Spider2-lite) as agent backends.

Each database becomes what the agent sees for the sales warehouse: a catalog (tables,
columns, types, descriptions, sample values), a hybrid search index, and a guard that
validates SQL (the same validator, SQLite dialect) and runs it on a read-only connection
(the v1 SQLite executor: read-only URI, authorizer, timeout, row cap). There is no PII and no
metric layer, and no EXPLAIN budget (SQLite gives no row estimates); timeout and row cap stay.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from data_agent.governance.policy import RolePolicy
from data_agent.safety.executor import ExecutionError, ExecutionResult, SafeSQLiteExecutor
from data_agent.safety.guard import ALLOW, REJECT, GuardDecision
from data_agent.safety.validator import SQLValidator
from data_agent.warehouse.catalog import Catalog, ColumnDoc, ModelDoc


@dataclass
class SqliteDb:
    db_id: str
    path: Path
    tables: dict[str, dict[str, str]]  # table -> {column: type}
    primary_keys: dict[str, list[str]] = field(default_factory=dict)
    foreign_keys: list[tuple[str, str, str, str]] = field(default_factory=list)
    descriptions: dict[tuple[str, str], str] = field(default_factory=dict)

    def ddl(self) -> str:
        """CREATE TABLE statements with PK/FK lines (v1's rendering; FKs tell the model how to join)."""
        out = []
        for table, cols in self.tables.items():
            lines = [f"  {_q(c)} {t.upper()}" for c, t in cols.items()]
            if self.primary_keys.get(table):
                lines.append(
                    f"  PRIMARY KEY ({', '.join(_q(c) for c in self.primary_keys[table])})"
                )
            for t, c, rt, rc in self.foreign_keys:
                if t == table:
                    lines.append(f"  FOREIGN KEY ({_q(c)}) REFERENCES {_q(rt)}({_q(rc)})")
            out.append(f"CREATE TABLE {_q(table)} (\n" + ",\n".join(lines) + "\n);")
        return "\n\n".join(out)


def _q(name: str) -> str:
    return name if name.replace("_", "").isalnum() and not name[0].isdigit() else f"`{name}`"


def load_spider_style_schemas(tables_json: Path, db_root: Path) -> dict[str, SqliteDb]:
    """Spider/BIRD tables.json -> SqliteDb per database (with BIRD column descriptions if present)."""
    out = {}
    for entry in json.loads(Path(tables_json).read_text(encoding="utf-8")):
        db_id = entry["db_id"]
        names = entry["table_names_original"]
        tables: dict[str, dict[str, str]] = {t: {} for t in names}
        col_ref: dict[int, tuple[str, str]] = {}
        for idx, (t_idx, col) in enumerate(entry["column_names_original"]):
            if t_idx < 0:
                continue
            ctype = entry["column_types"][idx]
            tables[names[t_idx]][col] = {
                "number": "REAL",
                "time": "TEXT",
                "others": "TEXT",
                "boolean": "INTEGER",
            }.get(ctype, ctype.upper())
            col_ref[idx] = (names[t_idx], col)
        pks: dict[str, list[str]] = {}
        for pk in entry.get("primary_keys", []):
            for idx in pk if isinstance(pk, list) else [pk]:
                if idx in col_ref:
                    t, c = col_ref[idx]
                    pks.setdefault(t, []).append(c)
        fks = [
            (*col_ref[a], *col_ref[b])
            for a, b in entry.get("foreign_keys", [])
            if a in col_ref and b in col_ref
        ]
        path = Path(db_root) / db_id / f"{db_id}.sqlite"
        db = SqliteDb(db_id, path, tables, pks, fks)
        db.descriptions = _bird_descriptions(Path(db_root) / db_id / "database_description", tables)
        out[db_id] = db
    return out


def _bird_descriptions(
    folder: Path, tables: dict[str, dict[str, str]]
) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    if not folder.exists():
        return out
    lower = {t.lower(): t for t in tables}
    for path in folder.glob("*.csv"):
        table = lower.get(path.stem.lower())
        if table is None:
            continue
        raw = path.read_bytes()
        text = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        cols = {c.lower(): c for c in tables[table]}
        for row in csv.DictReader(text.splitlines()):
            name = (row.get("original_column_name") or "").strip()
            col = cols.get(name.lower())
            if not col:
                continue
            bits = [
                (row.get("column_description") or "").strip(),
                (row.get("value_description") or "").strip(),
            ]
            desc = " ".join(" ".join(b.split()) for b in bits if b)
            if desc:
                out[(table, col)] = desc[:400]
    return out


def sample_values(
    db: SqliteDb, per_column: int = 3, max_len: int = 40
) -> dict[tuple[str, str], list[str]]:
    con = sqlite3.connect(db.path.resolve().as_uri() + "?mode=ro", uri=True)
    con.text_factory = lambda b: b.decode("utf-8", errors="replace")
    out = {}
    try:
        for table, cols in db.tables.items():
            for col in cols:
                try:
                    rows = con.execute(
                        f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL LIMIT {per_column}'  # noqa: S608
                    ).fetchall()
                except sqlite3.Error:
                    continue
                out[(table, col)] = [str(r[0])[:max_len] for r in rows]
    finally:
        con.close()
    return out


def catalog_for(db: SqliteDb, samples: dict[tuple[str, str], list[str]] | None = None) -> Catalog:
    models = {}
    for table, cols in db.tables.items():
        docs = []
        for col, ctype in cols.items():
            desc = db.descriptions.get((table, col), "")
            if samples and samples.get((table, col)):
                desc = (
                    (desc + " " if desc else "") + "Examples: " + ", ".join(samples[(table, col)])
                )
            tests = ["primary key"] if col in db.primary_keys.get(table, []) else []
            for t, c, rt, rc in db.foreign_keys:
                if t == table and c == col:
                    tests.append(f"relationship -> {rt}.{rc}")
            docs.append(ColumnDoc(col, ctype, desc, tests))
        models[table.lower()] = ModelDoc(
            name=table, description=f"Table {table} of the {db.db_id} database.", columns=docs
        )
    return Catalog(models, source=f"{db.db_id} (SQLite)")


class SqliteGuard:
    """Same interface as SqlGuard: validate (SQLite dialect) then run read-only."""

    def __init__(self, db: SqliteDb, timeout_s: float = 30.0, max_rows: int = 200):
        self.db = db
        self.executor = SafeSQLiteExecutor(timeout_s=timeout_s, max_rows=max_rows)
        self.validator = SQLValidator(db.tables, dialect="sqlite")
        self.role = RolePolicy(db.db_id, replica="", models=tuple(db.tables))

    def check(self, sql: str, reveal_pii: bool = False) -> GuardDecision:
        validation = self.validator.validate(sql)
        decision = GuardDecision(REJECT, sql, validation=validation)
        if not validation.ok:
            decision.reasons.append(validation.feedback())
            return decision
        decision.safe_sql = validation.safe_sql
        decision.decision = ALLOW
        return decision

    def execute(
        self, decision: GuardDecision, approved: bool = False, max_rows: int | None = -1
    ) -> ExecutionResult:
        if decision.safe_sql is None:
            raise ExecutionError("refusing to execute a rejected query")
        return self.executor.execute(self.db.path, decision.safe_sql, max_rows=max_rows)
