"""
Stage 4 of the safety layer: guarded execution.

`SafeDuckDBExecutor` runs validated SQL on a role's replica, and the database refuses to do
anything but read even if a statement slipped past the validator:
  - the replica file is opened `read_only=True` (any CREATE/INSERT/COPY ... fails);
  - `enable_external_access = false`: no file or network reads, no ATTACH, no INSTALL/LOAD;
  - extension autoload/autoinstall and community extensions are off;
  - `lock_configuration = true`: no SET can undo any of the above;
  - memory and thread caps per connection;
  - cost budget: `EXPLAIN (FORMAT JSON)` estimates the rows every operator will produce
    before anything runs (the agent graph turns "over budget" into an approval interrupt);
  - wall-clock timeout: a timer calls `connection.interrupt()` on the query's own cursor;
  - row cap: at most `max_rows` rows come back, with a `truncated` flag.

`SafeSQLiteExecutor` is v1's SQLite executor (read-only URI, authorizer, progress-handler
timeout, row cap), kept for the BIRD and Spider2 benchmarks, whose databases are SQLite.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import math
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from data_agent.safety.validator import FORBIDDEN_FUNCTIONS


class ExecutionError(Exception):
    """The database rejected or failed to run the query."""

    kind = "db_error"


class QueryTimeoutError(ExecutionError):
    kind = "timeout"


class QueryMemoryError(ExecutionError):
    kind = "memory"


class ReadOnlyViolation(ExecutionError):
    """The database itself refused a write: the validator was bypassed. Must never happen."""

    kind = "read_only_violation"


@dataclass
class ExecutionResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    elapsed_ms: float
    column_types: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass
class CostEstimate:
    """Rows the planner expects each operator to produce, summed and maxed over the plan."""

    total_rows: float
    max_rows: float
    operators: list[tuple[str, float]]
    unestimated: int = 0

    def to_dict(self) -> dict:
        return {
            "estimated_rows_total": self.total_rows,
            "estimated_rows_max_operator": self.max_rows,
            "operators": len(self.operators),
            "unestimated_operators": self.unestimated,
        }


def to_jsonable(value: Any) -> Any:
    """DuckDB/SQLite values -> JSON-safe Python values (Decimal as float, dates as ISO)."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list | tuple):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    return str(value)


def _estimate(node: dict) -> tuple[float, list[tuple[str, float]], int]:
    """Bottom-up cardinality estimate of one plan node (and its subtree)."""
    child_out, ops, unest = [], [], 0
    for child in node.get("children", []):
        rows, child_ops, child_unest = _estimate(child)
        child_out.append(rows)
        ops += child_ops
        unest += child_unest
    raw = (node.get("extra_info") or {}).get("Estimated Cardinality")
    name = node.get("name", "?")
    if raw is not None:
        try:
            rows = float(str(raw).replace(",", ""))
        except ValueError:
            rows, unest = max(child_out, default=1.0), unest + 1
    elif name in ("CROSS_PRODUCT", "NESTED_LOOP_JOIN", "BLOCKWISE_NL_JOIN") and len(child_out) == 2:
        rows = child_out[0] * child_out[1]  # DuckDB omits estimates for cross products
    else:
        rows = max(child_out, default=1.0)
        unest += 1
    ops.append((name, rows))
    return rows, ops, unest


def parse_explain_json(plan_json: str) -> CostEstimate:
    plan = json.loads(plan_json)
    roots = plan if isinstance(plan, list) else [plan]
    ops: list[tuple[str, float]] = []
    unest = 0
    for root in roots:
        _, root_ops, root_unest = _estimate(root)
        ops += root_ops
        unest += root_unest
    total = sum(r for _, r in ops)
    return CostEstimate(total, max((r for _, r in ops), default=0.0), ops, unest)


class SafeDuckDBExecutor:
    def __init__(
        self,
        db_path: str | Path,
        timeout_s: float = 15.0,
        max_rows: int = 200,
        threads: int = 4,
        memory_limit: str = "1GB",
        temp_directory: str | Path | None = None,
    ):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"replica not found: {self.db_path} (run `data-agent build-replicas`)"
            )
        self.timeout_s = timeout_s
        self.max_rows = max_rows
        config: dict[str, Any] = {
            "threads": threads,
            "memory_limit": memory_limit,
            "enable_external_access": False,
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
            "allow_community_extensions": False,
            "lock_configuration": True,
        }
        if temp_directory is not None:
            config["temp_directory"] = str(temp_directory)
            config["max_temp_directory_size"] = "2GB"
        self._con = duckdb.connect(str(self.db_path), read_only=True, config=config)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._con.close()

    def tables(self) -> dict[str, dict[str, str]]:
        """{table: {column: type}} as stored in the replica (main schema)."""
        with self._lock:
            cur = self._con.cursor()
        try:
            rows = cur.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
            ).fetchall()
        finally:
            cur.close()
        out: dict[str, dict[str, str]] = {}
        for table, col, dtype in rows:
            out.setdefault(table, {})[col] = dtype
        return out

    def row_counts(self) -> dict[str, int]:
        with self._lock:
            cur = self._con.cursor()
        try:
            return {
                t: cur.execute(f'SELECT count(*) FROM main."{t}"').fetchone()[0]
                for t in self.tables()
            }
        finally:
            cur.close()

    def explain(self, sql: str) -> CostEstimate:
        with self._lock:
            cur = self._con.cursor()
        try:
            rows = cur.execute("EXPLAIN (FORMAT JSON) " + sql).fetchall()
        except duckdb.Error as e:
            raise _classify(e) from e
        finally:
            cur.close()
        plan_json = next((r[1] for r in rows if r[0] == "physical_plan"), rows[0][1])
        return parse_explain_json(plan_json)

    def execute(
        self, sql: str, max_rows: int | None = -1, timeout_s: float | None = None
    ) -> ExecutionResult:
        """Run one validated statement. `max_rows=None` fetches everything (evaluation)."""
        limit = self.max_rows if max_rows == -1 else max_rows
        timeout = self.timeout_s if timeout_s is None else timeout_s
        with self._lock:  # cursor creation is not thread-safe; execution on cursors is
            cur = self._con.cursor()
        fired = threading.Event()

        def _interrupt() -> None:
            fired.set()
            cur.interrupt()

        timer = threading.Timer(timeout, _interrupt)
        timer.daemon = True
        start = time.perf_counter()
        timer.start()
        try:
            rel = cur.execute(sql)
            description = rel.description or []
            columns = [d[0] for d in description]
            types = [str(d[1]) for d in description]
            if limit is None:
                raw, truncated = rel.fetchall(), False
            else:
                raw = rel.fetchmany(limit + 1)
                truncated = len(raw) > limit
                raw = raw[:limit]
        except duckdb.Error as e:
            if fired.is_set() or isinstance(e, duckdb.InterruptException):
                raise QueryTimeoutError(f"Query exceeded the {timeout:.0f}s time limit") from e
            raise _classify(e) from e
        finally:
            timer.cancel()
            cur.close()
        rows = [[to_jsonable(v) for v in row] for row in raw]
        return ExecutionResult(
            columns=columns,
            rows=rows,
            truncated=truncated,
            elapsed_ms=round((time.perf_counter() - start) * 1000, 2),
            column_types=types,
        )


def _classify(e: duckdb.Error) -> ExecutionError:
    text = str(e).splitlines()[0] if str(e) else type(e).__name__
    if isinstance(e, duckdb.OutOfMemoryException):
        return QueryMemoryError(f"Query exceeded the memory budget: {text}")
    lowered = text.lower()
    if "read-only" in lowered or "read only" in lowered:
        return ReadOnlyViolation(text)
    return ExecutionError(text)


# ----------------------------------------------------------------------------------
# SQLite (benchmarks) - ported from text-to-sql-llm-platform v1 src/safety/executor.py
# ----------------------------------------------------------------------------------

_SQLITE_READ = getattr(sqlite3, "SQLITE_READ", 20)
_SQLITE_SELECT = getattr(sqlite3, "SQLITE_SELECT", 21)
_SQLITE_FUNCTION = getattr(sqlite3, "SQLITE_FUNCTION", 31)
_SQLITE_RECURSIVE = getattr(sqlite3, "SQLITE_RECURSIVE", 33)
_READ_ONLY_ACTIONS = frozenset({_SQLITE_READ, _SQLITE_SELECT, _SQLITE_RECURSIVE})


def _authorizer(action: int, arg1, arg2, db_name, trigger) -> int:
    if action in _READ_ONLY_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == _SQLITE_FUNCTION:
        name = (arg2 or "").lower()
        return sqlite3.SQLITE_DENY if name in FORBIDDEN_FUNCTIONS else sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class SafeSQLiteExecutor:
    """Read-only URI + `PRAGMA query_only` + authorizer + progress-handler timeout + row cap."""

    def __init__(self, timeout_s: float = 30.0, max_rows: int | None = 200):
        self.timeout_s = timeout_s
        self.max_rows = max_rows

    def execute(
        self,
        db_path: str | Path,
        sql: str,
        max_rows: int | None = -1,
        timeout_s: float | None = None,
    ) -> ExecutionResult:
        limit = self.max_rows if max_rows == -1 else max_rows
        timeout = self.timeout_s if timeout_s is None else timeout_s
        db_path = Path(db_path)
        if not db_path.exists():
            raise ExecutionError(f"Database file not found: {db_path.name}")
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0, check_same_thread=False)
        conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()  # load schema first
        conn.set_authorizer(_authorizer)
        deadline = time.monotonic() + timeout
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1_000)
        start = time.perf_counter()
        try:
            cursor = conn.execute(sql)
            columns = [d[0] for d in (cursor.description or [])]
            if limit is None:
                raw, truncated = cursor.fetchall(), False
            else:
                raw = cursor.fetchmany(limit + 1)
                truncated = len(raw) > limit
                raw = raw[:limit]
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e).lower():
                raise QueryTimeoutError(f"Query exceeded the {timeout:.0f}s time limit") from e
            raise ExecutionError(str(e)) from e
        except (sqlite3.Error, sqlite3.Warning) as e:
            raise ExecutionError(str(e)) from e
        finally:
            conn.close()
        return ExecutionResult(
            columns=columns,
            rows=[[to_jsonable(v) for v in row] for row in raw],
            truncated=truncated,
            elapsed_ms=round((time.perf_counter() - start) * 1000, 2),
        )
