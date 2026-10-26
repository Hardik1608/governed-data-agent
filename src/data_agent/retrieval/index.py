"""Hybrid catalog search: BM25 + embeddings, fused with reciprocal rank fusion (RRF).

Documents: one per model, one per column, one per certified metric. Lexical search catches
exact identifiers ("stock_code", "AOV"); embeddings catch paraphrases ("best clients",
"refunds"). RRF (Cormack et al., 2009) fuses the two rankings without score calibration:
    rrf(d) = sum over rankers of 1 / (k + rank_r(d)),  k = 60.

Store: a DuckDB file (`data/index/catalog_index.duckdb`) with one row per document and its
embedding (FLOAT[]). Embeddings are keyed by a content hash + model name, so a rebuild only
embeds documents that changed. Similarity is exact cosine over ~150 normalised vectors in
numpy; an HNSW index (DuckDB VSS) would only pay off at tens of thousands of documents.
Results are filtered to the models the caller's role may see.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import numpy as np
import sqlglot
from sqlglot import exp

from data_agent.retrieval.bm25 import BM25
from data_agent.retrieval.embeddings import Embedder
from data_agent.retrieval.text import expand_query, split_identifier, tokenize
from data_agent.warehouse.catalog import Catalog
from data_agent.warehouse.metrics import MetricsLayer

log = logging.getLogger(__name__)
RRF_K = 60


@dataclass
class CatalogDoc:
    doc_id: str
    kind: str  # model | column | metric
    name: str
    title: str
    text: str
    models: tuple[str, ...] = ()

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(f"{self.kind}|{self.name}|{self.text}".encode()).hexdigest()[:16]


@dataclass
class Hit:
    doc: CatalogDoc
    score: float
    bm25_rank: int | None = None
    embedding_rank: int | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.doc.kind,
            "name": self.doc.name,
            "title": self.doc.title,
            "text": self.doc.text,
            "score": round(self.score, 5),
        }


def _metric_tables(from_clause: str) -> tuple[str, ...]:
    try:
        tree = sqlglot.parse_one(f"SELECT 1 FROM {from_clause}", read="duckdb")  # noqa: S608 - parsed, never run
    except Exception:  # noqa: BLE001 - a malformed metric must not break search
        return ()
    return tuple(sorted({t.name for t in tree.find_all(exp.Table)}))


def build_documents(catalog: Catalog, metrics: MetricsLayer | None = None) -> list[CatalogDoc]:
    docs: list[CatalogDoc] = []
    for model in catalog.models.values():
        body = [model.description]
        if model.grain:
            body.append(f"Grain: {model.grain}.")
        body += model.notes
        body.append("Columns: " + ", ".join(c.name for c in model.columns))
        docs.append(
            CatalogDoc(
                f"model:{model.name}",
                "model",
                model.name,
                f"Model {model.name}",
                " ".join(body),
                (model.name,),
            )
        )
        for col in model.columns:
            text = f"{col.name} ({col.data_type}) in {model.name}. {col.description}"
            if col.tests:
                text += f" Tests: {', '.join(col.tests)}."
            if col.masking:
                text += f" PII: {col.masking}."
            docs.append(
                CatalogDoc(
                    f"column:{model.name}.{col.name}",
                    "column",
                    f"{model.name}.{col.name}",
                    f"Column {model.name}.{col.name}",
                    text,
                    (model.name,),
                )
            )
    if metrics is not None:
        synonyms: dict[str, list[str]] = {}
        for term, spec in metrics.terms.items():
            for ref in [spec.get("default"), *spec.get("competing", [])]:
                if ref:
                    synonyms.setdefault(ref, []).append(term)
        for m in metrics.metrics.values():
            words = synonyms.get(m.name, [])
            text = f"{m.label}. {m.description} Computed as {m.expression} over {m.from_}."
            if words:
                text += f" Business terms: {', '.join(words)}."
            if m.caveats:
                text += " Caveats: " + " ".join(m.caveats)
            docs.append(
                CatalogDoc(
                    f"metric:{m.name}",
                    "metric",
                    m.name,
                    f"Metric {m.name}",
                    text,
                    _metric_tables(m.from_),
                )
            )
    return docs


def _doc_tokens(doc: CatalogDoc) -> list[str]:
    return split_identifier(doc.name) + split_identifier(doc.name) + tokenize(doc.text)


@dataclass
class CatalogIndex:
    docs: list[CatalogDoc]
    embedder: Embedder | None = None
    embeddings: np.ndarray | None = None
    _bm25: BM25 = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._bm25 = BM25([_doc_tokens(d) for d in self.docs])

    @property
    def has_embeddings(self) -> bool:
        return self.embedder is not None and self.embeddings is not None

    # ------------------------------------------------------------------ build / store
    @classmethod
    def build(
        cls,
        docs: list[CatalogDoc],
        embedder: Embedder | None = None,
        store: Path | None = None,
    ) -> CatalogIndex:
        if embedder is None:
            return cls(docs)
        cached: dict[str, list[float]] = {}
        if store is not None and Path(store).exists():
            cached = _load_cache(Path(store), embedder.name)
        missing = [d for d in docs if d.content_hash not in cached]
        if missing:
            vectors = embedder.embed_documents([f"{d.title}. {d.text}" for d in missing])
            for doc, vec in zip(missing, vectors, strict=True):
                cached[doc.content_hash] = vec.tolist()
            log.info(
                "embedded %d/%d catalog documents with %s", len(missing), len(docs), embedder.name
            )
        mat = np.asarray([cached[d.content_hash] for d in docs], dtype=np.float32)
        if store is not None:
            _save(Path(store), docs, mat, embedder.name)
        return cls(docs, embedder, mat)

    # ------------------------------------------------------------------ search
    def search(
        self,
        query: str,
        k: int = 8,
        allowed_models: set[str] | None = None,
        mode: str = "hybrid",
        kinds: tuple[str, ...] | None = None,
    ) -> list[Hit]:
        allowed = {m.lower() for m in allowed_models} if allowed_models is not None else None
        keep = [
            i
            for i, d in enumerate(self.docs)
            if (allowed is None or all(m.lower() in allowed for m in d.models))
            and (kinds is None or d.kind in kinds)
        ]
        if not keep:
            return []
        rankings: list[list[int]] = []
        bm25_rank: dict[int, int] = {}
        emb_rank: dict[int, int] = {}
        if mode in ("hybrid", "bm25"):
            scores = self._bm25.get_scores(expand_query(tokenize(query)))
            order = sorted((i for i in keep if scores[i] > 0), key=lambda i: -scores[i])
            bm25_rank = {i: r for r, i in enumerate(order, start=1)}
            rankings.append(order)
        if mode in ("hybrid", "embedding") and self.has_embeddings:
            qv = self.embedder.embed_query(query)
            sims = self.embeddings[keep] @ qv
            order = [keep[j] for j in np.argsort(-sims)]
            emb_rank = {i: r for r, i in enumerate(order, start=1)}
            rankings.append(order)
        fused: dict[int, float] = {}
        for ranking in rankings:
            for rank, i in enumerate(ranking, start=1):
                fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank)
        best = sorted(fused, key=lambda i: (-fused[i], self.docs[i].doc_id))[:k]
        return [Hit(self.docs[i], fused[i], bm25_rank.get(i), emb_rank.get(i)) for i in best]


def _load_cache(store: Path, model: str) -> dict[str, list[float]]:
    try:
        con = duckdb.connect(str(store), read_only=True)
    except duckdb.Error:
        return {}
    try:
        rows = con.execute(
            "SELECT content_hash, embedding FROM catalog_docs WHERE embed_model = ?", [model]
        ).fetchall()
    except duckdb.Error:
        rows = []
    finally:
        con.close()
    return {h: list(v) for h, v in rows}


def _save(store: Path, docs: list[CatalogDoc], mat: np.ndarray, model: str) -> None:
    store.parent.mkdir(parents=True, exist_ok=True)
    tmp = store.with_name(store.stem + ".tmp.duckdb")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    try:
        con.execute(
            "CREATE TABLE catalog_docs (doc_id VARCHAR PRIMARY KEY, kind VARCHAR, name VARCHAR, "
            "title VARCHAR, text VARCHAR, models VARCHAR[], content_hash VARCHAR, "
            "embed_model VARCHAR, embedding FLOAT[])"
        )
        con.executemany(
            "INSERT INTO catalog_docs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    d.doc_id,
                    d.kind,
                    d.name,
                    d.title,
                    d.text,
                    list(d.models),
                    d.content_hash,
                    model,
                    v.tolist(),
                )
                for d, v in zip(docs, mat, strict=True)
            ],
        )
        con.execute("CHECKPOINT")
    finally:
        con.close()
    tmp.replace(store)
