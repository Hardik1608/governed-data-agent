"""
SQL safety layer, stages 1-3: static validation of model-generated SQL.

Ported from text-to-sql-llm-platform v1 (`src/safety/sql_validator.py`, MIT, same author)
and adapted to the DuckDB dialect. LLM output is untrusted input; every query passes:

  Stage 1  AST parse        : sqlglot must parse it, and it must be exactly ONE statement
                              (blocks stacked queries such as `SELECT 1; DROP TABLE t`).
  Stage 2  Read-only        : the root must be SELECT / UNION / INTERSECT / EXCEPT and no
                              write, DDL or session node may appear anywhere in the tree (DML
                              hidden in CTEs, SELECT ... INTO, COPY, ATTACH, PRAGMA, SET,
                              INSTALL/LOAD). Table functions are rejected (read_csv, query(),
                              glob(), range(), duckdb_*()), and functions that reach outside
                              the query are denied by name (getenv, current_setting, nextval).
  Stage 3  Schema allowlist : every table must be one of the role's models (or a CTE), with no
                              catalog/schema prefix, and every column must resolve against the
                              schema (hallucinated identifiers are caught before execution).
                              PII-tagged columns referenced by the query are reported, which
                              drives the approval guard.
  Stage 4  Execution guard  : `data_agent.safety.executor` - a read-only DuckDB replica with
                              external access disabled and configuration locked, an EXPLAIN
                              cost estimate, a wall-clock timeout (connection.interrupt()) and
                              a row cap.

The validator returns `safe_sql`, the re-serialised AST. The executor runs exactly that text,
which closes parser-differential tricks (comments, odd quoting) between the validator and the
database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, TokenError
from sqlglot.optimizer.qualify import qualify

logging.getLogger("sqlglot").setLevel(logging.ERROR)

_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Merge", "Drop", "Create", "Alter", "TruncateTable",
    "Command", "Pragma", "Attach", "Detach", "Transaction", "Commit", "Rollback", "Set",
    "Use", "Grant", "Revoke", "Into", "Copy", "Analyze", "Cache", "Uncache", "LoadData",
    "Kill", "Lock", "Describe", "Show", "Install", "Export", "Summarize", "Refresh",
    "Comment", "Execute", "Declare",
)  # fmt: skip
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name) for name in _FORBIDDEN_NODE_NAMES if hasattr(exp, name)
)
ALLOWED_ROOTS: tuple[type[exp.Expression], ...] = (exp.Select, exp.SetOperation)

# DuckDB functions that read files or the network, run SQL text, expose settings or
# secrets, or change session state. Most are also stopped by the database
# (enable_external_access = false, locked configuration); the validator rejects them first
# so the model gets a clear message instead of a permission error.
FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {
        "getenv", "current_setting", "nextval", "currval", "setseed", "query", "query_table",
        "glob", "sniff_csv", "load_aws_credentials", "which_secret", "read_text", "read_blob",
        "parquet_scan", "json_execute_serialized_sql", "sql_auto_complete", "checkpoint",
        "force_checkpoint", "enable_profiling", "disable_profiling", "summary", "make_secret",
        "install", "load", "pg_sleep", "sleep", "sleep_ms",
        # SQLite (benchmark backend) - kept from v1
        "load_extension", "readfile", "writefile", "edit", "fts3_tokenizer",
    }
)  # fmt: skip
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "read_", "duckdb_", "pragma_", "parquet_", "iceberg_", "delta_", "sqlite_", "postgres_",
    "mysql_", "json_execute", "st_read", "http_", "s3_", "azure_", "gcs_",
)  # fmt: skip

MAX_AST_NODES = 5_000

# Violation codes (also the `reason` label of the validation-failure metric)
EMPTY = "empty"
TOO_LONG = "too_long"
TOO_COMPLEX = "too_complex"
PARSE_ERROR = "parse_error"
MULTIPLE_STATEMENTS = "multiple_statements"
NOT_READ_ONLY = "not_read_only"
FORBIDDEN_FUNCTION = "forbidden_function"
TABLE_FUNCTION = "table_function"
QUALIFIED_REFERENCE = "qualified_reference"
UNKNOWN_TABLE = "unknown_table"
UNKNOWN_COLUMN = "unknown_column"

HALLUCINATION_CODES = frozenset({UNKNOWN_TABLE, UNKNOWN_COLUMN})
# Codes that mean "the query tried to do something it must never do" (counted as unsafe
# attempts by the eval, whether or not the model was manipulated into it).
UNSAFE_CODES = frozenset(
    {MULTIPLE_STATEMENTS, NOT_READ_ONLY, FORBIDDEN_FUNCTION, TABLE_FUNCTION, QUALIFIED_REFERENCE}
)


@dataclass(frozen=True)
class Violation:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass
class ValidationResult:
    sql: str
    safe_sql: str | None = None
    violations: list[Violation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    pii_columns: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations and self.safe_sql is not None

    @property
    def codes(self) -> list[str]:
        return [v.code for v in self.violations]

    @property
    def unsafe(self) -> bool:
        return bool(UNSAFE_CODES & set(self.codes))

    def feedback(self) -> str:
        """Human-readable summary, fed back to the model so it can repair the query."""
        return "; ".join(v.message for v in self.violations)


class SQLValidator:
    """Static validation of one model-generated query against a role's schema.

    `schema` maps table name -> {column: type}. `pii` maps table name -> PII column names.
    """

    def __init__(
        self,
        schema: dict[str, dict[str, str]] | None,
        dialect: str = "duckdb",
        pii: dict[str, set[str]] | None = None,
        check_columns: bool = True,
        max_length: int = 10_000,
        allowed_schemas: tuple[str, ...] = ("main",),
    ):
        self.schema = {t.lower(): cols for t, cols in (schema or {}).items()}
        self.table_names = {t.lower(): t for t in (schema or {})}
        self.dialect = dialect
        self.pii = {t.lower(): {c.lower() for c in cols} for t, cols in (pii or {}).items()}
        self.check_columns = check_columns
        self.max_length = max_length
        self.allowed_schemas = tuple(s.lower() for s in allowed_schemas)

    def validate(self, sql: str) -> ValidationResult:
        result = ValidationResult(sql=sql)
        text = (sql or "").strip()

        # ---- Stage 1: parse, exactly one statement --------------------------------
        if not text:
            return _fail(result, EMPTY, "The query is empty.")
        if len(text) > self.max_length:
            return _fail(result, TOO_LONG, f"The query exceeds {self.max_length} characters.")
        try:
            statements = [s for s in sqlglot.parse(text, read=self.dialect) if s is not None]
        except (ParseError, TokenError) as e:
            return _fail(result, PARSE_ERROR, f"SQL syntax error: {_first_line(e)}")
        except Exception as e:  # sqlglot can raise ValueError/RecursionError on odd input
            return _fail(result, PARSE_ERROR, f"SQL could not be parsed: {_first_line(e)}")
        if not statements:
            return _fail(result, EMPTY, "The query is empty.")
        if len(statements) > 1:
            return _fail(
                result,
                MULTIPLE_STATEMENTS,
                f"Expected exactly one statement, got {len(statements)}.",
            )
        root = statements[0]
        while isinstance(root, exp.Subquery | exp.Paren) and root.this is not None:
            root = root.this

        # ---- Stage 2: read-only ----------------------------------------------------
        if not isinstance(root, ALLOWED_ROOTS):
            return _fail(
                result,
                NOT_READ_ONLY,
                f"Only SELECT queries are allowed (got {root.key.upper()}).",
            )
        n_nodes = 0
        for node in root.walk():
            n_nodes += 1
            if isinstance(node, FORBIDDEN_NODES):
                return _fail(
                    result,
                    NOT_READ_ONLY,
                    f"Write/DDL/session operation '{node.key.upper()}' is not allowed.",
                )
        if n_nodes > MAX_AST_NODES:
            return _fail(result, TOO_COMPLEX, f"The query is too complex ({n_nodes} AST nodes).")
        for func in root.find_all(exp.Func):
            name = _function_name(func)
            if name in FORBIDDEN_FUNCTIONS or name.startswith(FORBIDDEN_PREFIXES):
                return _fail(result, FORBIDDEN_FUNCTION, f"Function '{name}' is not allowed.")

        # ---- Stage 3: schema allowlist ---------------------------------------------
        cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
        referenced: list[str] = []
        for table in root.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                result.violations.append(
                    Violation(
                        TABLE_FUNCTION,
                        "Table functions are not allowed; query the catalog's tables directly.",
                    )
                )
                continue
            if table.catalog or (table.db and table.db.lower() not in self.allowed_schemas):
                result.violations.append(
                    Violation(
                        QUALIFIED_REFERENCE,
                        f"Reference '{table.sql(dialect=self.dialect)}' is not allowed; use the "
                        "bare table name of a catalog model.",
                    )
                )
                continue
            name = table.name.lower()
            if name in cte_names:
                continue
            if self.schema and name not in self.schema:
                result.violations.append(
                    Violation(
                        UNKNOWN_TABLE,
                        f"Table '{table.name}' is not available to this role. Available: "
                        f"{', '.join(sorted(self.table_names.values()))}.",
                    )
                )
            elif name not in referenced:
                referenced.append(name)
        result.tables = [self.table_names.get(t, t) for t in referenced]
        if result.violations:
            return result

        if self.schema and self.check_columns:
            violation, warning, pii = self._check_columns(root, referenced)
            if violation:
                result.violations.append(violation)
                return result
            if warning:
                result.warnings.append(warning)
            result.pii_columns = pii
        else:
            result.pii_columns = self._pii_by_name(root, referenced)

        # Unqualified table names, all statements re-serialised without comments.
        result.safe_sql = root.sql(dialect=self.dialect, comments=False)
        return result

    # ------------------------------------------------------------------
    def _check_columns(
        self, root: exp.Expression, referenced: list[str]
    ) -> tuple[Violation | None, str | None, list[tuple[str, str]]]:
        tree = root.copy()
        # SQLite runs a CTE that reads itself without the RECURSIVE keyword; mark such WITH
        # clauses recursive (on the copy used for column checks only) so the self-reference
        # resolves to the CTE's own columns instead of failing as an unknown column.
        for with_ in tree.find_all(exp.With):
            if not with_.args.get("recursive") and any(
                any(
                    t.name.lower() == cte.alias_or_name.lower()
                    for t in cte.this.find_all(exp.Table)
                )
                for cte in with_.expressions
            ):
                with_.set("recursive", True)
        if self.dialect == "sqlite":
            # SQLite quirk (v1): a double-quoted token that is not a column is a string literal
            # (WHERE country = "France"); do not report it as an unknown column.
            known = {c.lower() for cols in self.schema.values() for c in cols}
            for column in list(tree.find_all(exp.Column)):
                ident = column.this
                if (
                    isinstance(ident, exp.Identifier)
                    and ident.quoted
                    and not column.table
                    and ident.name.lower() not in known
                ):
                    column.replace(exp.Literal.string(ident.name))
        try:
            qualified = qualify(
                tree,
                schema=self.schema,
                dialect=self.dialect,
                validate_qualify_columns=True,
                quote_identifiers=False,
                identify=False,
            )
        except OptimizeError as e:
            message = _first_line(e)
            lowered = message.lower()
            if "could not be resolved" in lowered or "unknown column" in lowered:
                return Violation(UNKNOWN_COLUMN, f"Unknown column: {message}"), None, []
            if "unknown table" in lowered:
                return Violation(UNKNOWN_TABLE, f"Unknown table: {message}"), None, []
            return None, f"column check skipped: {message}", self._pii_by_name(root, referenced)
        except Exception as e:  # sqlglot limitations must never block a valid query
            msg = f"column check skipped: {type(e).__name__}: {_first_line(e)}"
            return None, msg, self._pii_by_name(root, referenced)
        return None, None, self._pii_from_qualified(qualified, referenced)

    def _pii_from_qualified(
        self, tree: exp.Expression, referenced: list[str]
    ) -> list[tuple[str, str]]:
        """PII columns read by the query, resolved to their base table.

        After `qualify`, projection stars are expanded and every column carries the alias of
        the relation it comes from. A column read from a base table is attributed to that
        table; a PII column read from a base table inside a CTE or subquery is caught at the
        point where it is read, so it is tagged even if it is renamed later.
        """
        found: set[tuple[str, str]] = set()
        for select in tree.find_all(exp.Select):
            aliases = {}
            from_ = select.args.get("from_") or select.args.get("from")
            sources = [from_.this] if from_ is not None else []
            sources += [j.this for j in select.args.get("joins") or []]
            for source in sources:
                if isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier):
                    aliases[(source.alias_or_name or source.name).lower()] = source.name.lower()
            for col in select.find_all(exp.Column):
                if col.find_ancestor(exp.Select) is not select:
                    continue
                table = aliases.get(col.table.lower()) if col.table else None
                if table and table in self.pii and col.name.lower() in self.pii[table]:
                    found.add((table, col.name.lower()))
        return sorted(found)

    def _pii_by_name(self, tree: exp.Expression, referenced: list[str]) -> list[tuple[str, str]]:
        """Conservative fallback when qualification fails: match PII columns by name."""
        names = {c.name.lower() for c in tree.find_all(exp.Column)}
        projection_star = any(
            isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
            for select in tree.find_all(exp.Select)
            for e in select.expressions
        )
        found = set()
        for table in referenced:
            for col in self.pii.get(table, set()):
                if col in names or projection_star:
                    found.add((table, col))
        return sorted(found)


def _fail(result: ValidationResult, code: str, message: str) -> ValidationResult:
    result.violations.append(Violation(code, message))
    return result


def _function_name(func: exp.Func) -> str:
    if isinstance(func, exp.Anonymous):
        return str(func.name).lower()
    return func.sql_name().lower()


def _first_line(err: Exception) -> str:
    text = str(err).strip()
    return text.splitlines()[0] if text else type(err).__name__
