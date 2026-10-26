"""The six agent tools, as OpenAI function-calling schemas.

The descriptions are part of the prompt: they tell the model when to use each tool.
"""

from __future__ import annotations

SEARCH_CATALOG = {
    "type": "function",
    "function": {
        "name": "search_catalog",
        "description": (
            "Search the warehouse catalog (dbt model docs, columns, certified metrics) with "
            "hybrid keyword + semantic search. Use it to find which model and column hold a "
            "concept before writing SQL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you are looking for, in words."},
                "k": {"type": "integer", "description": "Number of results (1-12).", "default": 6},
            },
            "required": ["query"],
        },
    },
}

DESCRIBE_MODEL = {
    "type": "function",
    "function": {
        "name": "describe_model",
        "description": (
            "Full documentation of one model: grain, every column with type, meaning and data "
            "tests, and PII handling. Call it before querying a model you have not described."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Model name, e.g. fct_order_lines."}
            },
            "required": ["name"],
        },
    },
}

GET_METRIC = {
    "type": "function",
    "function": {
        "name": "get_metric",
        "description": (
            "Certified definition and SQL of a business metric (e.g. net_revenue, orders, aov, "
            "active_customers) or business term (e.g. 'revenue', 'cancellation rate'). Always use "
            "the certified expression, FROM and WHERE when computing a metric."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Metric name or business term."}
            },
            "required": ["name"],
        },
    },
}

RUN_SQL = {
    "type": "function",
    "function": {
        "name": "run_sql",
        "description": (
            "Run ONE read-only DuckDB SELECT on the warehouse and get the rows back with a "
            "query id (q1, q2, ...) to cite in your answer. Use bare model names (no schema). "
            "Aggregate in SQL; at most 200 rows are returned. Expensive queries may need human "
            "approval. Customer ids are tokens (cust_...)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A single DuckDB SELECT statement."},
                "purpose": {"type": "string", "description": "One line: what this query answers."},
                "reveal_pii": {
                    "type": "boolean",
                    "description": "Request clear customer identifiers (finance role only, needs human approval).",
                    "default": False,
                },
            },
            "required": ["sql"],
        },
    },
}

MAKE_CHART = {
    "type": "function",
    "function": {
        "name": "make_chart",
        "description": (
            "Build a Vega-Lite chart from the rows of an earlier run_sql result. The data is "
            "embedded from that result; no external data is allowed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query_id": {"type": "string", "description": "Id of a run_sql result, e.g. q2."},
                "mark": {"type": "string", "enum": ["bar", "line", "point", "area"]},
                "x": {"type": "string", "description": "Column for the x axis."},
                "y": {"type": "string", "description": "Column for the y axis."},
                "color": {"type": "string", "description": "Optional column for colour."},
                "title": {"type": "string"},
            },
            "required": ["query_id", "mark", "x", "y"],
        },
    },
}

ASK_USER = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "Ask the user a clarifying question when the request is ambiguous (unclear period, "
            "metric or ranking criterion). Offer 2-4 short options. Do this before querying."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["question"],
        },
    },
}

ALL_TOOLS = {
    "search_catalog": SEARCH_CATALOG,
    "describe_model": DESCRIBE_MODEL,
    "get_metric": GET_METRIC,
    "run_sql": RUN_SQL,
    "make_chart": MAKE_CHART,
    "ask_user": ASK_USER,
}


def tool_schemas(names: list[str]) -> list[dict]:
    unknown = set(names) - set(ALL_TOOLS)
    if unknown:
        raise ValueError(f"unknown tools: {sorted(unknown)}")
    return [ALL_TOOLS[n] for n in names]
