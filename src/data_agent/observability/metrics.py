"""Prometheus metrics (exposed by the API at /metrics). Label sets are small and bounded."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

_LATENCY_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300)
_FAST_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)

HTTP_REQUESTS = Counter(
    "data_agent_http_requests_total", "HTTP requests", ["endpoint", "method", "status"]
)
HTTP_LATENCY = Histogram(
    "data_agent_http_request_seconds", "HTTP request latency", ["endpoint"], buckets=_FAST_BUCKETS
)
QUESTIONS = Counter("data_agent_questions_total", "Questions by final status", ["status", "role"])
QUESTION_LATENCY = Histogram(
    "data_agent_question_seconds", "End-to-end latency per question turn", buckets=_LATENCY_BUCKETS
)
INFLIGHT = Gauge("data_agent_inflight_questions", "Questions being processed")
LLM_CALLS = Counter("data_agent_llm_calls_total", "LLM calls", ["purpose", "outcome"])
LLM_TOKENS = Counter("data_agent_llm_tokens_total", "LLM tokens", ["kind"])
LLM_LATENCY = Histogram(
    "data_agent_llm_seconds", "LLM call latency", ["purpose"], buckets=_LATENCY_BUCKETS
)
TOOL_CALLS = Counter("data_agent_tool_calls_total", "Tool calls", ["tool", "outcome"])
SQL_REJECTIONS = Counter("data_agent_sql_rejections_total", "Rejected SQL", ["reason"])
SQL_LATENCY = Histogram("data_agent_sql_seconds", "SQL execution latency", buckets=_FAST_BUCKETS)
INTERRUPTS = Counter("data_agent_interrupts_total", "Human-in-the-loop interrupts", ["kind"])
BUDGET_EXCEEDED = Counter("data_agent_budget_exceeded_total", "Budget stops", ["budget"])
UNTRACED_NUMBERS = Counter(
    "data_agent_untraced_numbers_total", "Numbers in answers with no source query"
)
PII_REDACTIONS = Counter("data_agent_pii_redactions_total", "Identifiers redacted on egress")
RATE_LIMITED = Counter("data_agent_rate_limited_total", "Requests refused by the rate limiter")
