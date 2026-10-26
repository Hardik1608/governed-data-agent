"""The data catalog the agent reasons over: dbt docs + types + tests + governance tags.

Sources, merged in this order:
  1. dbt `manifest.json` (model and column descriptions, tests, contracts, compiled SQL);
  2. dbt `catalog.json` (column types and order as built in the warehouse);
  3. `configs/catalog_overlay.yaml` (definitions the dbt YAML leaves undocumented, taken from
     the model SQL comments, so the agent does not have to read SQL to learn what a column
     means);
  4. the governance policy (which models exist for the agent, which columns are PII).

`CatalogSnapshot` serialises the merged result to `golden/catalog_snapshot.json`, which is
committed: CI validates the golden set's gold SQL against it without the warehouse.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from data_agent.governance.policy import Policy

PROJECT = "sales_analytics"


@dataclass
class ColumnDoc:
    name: str
    data_type: str
    description: str = ""
    tests: list[str] = field(default_factory=list)
    pii: bool = False
    masking: str = ""  # e.g. "tokenised: cust_<hash>" for the masked replica

    def line(self) -> str:
        bits = [f"{self.name} {self.data_type}"]
        if self.description:
            bits.append(f"- {self.description}")
        if self.tests:
            bits.append(f"[tests: {', '.join(self.tests)}]")
        if self.masking:
            bits.append(f"[PII: {self.masking}]")
        return " ".join(bits)


@dataclass
class ModelDoc:
    name: str
    description: str
    columns: list[ColumnDoc]
    materialization: str = ""
    tags: list[str] = field(default_factory=list)
    grain: str = ""
    depends_on: list[str] = field(default_factory=list)
    model_tests: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    row_count: int | None = None

    def column(self, name: str) -> ColumnDoc | None:
        lowered = name.lower()
        return next((c for c in self.columns if c.name.lower() == lowered), None)

    def summary(self) -> str:
        first = self.description.split(". ")[0].rstrip(".")
        return f"{self.name}: {first}."

    def to_text(self, max_columns: int | None = None) -> str:
        cols = self.columns if max_columns is None else self.columns[:max_columns]
        lines = [f"Model {self.name} ({self.materialization or 'table'})", self.description]
        if self.grain:
            lines.append(f"Grain: {self.grain}")
        if self.row_count is not None:
            lines.append(f"Rows: {self.row_count:,}")
        lines.append("Columns:")
        lines += [f"  - {c.line()}" for c in cols]
        if self.notes:
            lines.append("Notes:")
            lines += [f"  - {n}" for n in self.notes]
        if self.model_tests:
            lines.append("Model-level tests: " + "; ".join(self.model_tests))
        return "\n".join(lines)


@dataclass
class Catalog:
    models: dict[str, ModelDoc]
    source: str = ""

    def __contains__(self, name: str) -> bool:
        return name.lower() in self.models

    def get(self, name: str) -> ModelDoc | None:
        return self.models.get(name.lower())

    def for_models(self, names: tuple[str, ...] | list[str]) -> Catalog:
        keep = {n.lower() for n in names}
        return Catalog({k: v for k, v in self.models.items() if k in keep}, self.source)

    def sqlglot_schema(self) -> dict[str, dict[str, str]]:
        return {m.name: {c.name: c.data_type for c in m.columns} for m in self.models.values()}

    def overview(self) -> str:
        """One line per model: grain, purpose and business columns (lineage columns omitted)."""
        lines = []
        for m in self.models.values():
            cols = ", ".join(c.name for c in m.columns if not c.name.startswith("_"))
            grain = f" [{m.grain}]" if m.grain else ""
            lines.append(
                f"- {m.name}{grain}: {m.description.split('. ')[0].rstrip('.')}. Columns: {cols}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ snapshot
    def to_json(self) -> dict:
        return {
            "source": self.source,
            "models": {k: asdict(v) for k, v in sorted(self.models.items())},
        }

    @classmethod
    def from_json(cls, data: dict) -> Catalog:
        models = {}
        for name, m in data["models"].items():
            cols = [ColumnDoc(**c) for c in m.pop("columns")]
            models[name] = ModelDoc(columns=cols, **m)
        return cls(models, data.get("source", ""))

    def save(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(self.to_json(), indent=1, sort_keys=False) + "\n", encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> Catalog:
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))


# ----------------------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------------------


def _test_label(test: dict) -> str | None:
    meta = test.get("test_metadata") or {}
    name = meta.get("name")
    kwargs = meta.get("kwargs") or {}
    if not name:
        return None
    if name == "accepted_values":
        values = kwargs.get("values") or (kwargs.get("arguments") or {}).get("values") or []
        return f"accepted_values {list(values)}"
    if name == "relationships":
        args = kwargs.get("arguments") or kwargs
        to = str(args.get("to", "")).replace("ref('", "").replace("')", "")
        return f"relationship -> {to}.{args.get('field', '')}"
    if name == "accepted_range":
        args = kwargs.get("arguments") or kwargs
        return f"range [{args.get('min_value')}, {args.get('max_value')}]"
    if name == "unique_combination_of_columns":
        args = kwargs.get("arguments") or kwargs
        return f"unique ({', '.join(args.get('combination_of_columns', []))})"
    return name


def build_catalog(
    dbt_dir: Path,
    policy: Policy,
    overlay_path: Path | None = None,
    row_counts: dict[str, int] | None = None,
) -> Catalog:
    manifest = json.loads((Path(dbt_dir) / "manifest.json").read_text(encoding="utf-8"))
    catalog = json.loads((Path(dbt_dir) / "catalog.json").read_text(encoding="utf-8"))
    overlay = yaml.safe_load(Path(overlay_path).read_text(encoding="utf-8")) if overlay_path else {}
    overlay_models = (overlay or {}).get("models", {})

    tests_by_model: dict[str, dict[str | None, list[str]]] = {}
    for node in manifest["nodes"].values():
        if node["resource_type"] != "test":
            continue
        attached = node.get("attached_node") or ""
        label = _test_label(node)
        if attached.startswith(f"model.{PROJECT}.") and label:
            model = attached.split(".")[-1]
            tests_by_model.setdefault(model, {}).setdefault(node.get("column_name"), []).append(
                label
            )
        elif not attached:  # singular test: attach to every model it reads
            for dep in node.get("depends_on", {}).get("nodes", []):
                model = dep.split(".")[-1]
                tests_by_model.setdefault(model, {}).setdefault(None, []).append(node["name"])

    models: dict[str, ModelDoc] = {}
    for name in policy.models:
        uid = f"model.{PROJECT}.{name}"
        node = manifest["nodes"].get(uid)
        cat = catalog["nodes"].get(uid)
        if node is None or cat is None:
            raise KeyError(f"dbt artifacts have no model {name}")
        over = overlay_models.get(name, {})
        col_over = over.get("columns", {})
        model_tests = tests_by_model.get(name, {})
        cols = []
        for col_name, col in sorted(cat["columns"].items(), key=lambda kv: kv[1]["index"]):
            doc = node["columns"].get(col_name, {})
            description = " ".join((col_over.get(col_name) or doc.get("description") or "").split())
            tests = list(model_tests.get(col_name, []))
            for constraint in doc.get("constraints", []):
                if constraint.get("type") in ("not_null", "primary_key"):
                    label = constraint["type"].replace("_", " ")
                    if label not in tests:
                        tests.append(label)
            pii = policy.is_pii(name, col_name)
            cols.append(
                ColumnDoc(
                    name=col_name,
                    data_type=col["type"],
                    description=description,
                    tests=sorted(set(tests)),
                    pii=pii,
                    masking="tokenised as cust_<hash> for analyst/viewer; clear only with approval"
                    if pii
                    else "",
                )
            )
        rekeyed = {c.column for c in policy.rekey_columns if c.model == name}
        for col in cols:
            if col.name in rekeyed and not col.masking:
                col.masking = "re-keyed (opaque join key; the dbt md5 key is re-identifiable)"
        models[name] = ModelDoc(
            name=name,
            description=" ".join(
                (over.get("description") or node.get("description") or "").split()
            ),
            columns=cols,
            materialization=node["config"].get("materialized", ""),
            tags=list(node.get("tags", [])),
            grain=over.get("grain", ""),
            depends_on=[d.split(".")[-1] for d in node["depends_on"]["nodes"]],
            model_tests=sorted(set(model_tests.get(None, []))),
            notes=[" ".join(n.split()) for n in over.get("notes", [])],
            row_count=(row_counts or {}).get(name),
        )
    return Catalog(models, source=f"dbt {manifest['metadata'].get('dbt_version', '?')} artifacts")
