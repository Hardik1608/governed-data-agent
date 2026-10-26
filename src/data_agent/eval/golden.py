"""The golden set: loading, schema validation, and freezing expected values.

Layout (golden/v1/):
  single_metric.yaml (60)  multi_step.yaml (30)  ambiguous.yaml (15)  unanswerable.yaml (10)
  pii.yaml (5)  injection.yaml (20 items + the planted descriptions)  expected.json (generated)
Every file is validated against golden/schema.json.

`freeze()` runs each answerable item's gold SQL and its independent alternative through the
analyst's validator (so the gold SQL itself passes the safety layer) on the analyst replica
(the injection items on the injection TEST copy), checks that both formulations agree on
every graded cell, and records the expected values together with the fingerprint of the
warehouse they were computed on.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import yaml

CATEGORY_COUNTS = {
    "single_metric": 60,
    "multi_step": 30,
    "ambiguous": 15,
    "unanswerable": 10,
    "pii": 5,
    "injection": 20,
}
ANSWERABLE = ("single_metric", "multi_step", "injection")
FILES = {c: f"{c}.yaml" for c in CATEGORY_COUNTS}
INJECTION_REPLICA = "masked_injection"


@dataclass
class ValueSpec:
    column: str
    row: int = 0
    label: str | None = None
    tolerance: float = 0.005


@dataclass
class EntitySpec:
    columns: list[str]
    row: int = 0
    aliases: list[str] = field(default_factory=list)


@dataclass
class GoldenItem:
    id: str
    category: str
    question: str
    tags: list[str] = field(default_factory=list)
    gold_sql: str | None = None
    alt_sql: str | None = None
    values: list[ValueSpec] = field(default_factory=list)
    entities: list[EntitySpec] = field(default_factory=list)
    order_matters: bool = False
    note: str = ""
    on_clarification: str = ""
    ambiguity: str = ""
    interpretations: list[dict] = field(default_factory=list)
    missing: str = ""
    expected_behavior: str = ""
    injection: dict = field(default_factory=dict)
    detect: dict = field(default_factory=dict)

    @property
    def answerable(self) -> bool:
        return self.category in ANSWERABLE

    @property
    def replica(self) -> str:
        return INJECTION_REPLICA if self.category == "injection" else "masked"


def _value_spec(v: Any) -> ValueSpec:
    if isinstance(v, str):
        return ValueSpec(v)
    return ValueSpec(v["column"], v.get("row", 0), v.get("label"), v.get("tolerance", 0.005))


def _entity_spec(e: Any) -> EntitySpec:
    if isinstance(e, str):
        return EntitySpec([e])
    cols = e.get("any_of") or [e["column"]]
    return EntitySpec(list(cols), e.get("row", 0), list(e.get("aliases", [])))


def load_schema(golden_root: Path) -> dict:
    return json.loads((Path(golden_root) / "schema.json").read_text(encoding="utf-8"))


def load_file(path: Path, schema: dict) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    jsonschema.validate(data, schema)
    return data


def load_golden(version_dir: Path, categories: tuple[str, ...] | None = None) -> list[GoldenItem]:
    version_dir = Path(version_dir)
    schema = load_schema(version_dir.parent)
    items: list[GoldenItem] = []
    for category, filename in FILES.items():
        if categories and category not in categories:
            continue
        path = version_dir / filename
        if not path.exists():
            continue
        data = load_file(path, schema)
        if data["category"] != category:
            raise ValueError(f"{path}: category is {data['category']}, expected {category}")
        for raw in data["items"]:
            answer = raw.get("answer") or {}
            items.append(
                GoldenItem(
                    id=raw["id"],
                    category=category,
                    question=raw["question"].strip(),
                    tags=list(raw.get("tags", [])),
                    gold_sql=raw.get("gold_sql"),
                    alt_sql=raw.get("alt_sql"),
                    values=[_value_spec(v) for v in answer.get("values", [])],
                    entities=[_entity_spec(e) for e in answer.get("entities", [])],
                    order_matters=bool(answer.get("order_matters", False)),
                    note=raw.get("note", ""),
                    on_clarification=raw.get("on_clarification", ""),
                    ambiguity=raw.get("ambiguity", ""),
                    interpretations=list(raw.get("interpretations", [])),
                    missing=raw.get("missing", ""),
                    expected_behavior=raw.get("expected_behavior", ""),
                    injection=dict(raw.get("injection", {})),
                    detect=dict(raw.get("detect", {})),
                )
            )
    ids = [i.id for i in items]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate golden ids: {sorted(dupes)}")
    return items


def load_injections(path: Path) -> dict[str, str]:
    """stock_code -> planted description, from injection.yaml (`plants`)."""
    path = Path(path)
    if path.is_dir():
        path = path / FILES["injection"]
    data = load_file(path, load_schema(path.parent.parent))
    return {p["stock_code"]: p["description"] for p in data.get("plants", [])}


# ----------------------------------------------------------------------------------
# Freezing expected values
# ----------------------------------------------------------------------------------


class GoldenError(AssertionError):
    pass


def _cell(result, column: str, row: int, item_id: str, which: str) -> Any:
    if column not in result.columns:
        raise GoldenError(
            f"{item_id}: {which} result has no column '{column}' (has {result.columns})"
        )
    if row >= len(result.rows):
        raise GoldenError(f"{item_id}: {which} result has {len(result.rows)} rows, needs row {row}")
    return result.rows[row][result.columns.index(column)]


_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ]00:00:00)?$")


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        m = _DATE.match(value.strip())
        return m.group(1) if m else value.strip()
    return value


def agree(a: Any, b: Any) -> bool:
    a, b = _norm(a), _norm(b)
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        if math.isclose(a, b, rel_tol=0, abs_tol=1e-9):
            return True
        diff = abs(a - b)
        return diff <= 0.1 and diff <= 1e-4 * max(abs(a), abs(b))
    return a == b


def freeze(items: list[GoldenItem], execute, validate) -> dict:
    """`execute(replica, sql) -> ExecutionResult`; `validate(sql) -> ValidationResult`."""
    out: dict[str, dict] = {}
    for item in items:
        if not item.answerable:
            continue
        if not item.gold_sql or not item.alt_sql:
            raise GoldenError(f"{item.id}: answerable items need gold_sql and alt_sql")
        for which, sql in (("gold_sql", item.gold_sql), ("alt_sql", item.alt_sql)):
            v = validate(sql)
            if not v.ok:
                raise GoldenError(f"{item.id}: {which} fails the safety validator: {v.feedback()}")
        gold = execute(item.replica, item.gold_sql)
        alt = execute(item.replica, item.alt_sql)
        record: dict[str, Any] = {"values": [], "entities": [], "gold_rows": gold.row_count}
        for spec in item.values:
            g = _cell(gold, spec.column, spec.row, item.id, "gold")
            a = _cell(alt, spec.column, spec.row, item.id, "alt")
            if g is None or not isinstance(g, int | float) or isinstance(g, bool):
                raise GoldenError(f"{item.id}: value {spec.column} is not a number: {g!r}")
            if not agree(g, a):
                raise GoldenError(
                    f"{item.id}: gold and alt disagree on {spec.column}: {g!r} vs {a!r}"
                )
            record["values"].append(
                {
                    "column": spec.column,
                    "row": spec.row,
                    "value": g,
                    "tolerance": spec.tolerance,
                    "label": spec.label or spec.column,
                }
            )
        for spec in item.entities:
            cands = []
            for col in spec.columns:
                g = _cell(gold, col, spec.row, item.id, "gold")
                a = _cell(alt, col, spec.row, item.id, "alt")
                if not agree(g, a):
                    raise GoldenError(f"{item.id}: gold and alt disagree on {col}: {g!r} vs {a!r}")
                cands.append(str(_norm(g)))
            record["entities"].append(
                {"columns": spec.columns, "row": spec.row, "any_of": cands + spec.aliases}
            )
        out[item.id] = record
    return out


def write_expected(path: Path, records: dict, fingerprints: dict) -> None:
    payload = {
        "golden_version": Path(path).parent.name,
        "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "replica_fingerprints": fingerprints,
        "items": dict(sorted(records.items())),
    }
    Path(path).write_text(json.dumps(payload, indent=1, default=str) + "\n", encoding="utf-8")


def load_expected(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
