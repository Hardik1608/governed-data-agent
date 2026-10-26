"""Retrieval, metrics layer, catalog, traceability, LLM client, audit log, PII redactor."""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from data_agent.agent.context import estimate_tokens, fit_context, shrink_payload
from data_agent.agent.graph import classify_final, parse_plan
from data_agent.agent.traceability import check_answer, extract_numbers, number_matches
from data_agent.config import AgentConfig, Settings
from data_agent.governance.audit import AuditLog
from data_agent.governance.pii import PiiRedactor, _fp
from data_agent.governance.policy import Policy, PolicyError
from data_agent.llm.client import LLMError, OpenAICompatibleClient, parse_tool_calls
from data_agent.retrieval.bm25 import BM25
from data_agent.retrieval.embeddings import HashingEmbedder
from data_agent.retrieval.index import CatalogIndex, build_documents
from data_agent.warehouse.metrics import MetricsLayer

S = Settings()


@pytest.fixture(scope="module")
def metrics():
    return MetricsLayer.load(S.metrics_path)


@pytest.fixture(scope="module")
def index(snapshot_catalog, metrics):
    return CatalogIndex.build(build_documents(snapshot_catalog, metrics), HashingEmbedder())


# ---------------------------------------------------------------- retrieval
def test_bm25_idf_is_positive_on_tiny_corpora():
    bm = BM25([["a", "b"], ["a", "c"]])
    assert bm.get_scores(["a"]).min() > 0


@pytest.mark.parametrize(
    "query, expected",
    [
        ("loyalty tier of the customer", "dim_customer"),
        ("stock code of a product", "dim_product"),
        ("monthly net merchandise revenue", "mart_revenue_monthly"),
        ("cohort retention rate", "mart_cohort_retention"),
        ("average order value", "aov"),
    ],
)
def test_hybrid_search_finds_the_right_object(index, query, expected):
    names = [h.doc.name for h in index.search(query, k=5)]
    assert any(expected in n for n in names), names


def test_search_respects_role_allowlist(index, policy):
    viewer = set(policy.role("viewer").models)
    hits = index.search("customer loyalty tier", k=10, allowed_models=viewer)
    assert hits and all(set(h.doc.models) <= viewer for h in hits)


def test_bm25_only_mode_without_embeddings(snapshot_catalog, metrics):
    idx = CatalogIndex.build(build_documents(snapshot_catalog, metrics), None)
    assert not idx.has_embeddings
    assert idx.search("invoice timestamp", k=3, mode="hybrid")


def test_embedding_cache_is_reused(tmp_path, snapshot_catalog, metrics):
    class Counting(HashingEmbedder):
        calls = 0

        def embed_documents(self, texts):
            Counting.calls += len(texts)
            return super().embed_documents(texts)

    docs = build_documents(snapshot_catalog, metrics)
    store = tmp_path / "idx.duckdb"
    CatalogIndex.build(docs, Counting(), store)
    first = Counting.calls
    CatalogIndex.build(docs, Counting(), store)
    assert first == len(docs) and Counting.calls == first  # nothing re-embedded


# ---------------------------------------------------------------- metrics layer / policy / catalog
def test_metric_resolution(metrics):
    assert metrics.resolve("net_revenue").metric.name == "net_revenue"
    r = metrics.resolve("Revenue")
    assert r.status == "ok" and r.metric.name == "net_revenue" and "gross_revenue" in r.alternatives
    c = metrics.resolve("cancellation rate")
    assert c.status == "competing" and set(c.competing) == {
        "cancellation_rate_value",
        "cancellation_rate_orders",
    }
    assert metrics.resolve("ebitda").status == "not_found"


def test_policy_rejects_clear_pii_as_default(tmp_path):
    data = Policy.load(S.policy_path).raw
    bad = json.loads(json.dumps(data))
    bad["roles"]["analyst"]["replica"] = "clear"
    with pytest.raises(PolicyError):
        Policy.from_dict(bad)


def test_catalog_snapshot_marks_pii(snapshot_catalog):
    col = snapshot_catalog.get("dim_customer").column("customer_id")
    assert col.pii and "tokenised" in col.masking
    assert "re-keyed" in snapshot_catalog.get("fct_order_lines").column("order_line_id").masking
    assert (
        snapshot_catalog.get("mart_revenue_monthly").column("net_merchandise_revenue").description
    )


def test_agent_config_fingerprint_changes_with_the_prompt():
    cfg = AgentConfig.load(S.agent_config_path)
    other = AgentConfig.model_validate({**cfg.model_dump(), "prompt": "plan_v1.md"})
    assert cfg.fingerprint != other.fingerprint


# ---------------------------------------------------------------- traceability
def test_extract_numbers_skips_dates_years_and_citations():
    text = "In Nov 2011 (2011-11-01 to 30 November) revenue was £1.43M [q1, q2]; top 5 products."
    assert [m.text for m in extract_numbers(text)] == ["£1.43M"]


def test_check_answer_traces_each_number():
    queries = {"q1": {"columns": ["rev", "share"], "rows": [[1427124.54, 0.3442]]}}
    ok = check_answer("Revenue £1,427,124.54 (34.4% of the year) [q1].", queries)
    assert ok.ok and {p.column for p in ok.traced} == {"rev", "share"}
    bad = check_answer("Revenue £1,500,000.", queries)
    assert not bad.ok and bad.untraced == ["£1,500,000"]


def test_grader_tolerance_is_half_a_percent():
    assert number_matches(1427124.54, "£1.427M")
    assert not number_matches(1427124.54, "£1.4M")
    assert number_matches(-174091.36, "cancellations of £174,091")
    assert number_matches(0.2017, "20.2%")
    # a percentage shown with >= 1 decimal matches a ratio that rounds to it
    assert number_matches(0.0526, "a retention rate of 5.3%")
    assert number_matches(-0.003778, "a share of -0.38%")
    assert not number_matches(0.0526, "5.2%")  # 5.26% does not round to 5.2%
    assert not number_matches(0.0526, "5%")  # no decimal shown: 0.5% relative still applies


def test_classify_final_and_parse_plan():
    assert classify_final("CANNOT ANSWER: no cost data.") == "refused"
    assert classify_final("Could you clarify which period you mean?") == "needs_clarification"
    assert classify_final("Revenue was £5.") == "answered"
    assert parse_plan('```json\n{"action": "refuse", "reason": "x"}\n```')["action"] == "refuse"
    assert parse_plan("not json")["action"] == "answer"


# ---------------------------------------------------------------- context control
def test_shrink_payload_and_fit_context():
    payload = {"query_id": "q1", "columns": ["a"], "rows": [[i] for i in range(500)], "note": "n"}
    small = shrink_payload(payload, max_rows=40)
    assert len(small["rows"]) == 40 and "Showing 40 of 500" in small["note"]
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    msgs += [
        {
            "role": "tool",
            "content": json.dumps(
                {"query_id": f"q{i}", "columns": ["x"], "row_count": 1, "rows": [["x" * 3000]]}
            ),
        }
        for i in range(6)
    ]
    fitted = fit_context(msgs, max_tokens=3_000)
    assert estimate_tokens(fitted) <= 3_000 and "elided" in fitted[2]["content"]
    assert fitted[-1] == msgs[-1]  # the latest tool result is kept


# ---------------------------------------------------------------- LLM client
def _client(handler, **kw):
    return OpenAICompatibleClient(
        "http://llm.test/v1", "m", transport=httpx.MockTransport(handler), backoff_base_s=0.01, **kw
    )


def test_client_parses_tool_calls_and_usage():
    def handler(request):
        body = json.loads(request.content)
        assert body["tools"][0]["function"]["name"] == "run_sql" and body["top_k"] == 20
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "run_sql",
                                        "arguments": '{"sql": "SELECT 1"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )

    resp = _client(handler, sampling={"top_k": 20}).chat(
        [{"role": "user", "content": "x"}],
        tools=[{"type": "function", "function": {"name": "run_sql"}}],
    )
    assert resp.tool_calls[0].arguments == {"sql": "SELECT 1"}
    assert (resp.prompt_tokens, resp.completion_tokens) == (11, 7)


def test_client_retries_503_then_succeeds_and_never_retries_400():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="restarting")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "pong"}}], "usage": {}}
        )

    assert _client(flaky).chat([{"role": "user", "content": "ping"}]).content == "pong"
    assert calls["n"] == 3

    def bad(request):
        calls["n"] += 1
        return httpx.Response(400, text="context too long")

    calls["n"] = 0
    with pytest.raises(LLMError) as e:
        _client(bad).chat([{"role": "user", "content": "x"}])
    assert e.value.kind == "http_error" and calls["n"] == 1


def test_client_concurrency_cap():
    active, peak, lock = [0], [0], threading.Lock()

    def slow(request):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        import time

        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatibleClient(
        "http://cap.test/v1", "m", transport=httpx.MockTransport(slow), max_concurrency=2
    )
    threads = [
        threading.Thread(target=client.chat, args=([{"role": "user", "content": "x"}],))
        for _ in range(6)
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak[0] <= 2


def test_tool_calls_printed_in_content_are_recovered():
    content, calls = parse_tool_calls(
        {
            "content": 'ok <tool_call>{"name": "get_metric", "arguments": {"name": "aov"}}</tool_call>'
        }
    )
    assert (
        calls[0].name == "get_metric" and calls[0].arguments == {"name": "aov"} and content == "ok"
    )


# ---------------------------------------------------------------- audit + PII
def test_audit_log_is_idempotent(tmp_path):
    log = AuditLog(tmp_path / "a.sqlite")
    assert log.record("t:1:sql:c1", "sql", thread_id="t", sql="SELECT 1", status="executed")
    assert not log.record("t:1:sql:c1", "sql", thread_id="t", sql="SELECT 1", status="executed")
    assert len(log.events("t")) == 1
    with pytest.raises(ValueError):
        log.record("x", "bogus")


def test_redactor_only_touches_real_identifiers():
    r = PiiRedactor({_fp("s", "12346")}, "s")
    out = r.redact("Customer 12346 spent £12,346.00 on 12346 items; 12347 too.", allow="")
    assert out.text.startswith("Customer [REDACTED-ID]") and "£12,346.00" in out.text
    assert "12347" in out.text  # not a known identifier
    assert r.redact("Customer 12346", allow="about customer 12346?").text == "Customer 12346"


# ---------------------------------------------------------------- source hygiene
def test_text_file_io_names_its_encoding():
    """Reports contain '£'; Windows defaults to cp1252, so every text read/write says utf-8."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    offenders = []
    for path in sorted([*(root / "src").rglob("*.py"), *(root / "scripts").rglob("*.py")]):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            keywords = {k.arg for k in node.keywords}
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
                if "encoding" not in keywords:
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
            elif isinstance(func, ast.Attribute | ast.Name) and (
                getattr(func, "attr", None) == "open" or getattr(func, "id", None) == "open"
            ):
                pos = node.args[0:1] if isinstance(func, ast.Attribute) else node.args[1:2]
                mode = next(
                    (a.value for a in pos if isinstance(a, ast.Constant)),
                    next(
                        (
                            k.value.value
                            for k in node.keywords
                            if k.arg == "mode" and isinstance(k.value, ast.Constant)
                        ),
                        "r",
                    ),
                )
                if isinstance(mode, str) and "b" not in mode and "encoding" not in keywords:
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, offenders
