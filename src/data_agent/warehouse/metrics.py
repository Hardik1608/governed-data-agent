"""Certified metric definitions and the business-term glossary (configs/metrics.yaml)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    description: str
    expression: str
    from_: str
    where: str
    time_column: str
    example_sql: str
    monthly_source: str | None = None
    caveats: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        out = {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "expression": self.expression,
            "from": self.from_,
            "where": self.where,
            "time_column": self.time_column,
            "example_sql": self.example_sql.strip(),
        }
        if self.monthly_source:
            out["monthly_source"] = self.monthly_source
        if self.caveats:
            out["caveats"] = list(self.caveats)
        return out

    def search_text(self) -> str:
        return " ".join([self.name.replace("_", " "), self.label, self.description, *self.caveats])


@dataclass(frozen=True)
class Resolution:
    """Result of resolving a metric name or business term."""

    status: str  # "ok" | "competing" | "not_found"
    query: str
    metric: Metric | None = None
    alternatives: tuple[str, ...] = ()
    competing: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    via_term: str | None = None


@dataclass
class MetricsLayer:
    metrics: dict[str, Metric]
    terms: dict[str, dict]
    currency: str = "GBP"
    data_window: str = ""
    _norm_terms: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._norm_terms = {_norm(t): t for t in self.terms}

    @classmethod
    def load(cls, path: Path) -> MetricsLayer:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        metrics = {}
        for name, m in data["metrics"].items():
            metrics[name] = Metric(
                name=name,
                label=m["label"],
                description=m["description"],
                expression=m["expression"],
                from_=m["from"],
                where=str(m.get("where", "TRUE")),
                time_column=m["time_column"],
                example_sql=m["example_sql"],
                monthly_source=m.get("monthly_source"),
                caveats=tuple(m.get("caveats", [])),
            )
        terms = data.get("terms", {})
        for term, spec in terms.items():
            for ref in [
                spec.get("default"),
                *spec.get("alternatives", []),
                *spec.get("competing", []),
            ]:
                if ref is not None and ref not in metrics:
                    raise ValueError(f"term '{term}' references unknown metric '{ref}'")
        return cls(metrics, terms, data.get("currency", "GBP"), data.get("data_window", ""))

    def resolve(self, query: str) -> Resolution:
        key = _norm(query)
        if key.replace(" ", "_") in self.metrics:
            return Resolution("ok", query, self.metrics[key.replace(" ", "_")])
        term = self._norm_terms.get(key)
        if term is not None:
            spec = self.terms[term]
            if "competing" in spec:
                return Resolution(
                    "competing", query, competing=tuple(spec["competing"]), via_term=term
                )
            return Resolution(
                "ok",
                query,
                self.metrics[spec["default"]],
                alternatives=tuple(spec.get("alternatives", [])),
                via_term=term,
            )
        return Resolution("not_found", query, suggestions=tuple(self.suggest(query)))

    def suggest(self, query: str, k: int = 3) -> list[str]:
        words = set(_norm(query).split())
        scored = []
        for m in self.metrics.values():
            overlap = len(words & set(_norm(m.search_text()).split()))
            if overlap:
                scored.append((overlap, m.name))
        return [name for _, name in sorted(scored, key=lambda x: (-x[0], x[1]))[:k]]


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())
