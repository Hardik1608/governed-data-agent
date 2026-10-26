"""Catalog retrieval quality on the golden set: BM25 vs embeddings vs hybrid (RRF).

For every answerable golden question, the relevant models are the tables its gold SQL reads.
A question is a hit at k when every relevant model's document (or one of its columns) is in
the top-k search results. Embeddings: Ollama nomic-embed-text (needs Ollama locally).
Output: reports/retrieval/golden_recall.json and .md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_agent.config import Settings  # noqa: E402
from data_agent.eval.golden import load_golden  # noqa: E402
from data_agent.governance.policy import Policy  # noqa: E402
from data_agent.retrieval.index import CatalogIndex, build_documents  # noqa: E402
from data_agent.runtime import load_catalog, make_embedder  # noqa: E402
from data_agent.warehouse.metrics import MetricsLayer  # noqa: E402

KS = (3, 5, 8)


def relevant_models(sql: str) -> set[str]:
    tree = sqlglot.parse_one(sql, read="duckdb")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    return {t.name.lower() for t in tree.find_all(exp.Table) if t.name.lower() not in ctes}


def main() -> int:
    s = Settings()
    embedder = make_embedder(s)
    if embedder is None:
        print("Ollama embeddings unavailable: start Ollama with nomic-embed-text", file=sys.stderr)
        return 1
    catalog = load_catalog(s, Policy.load(s.policy_path))
    docs = build_documents(catalog, MetricsLayer.load(s.metrics_path))
    index = CatalogIndex.build(docs, embedder, s.index_path)
    items = [i for i in load_golden(s.home / "golden" / "v1") if i.answerable]
    results: dict = {}
    for mode in ("bm25", "embedding", "hybrid"):
        hits = {k: 0 for k in KS}
        for item in items:
            need = relevant_models(item.gold_sql)
            found = index.search(item.question, k=max(KS), mode=mode)
            for k in KS:
                covered = {m.lower() for h in found[:k] for m in h.doc.models}
                hits[k] += int(need <= covered)
        results[mode] = {f"recall@{k}": round(hits[k] / len(items), 4) for k in KS}
    report = {
        "questions": len(items),
        "documents": len(docs),
        "embedder": embedder.name,
        "results": results,
    }
    out = s.home / "reports" / "retrieval"
    out.mkdir(parents=True, exist_ok=True)
    (out / "golden_recall.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    lines = [
        "# Catalog retrieval on the golden set",
        "",
        f"{len(items)} answerable questions; {len(docs)} catalog documents (models, columns, certified metrics); "
        f"embeddings: {embedder.name}. A question counts as a hit at k when every model its gold SQL reads "
        "appears in the top-k results (as the model document or one of its columns).",
        "",
        "| Retriever | " + " | ".join(f"all models in top {k}" for k in KS) + " |",
        "|---|" + "---|" * len(KS),
    ]
    for mode, r in results.items():
        lines.append(
            f"| {mode} | " + " | ".join(f"{100 * r[f'recall@{k}']:.1f}%" for k in KS) + " |"
        )
    (out / "golden_recall.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
