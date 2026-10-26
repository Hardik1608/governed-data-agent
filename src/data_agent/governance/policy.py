"""Role-based access policy: models per role, PII-tagged columns, replica definitions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PII_MODES = ("tokenize", "clear", "drop")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class PiiColumn:
    model: str
    column: str
    kind: str
    prefix: str
    length: int = 12
    keep: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplicaSpec:
    name: str
    models: tuple[str, ...]
    pii: str
    description: str = ""


@dataclass(frozen=True)
class RolePolicy:
    name: str
    replica: str
    models: tuple[str, ...]
    clear_replica: str | None = None
    description: str = ""

    @property
    def can_request_clear_pii(self) -> bool:
        return self.clear_replica is not None


@dataclass
class Policy:
    models: tuple[str, ...]
    pii_columns: tuple[PiiColumn, ...]
    rekey_columns: tuple[PiiColumn, ...]
    replicas: dict[str, ReplicaSpec]
    roles: dict[str, RolePolicy]
    raw: dict = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: Path) -> Policy:
        return cls.from_dict(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, data: dict) -> Policy:
        try:
            models = tuple(data["models"])
            pii = data.get("pii", {})
            pii_columns = tuple(_pii_column(c) for c in pii.get("columns", []))
            rekey = tuple(_pii_column(c) for c in pii.get("rekey", []))
            replicas: dict[str, ReplicaSpec] = {}
            for name, spec in data["replicas"].items():
                rep_models = models if spec["models"] == "all" else tuple(spec["models"])
                if spec["pii"] not in PII_MODES:
                    raise PolicyError(f"replica {name}: pii must be one of {PII_MODES}")
                unknown = set(rep_models) - set(models)
                if unknown:
                    raise PolicyError(f"replica {name}: unknown models {sorted(unknown)}")
                replicas[name] = ReplicaSpec(
                    name, rep_models, spec["pii"], spec.get("description", "")
                )
            roles: dict[str, RolePolicy] = {}
            for name, spec in data["roles"].items():
                replica = spec["replica"]
                if replica not in replicas:
                    raise PolicyError(f"role {name}: unknown replica {replica}")
                clear = spec.get("clear_replica")
                if clear is not None and replicas.get(clear) is None:
                    raise PolicyError(f"role {name}: unknown clear_replica {clear}")
                if replicas[replica].pii == "clear":
                    raise PolicyError(
                        f"role {name}: the default replica must not expose clear PII; "
                        "use clear_replica (approval-gated) instead"
                    )
                roles[name] = RolePolicy(
                    name=name,
                    replica=replica,
                    models=replicas[replica].models,
                    clear_replica=clear,
                    description=" ".join(str(spec.get("description", "")).split()),
                )
        except KeyError as e:
            raise PolicyError(f"policy is missing key {e}") from e
        for col in pii_columns + rekey:
            if col.model not in models:
                raise PolicyError(f"PII column on unknown model {col.model}")
        return cls(models, pii_columns, rekey, replicas, roles, raw=data)

    # ------------------------------------------------------------------ lookups
    def role(self, name: str) -> RolePolicy:
        try:
            return self.roles[name]
        except KeyError:
            raise PolicyError(f"unknown role '{name}' (known: {sorted(self.roles)})") from None

    def pii_columns_of(self, model: str) -> list[PiiColumn]:
        return [c for c in self.pii_columns if c.model == model]

    def is_pii(self, model: str, column: str) -> bool:
        return any(c.model == model and c.column == column for c in self.pii_columns)

    def pii_column_names(self) -> set[str]:
        return {c.column for c in self.pii_columns}

    @property
    def fingerprint(self) -> str:
        blob = json.dumps(self.raw, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


def _pii_column(c: dict) -> PiiColumn:
    return PiiColumn(
        model=c["model"],
        column=c["column"],
        kind=c["kind"],
        prefix=c["prefix"],
        length=int(c.get("length", 12)),
        keep=tuple(str(v) for v in c.get("keep", [])),
    )
