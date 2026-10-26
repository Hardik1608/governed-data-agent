"""Keep every model request small.

The shared model server has a KV cache of ~80k tokens for all clients, so each request
should stay under ~12k tokens. Two mechanisms:
  - `shrink_payload`: a tool result shown to the model carries at most `max_rows` rows and
    `max_chars` characters (the full, capped result stays in the graph state, where the
    traceability check reads it);
  - `fit_context`: before each call, the oldest tool results are replaced by one-line stubs
    until the estimated size fits. The system prompt, the question and the latest tool
    results are never elided. The state keeps the full history; only the request is trimmed.
Token counts are estimated as characters / 3 (JSON with many digits tokenises densely), so
the estimate errs on the large side.
"""

from __future__ import annotations

import json
from typing import Any

CHARS_PER_TOKEN = 3.0


def estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for msg in messages:
        total += len(str(msg.get("content") or "")) + 16
        for call in msg.get("tool_calls") or []:
            total += len(json.dumps(call))
    return int(total / CHARS_PER_TOKEN)


def shrink_payload(
    payload: dict[str, Any], max_rows: int = 40, max_chars: int = 9_000, max_cell: int = 300
) -> dict:
    rows = payload.get("rows")
    if not isinstance(rows, list):
        text = json.dumps(payload, default=str)
        if len(text) <= max_chars:
            return payload
        return {"truncated_output": text[:max_chars], "note": "output truncated to save context"}
    out = dict(payload)

    def cell(v: Any) -> Any:
        return v[:max_cell] + "..." if isinstance(v, str) and len(v) > max_cell else v

    shown = [[cell(v) for v in row] for row in rows[:max_rows]]
    while shown and len(json.dumps({**out, "rows": shown}, default=str)) > max_chars:
        shown = shown[: max(1, len(shown) // 2)] if len(shown) > 1 else []
    if len(shown) < len(rows):
        out["rows_shown"] = len(shown)
        out["note"] = (
            f"{out.get('note', '')} Showing {len(shown)} of {len(rows)} rows to save context; "
            "aggregate in SQL or add LIMIT for the rest."
        ).strip()
    out["rows"] = shown
    return out


def fit_context(messages: list[dict], max_tokens: int, keep_last_tools: int = 2) -> list[dict]:
    if estimate_tokens(messages) <= max_tokens:
        return messages
    trimmed = [dict(msg) for msg in messages]
    tool_idx = [i for i, msg in enumerate(trimmed) if msg.get("role") == "tool"]
    for i in tool_idx[:-keep_last_tools] if keep_last_tools else tool_idx:
        content = trimmed[i].get("content") or ""
        stub = "[earlier tool result elided to save context]"
        try:
            data = json.loads(content)
            if isinstance(data, dict) and "query_id" in data:
                stub = (
                    f"[result of {data['query_id']} elided to save context: columns "
                    f"{data.get('columns')}, {data.get('row_count')} rows]"
                )
        except (json.JSONDecodeError, TypeError):
            pass
        trimmed[i]["content"] = stub
        if estimate_tokens(trimmed) <= max_tokens:
            break
    return trimmed
