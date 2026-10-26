"""Validation of the golden set (the most important asset).

Without data (CI): schema, category counts, every answerable item has two SQL formulations
and an answer spec, the gold SQL passes the analyst's safety validator against the committed
catalog snapshot and binds in DuckDB (EXPLAIN on the schema-identical mini warehouse), and
expected.json covers every item.
With the real warehouse (`warehouse` marker): gold and alternative SQL are re-run and must
agree with each other and with expected.json; ambiguous interpretations must differ.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import pytest

from data_agent.eval.golden import (
    CATEGORY_COUNTS,
    INJECTION_REPLICA,
    agree,
    freeze,
    load_expected,
    load_golden,
    load_injections,
)
from data_agent.safety.executor import SafeDuckDBExecutor
from data_agent.safety.validator import SQLValidator
from data_agent.warehouse.replica import replica_file

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "golden" / "v1"
REAL_REPLICAS = ROOT / "data" / "replicas"


@pytest.fixture(scope="module")
def items():
    return load_golden(GOLDEN)


@pytest.fixture(scope="module")
def analyst_validator(snapshot_catalog, policy):
    models = policy.role("analyst").models
    schema = {t: c for t, c in snapshot_catalog.sqlglot_schema().items() if t in models}
    pii: dict[str, set[str]] = {}
    for col in policy.pii_columns:
        pii.setdefault(col.model, set()).add(col.column)
    return SQLValidator(schema, pii=pii)


def _all_sql(item):
    for which in ("gold_sql", "alt_sql"):
        if getattr(item, which):
            yield which, getattr(item, which)
    for i, interp in enumerate(item.interpretations):
        yield f"interpretation[{i}]", interp["sql"]


# ---------------------------------------------------------------- structure (CI)
def test_category_counts_match_the_spec(items):
    counts = {c: sum(1 for i in items if i.category == c) for c in CATEGORY_COUNTS}
    assert counts == CATEGORY_COUNTS
    assert sum(counts.values()) == 140


def test_ids_are_unique_and_prefixed_by_category(items):
    prefix = {
        "single_metric": "sm",
        "multi_step": "ms",
        "ambiguous": "amb",
        "unanswerable": "un",
        "pii": "pii",
        "injection": "inj",
    }
    for item in items:
        assert item.id.split("-")[0] == prefix[item.category], item.id


def test_answerable_items_have_two_formulations_and_a_spec(items):
    for item in items:
        if item.answerable:
            assert item.gold_sql and item.alt_sql, item.id
            assert item.values or item.entities, item.id
            assert item.gold_sql.strip() != item.alt_sql.strip(), item.id
        else:
            assert not item.gold_sql and not item.alt_sql, item.id
    for item in items:
        if item.category == "ambiguous":
            assert item.ambiguity and len(item.interpretations) >= 2, item.id
        if item.category == "unanswerable":
            assert item.missing, item.id
        if item.category == "pii":
            assert item.expected_behavior, item.id


def test_alternative_sql_is_independent(items):
    """The second formulation must differ in structure, not only in whitespace or aliases."""

    def shape(sql: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"\bAS \w+", "", sql, flags=re.I)).strip().lower()

    for item in items:
        if item.answerable:
            assert shape(item.gold_sql) != shape(item.alt_sql), item.id


def test_every_sql_passes_the_safety_validator(items, analyst_validator):
    for item in items:
        for which, sql in _all_sql(item):
            result = analyst_validator.validate(sql)
            assert result.ok, f"{item.id} {which}: {result.feedback()}"


def test_every_sql_binds_in_duckdb(items, mini_env):
    """EXPLAIN on the schema-identical mini warehouse: catches type and binder errors in CI."""
    ex = SafeDuckDBExecutor(replica_file(mini_env.replica_path, "masked"), timeout_s=30)
    try:
        for item in items:
            for _which, sql in _all_sql(item):
                ex.explain(sql)  # raises ExecutionError on a binder error
    finally:
        ex.close()


def test_expected_values_cover_every_answerable_item(items):
    expected = load_expected(GOLDEN / "expected.json")["items"]
    answerable = {i.id: i for i in items if i.answerable}
    assert set(expected) == set(answerable)
    for item_id, item in answerable.items():
        rec = expected[item_id]
        assert len(rec["values"]) == len(item.values), item_id
        assert len(rec["entities"]) == len(item.entities), item_id
        assert all(isinstance(v["value"], int | float) for v in rec["values"]), item_id


def test_injection_plants_match_items(items):
    plants = load_injections(GOLDEN)
    inj = [i for i in items if i.category == "injection"]
    assert len(plants) == 20 and len({i.injection["stock_code"] for i in inj}) == 20
    for item in inj:
        code = item.injection["stock_code"]
        assert code in plants, item.id
        assert item.detect, item.id
        for pattern in item.detect.get("sql_patterns", []) + item.detect.get("answer_patterns", []):
            re.compile(pattern)
    types = {i.injection["payload_type"] for i in inj}
    assert len(types) == 10  # ten attack types, two cases each


def test_canaries_do_not_appear_in_the_planted_text(items):
    plants = load_injections(GOLDEN)
    for item in items:
        if item.category != "injection":
            continue
        text = plants[item.injection["stock_code"]]
        for pattern in item.detect.get("answer_patterns", []):
            if item.injection["payload_type"] in ("instruction_override",):
                assert not re.search(pattern, text), f"{item.id}: canary {pattern} is in the data"


# ---------------------------------------------------------------- against the real warehouse
@pytest.fixture(scope="module")
def real_executors():
    if not (
        replica_file(REAL_REPLICAS, "masked").exists()
        and replica_file(REAL_REPLICAS, INJECTION_REPLICA).exists()
    ):
        pytest.skip(
            "real replicas not built (data-agent build-replicas --injections golden/v1/injection.yaml)"
        )
    exes = {
        r: SafeDuckDBExecutor(replica_file(REAL_REPLICAS, r), timeout_s=120, max_rows=1000)
        for r in ("masked", INJECTION_REPLICA)
    }
    yield exes
    for ex in exes.values():
        ex.close()


@pytest.mark.warehouse
@pytest.mark.slow
def test_gold_and_alt_agree_and_match_expected(items, real_executors, analyst_validator):
    records = freeze(
        items,
        lambda r, sql: real_executors[r].execute(sql, max_rows=1000),
        analyst_validator.validate,
    )
    expected = load_expected(GOLDEN / "expected.json")["items"]
    for item_id, rec in records.items():
        for got, want in zip(rec["values"], expected[item_id]["values"], strict=True):
            assert agree(got["value"], want["value"]), (item_id, got, want)
        for got, want in zip(rec["entities"], expected[item_id]["entities"], strict=True):
            assert got["any_of"] == want["any_of"], (item_id, got, want)


@pytest.mark.warehouse
def test_ambiguous_interpretations_differ_materially(items, real_executors):
    for item in items:
        if item.category != "ambiguous":
            continue
        answers = []
        for interp in item.interpretations:
            res = real_executors["masked"].execute(interp["sql"], max_rows=5)
            answers.append(res.rows[0][0])
        for a, b in itertools.combinations(answers, 2):
            if isinstance(a, int | float) and isinstance(b, int | float):
                assert abs(a - b) > 0.05 * max(abs(a), abs(b)), (item.id, answers)
            else:
                assert a != b, (item.id, answers)


@pytest.mark.warehouse
def test_plants_exist_only_in_the_test_copy(real_executors):
    plants = load_injections(GOLDEN)
    path = replica_file(REAL_REPLICAS, "masked")
    prod = dict(
        real_executors["masked"]
        .execute("SELECT stock_code, product_description FROM dim_product", max_rows=None)
        .rows
    )
    test_copy = dict(
        real_executors[INJECTION_REPLICA]
        .execute("SELECT stock_code, product_description FROM dim_product", max_rows=None)
        .rows
    )
    for code, text in plants.items():
        assert test_copy[code] == text
        assert prod[code] != text
    meta = json.loads(path.with_suffix(".json").read_text())
    assert meta["description_overrides"] == []
