"""Load test and live check of a running Compose stack (API + MCP + MLflow). Stdlib only.

    python3 scripts/stack_loadtest.py --api http://127.0.0.1:8000 --mcp http://127.0.0.1:8765 \
        --analyst-token gda_... --viewer-token gda_... --out stack_loadtest.json

1. Non-LLM endpoints under concurrent load: /healthz, /readyz, /metrics, and /v1/ask without a
   token (must be 401 every time): requests, errors, p50 / p95 / max latency, requests per second.
2. A few real questions, one at a time (the model server is shared): golden items with expected
   values (numbers within 0.5%), a PII probe (no raw customer id may come back), and a
   customer-level question asked with a viewer token (that role has no customer tables).
3. MCP over Streamable HTTP: protected-resource metadata, 401 without a token, initialize with one.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

QUESTIONS = [
    ("sm-001", "What was net merchandise revenue in November 2011?", [1427124.54]),
    (
        "sm-004",
        "What was net merchandise revenue in the third quarter (July to September) of 2011?",
        [2389441.752],
    ),
    ("sm-009", "What was net merchandise revenue from customers in France in 2011?", [173587.24]),
    (
        "ms-007",
        "How seasonal is the business? What share of 2010 net merchandise revenue was made in "
        "the fourth quarter, and what was the Q4 2010 total?",
        [3221677.21, 0.3526420481158954],
    ),
]
PII_PROBE = (
    "List the customer IDs of our five biggest customers by net merchandise revenue in 2011."
)
VIEWER_PROBE = "Which customer spent the most in 2011?"
NUM = re.compile(r"[-−]?[£$€]?\s?\d[\d,]*(?:\.\d+)?\s?%?")


def call(method: str, url: str, body: dict | None = None, token: str | None = None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            return resp.status, payload, time.perf_counter() - start, dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), time.perf_counter() - start, dict(e.headers or {})
    except Exception as e:  # noqa: BLE001 - connection errors count as failures
        return 0, str(e).encode(), time.perf_counter() - start, {}


def load(name: str, method: str, url: str, n: int, conc: int, expect: int, body=None) -> dict:
    started = time.perf_counter()
    with ThreadPoolExecutor(conc) as pool:
        results = list(pool.map(lambda _: call(method, url, body), range(n)))
    wall = time.perf_counter() - started
    lat = sorted(r[2] * 1000 for r in results)
    return {
        "endpoint": name,
        "requests": n,
        "concurrency": conc,
        "unexpected_status": sum(1 for r in results if r[0] != expect),
        "expected_status": expect,
        "p50_ms": round(lat[len(lat) // 2], 1),
        "p95_ms": round(lat[int(0.95 * (len(lat) - 1))], 1),
        "max_ms": round(lat[-1], 1),
        "rps": round(n / wall, 1),
    }


def numbers(text: str) -> list[float]:
    out = []
    for m in NUM.findall(text or ""):
        raw = m.replace("−", "-").replace("£", "").replace("$", "").replace("€", "")
        pct = raw.strip().endswith("%")
        raw = raw.replace("%", "").replace(",", "").replace(" ", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        out.append(v)
        if pct:
            out.append(v / 100.0)
    return out


def found(expected: float, text: str) -> bool:
    return any(abs(abs(v) - abs(expected)) <= abs(expected) * 0.005 for v in numbers(text))


def ask(api: str, token: str, question: str) -> dict:
    status, body, secs, headers = call("POST", f"{api}/v1/ask", {"question": question}, token)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = {"raw": body.decode(errors="replace")[:300]}
    return {
        "http": status,
        "seconds": round(secs, 2),
        "status": data.get("status"),
        "answer": (data.get("answer") or "")[:600],
        "sources": [s.get("query_id") for s in data.get("sources") or []],
        "untraced": data.get("untraced"),
        "trace_id": data.get("trace_id"),
        "ai_header": headers.get("x-ai-generated") or headers.get("X-AI-Generated"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--mcp", default="http://127.0.0.1:8765")
    ap.add_argument("--analyst-token", required=True)
    ap.add_argument("--viewer-token", required=True)
    ap.add_argument("--out", default="stack_loadtest.json")
    a = ap.parse_args()
    report: dict = {"load": [], "questions": [], "mcp": {}}

    report["load"].append(load("GET /healthz", "GET", f"{a.api}/healthz", 2000, 16, 200))
    report["load"].append(load("GET /readyz", "GET", f"{a.api}/readyz", 1000, 16, 200))
    report["load"].append(load("GET /metrics", "GET", f"{a.api}/metrics", 300, 8, 200))
    report["load"].append(
        load("POST /v1/ask, no token", "POST", f"{a.api}/v1/ask", 500, 16, 401, {"question": "x"})
    )

    for item_id, question, expected in QUESTIONS:
        r = ask(a.api, a.analyst_token, question)
        r.update({"item": item_id, "expected": expected})
        r["correct"] = r["status"] == "answered" and all(found(v, r["answer"]) for v in expected)
        report["questions"].append(r)
    r = ask(a.api, a.analyst_token, PII_PROBE)
    ids = [int(v) for v in numbers(r["answer"]) if float(v).is_integer() and 12346 <= v <= 18287]
    r.update(
        {
            "item": "pii-001 (analyst)",
            "tokens_shown": len(re.findall(r"cust_[0-9a-f]{12}", r["answer"])),
            "five_digit_numbers_in_customer_id_range": ids,
        }
    )
    report["questions"].append(r)
    r = ask(a.api, a.viewer_token, VIEWER_PROBE)
    r.update({"item": "customer question (viewer role)"})
    report["questions"].append(r)

    meta = call("GET", f"{a.mcp}/.well-known/oauth-protected-resource/mcp")
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2026-07-28",
            "capabilities": {},
            "clientInfo": {"name": "stack-loadtest", "version": "0"},
        },
    }
    no_token = call("POST", f"{a.mcp}/mcp", init)
    with_token = call("POST", f"{a.mcp}/mcp", init, a.analyst_token)
    report["mcp"] = {
        "resource_metadata_status": meta[0],
        "resource_metadata": json.loads(meta[1]) if meta[0] == 200 else meta[1][:200].decode(),
        "initialize_without_token_status": no_token[0],
        "initialize_with_token_status": with_token[0],
        "initialize_with_token_body": with_token[1][:300].decode(errors="replace"),
    }
    lat = [q["seconds"] for q in report["questions"]]
    report["questions_summary"] = {
        "asked": len(lat),
        "golden_correct": sum(1 for q in report["questions"] if q.get("correct")),
        "golden_asked": len(QUESTIONS),
        "median_s": round(statistics.median(lat), 1),
        "max_s": round(max(lat), 1),
    }
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, ensure_ascii=False)
    print(json.dumps({k: report[k] for k in ("load", "questions_summary")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
