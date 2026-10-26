"""Turn the artefacts of scripts/stack_check.sh into reports/stack/stack_check.{json,md}.

python scripts/stack_report.py reports/stack/<run dir> [reports/stack/<earlier attempt> ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(d: Path, name: str) -> str:
    p = d / name
    return p.read_text(encoding="utf-8", errors="replace").strip() if p.exists() else ""


def main(run_dir: str, *earlier: str) -> int:
    d = Path(run_dir).resolve()
    lt = json.loads(read(d, "loadtest.json") or "{}")
    report = {
        "run": d.name,
        "load": lt.get("load", []),
        "questions": lt.get("questions", []),
        "questions_summary": lt.get("questions_summary", {}),
        "mcp": lt.get("mcp", {}),
        "memory_idle": read(d, "stats_idle.txt").splitlines(),
        "memory_after": read(d, "stats_after.txt").splitlines(),
        "containers": read(d, "inspect.txt").splitlines() or read(d, "ps.txt").splitlines(),
        "audit_events": read(d, "audit_counts.txt"),
        "mlflow_traces": read(d, "mlflow_traces.txt"),
        "earlier_attempts": [Path(e).name for e in earlier],
    }
    out = ROOT / "reports" / "stack"
    (out / "stack_check.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    md = [
        "# Compose stack on real replicas: load test and live questions",
        "",
        "`scripts/stack_check.sh` on the shared GPU host (no GPU used by the stack): API + MCP + "
        "MLflow from `docker-compose.yml` with memory limits of 2 GiB in total "
        "(api 704m, mcp 576m, mlflow 768m), the real analyst, finance and viewer replicas mounted "
        "read-only, the shared Qwen3.5-9B server as the model, embeddings off (no embedding "
        "server on that host: catalog search is BM25 only). Torn down afterwards. "
        f"Artefacts: `{d.relative_to(ROOT).as_posix()}/`.",
        "",
        "## Non-LLM endpoints under load (from the host, Python client)",
        "",
        "| Endpoint | Requests | Concurrency | Unexpected status | p50 | p95 | max | req/s |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in report["load"]:
        md.append(
            f"| {r['endpoint']} (expect {r['expected_status']}) | {r['requests']} | {r['concurrency']} | "
            f"{r['unexpected_status']} | {r['p50_ms']} ms | {r['p95_ms']} ms | {r['max_ms']} ms | {r['rps']} |"
        )
    md += [
        "",
        "## Real questions (one at a time; the model server is shared)",
        "",
        "| Item | Role | HTTP | Status | Seconds | Correct | Answer (start) |",
        "|---|---|---|---|---|---|---|",
    ]
    for q in report["questions"]:
        role = "viewer" if "viewer" in q["item"] else "analyst"
        ok = q.get("correct")
        if "pii" in q["item"]:
            ok = f"tokens shown {q.get('tokens_shown')}, raw-id-like numbers {len(q.get('five_digit_numbers_in_customer_id_range') or [])}"
        ans = (q.get("answer") or "").replace("|", "/").replace("\n", " ")[:110]
        md.append(
            f"| {q['item']} | {role} | {q['http']} | {q['status']} | {q['seconds']} | {ok if ok is not None else '-'} | {ans} |"
        )
    s = report["questions_summary"]
    m = report["mcp"]
    md += [
        "",
        f"Golden items correct: {s.get('golden_correct')} of {s.get('golden_asked')}; median "
        f"{s.get('median_s')} s, max {s.get('max_s')} s per question.",
        "",
        "## MCP (Streamable HTTP), audit, tracing, memory",
        "",
        f"- Protected-resource metadata: HTTP {m.get('resource_metadata_status')}; initialize "
        f"without a token: HTTP {m.get('initialize_without_token_status')}; with a token: HTTP "
        f"{m.get('initialize_with_token_status')}.",
        f"- Audit log events: {report['audit_events'] or 'not read'}.",
        f"- MLflow traces: {report['mlflow_traces'] or 'none (tracking server unavailable)'}.",
        "- Containers: " + "; ".join(report["containers"]),
        "- Memory at idle: " + "; ".join(report["memory_idle"]),
        "- Memory after the test: " + "; ".join(report["memory_after"]),
    ]
    if earlier:
        md += [
            "",
            "Earlier attempts (artefacts kept): "
            + ", ".join(f"`{e}`" for e in report["earlier_attempts"])
            + ".",
        ]
    (out / "stack_check.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
