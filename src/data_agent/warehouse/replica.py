"""Build per-role serving replicas of the warehouse.

Each replica is a small DuckDB file holding exactly what one class of user may see:
  - `masked`: every reporting model; customer identifiers replaced by keyed-hash tokens and
    the md5-derived keys (customer_sk, order_line_id) re-keyed. No raw identifier is stored,
    so masking cannot be bypassed by any SQL the agent writes.
  - `clear`: the same models in clear (opened only after a recorded human approval).
  - `aggregates`: marts and non-customer dimensions only.

Builds are atomic (write to a temp file, then rename) and fingerprinted: a sidecar JSON
records the source warehouse hash, the policy hash and a fingerprint of the PII secret, and
`ensure_replicas` rebuilds only what is stale. The secret is passed as a bound parameter and
is never written to the replica.

Product-description overrides (used to build the prompt-injection TEST copy) are applied at
build time to a separate replica; the production replicas never receive them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from data_agent.governance.policy import PiiColumn, Policy, ReplicaSpec

log = logging.getLogger(__name__)

SOURCE_SCHEMA = "gold"
# Physical sort keys: date-ordered facts let DuckDB's zone maps skip row groups.
SORT_KEYS = {"fct_order_lines": "invoice_ts", "fct_orders": "invoice_ts", "dim_date": "date_key"}
REPLICA_FORMAT_VERSION = 2


@dataclass(frozen=True)
class ReplicaInfo:
    name: str
    path: Path
    meta: dict


def secret_fingerprint(secret: str) -> str:
    return hashlib.sha256(("fingerprint:" + secret).encode()).hexdigest()[:12]


def token(secret: str, kind: str, value: str, prefix: str, length: int = 12) -> str:
    """Python twin of the SQL tokeniser (used by tests and the output redactor)."""
    digest = hashlib.sha256(f"{secret}:{kind}:{value}".encode()).hexdigest()
    return prefix + digest[:length]


def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def _source_fingerprint(raw: Path) -> str:
    """Cheap identity of the source file: size + mtime + sha256 recorded by make_data."""
    provenance = raw.parent.parent / "PROVENANCE.json"
    if provenance.exists():
        try:
            for f in json.loads(provenance.read_text(encoding="utf-8"))["files"]:
                if Path(f["path"]).name == raw.name:
                    return f["sha256"]
        except (KeyError, ValueError):
            pass
    stat = raw.stat()
    return f"size={stat.st_size};mtime={int(stat.st_mtime)}"


def _token_sql(col: PiiColumn) -> str:
    """SQL expression tokenising one column; `?` is the secret (bound parameter)."""
    expr = f"'{col.prefix}' || substr(sha256(? || ':{col.kind}:' || CAST(\"{col.column}\" AS VARCHAR)), 1, {col.length})"
    keep = " OR ".join(f"\"{col.column}\" = '{v}'" for v in col.keep)
    keep_clause = f'WHEN {keep} THEN "{col.column}" ' if keep else ""
    return f'CASE WHEN "{col.column}" IS NULL THEN NULL {keep_clause}ELSE {expr} END'


def _select_list(
    con: duckdb.DuckDBPyConnection, model: str, spec: ReplicaSpec, policy: Policy
) -> tuple[str, int]:
    """Projection for one model under the replica's PII mode. Returns (sql, n_params)."""
    columns = [
        r[0]
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_catalog = 'src' AND table_schema = ? AND table_name = ? "
            "ORDER BY ordinal_position",
            [SOURCE_SCHEMA, model],
        ).fetchall()
    ]
    if not columns:
        raise RuntimeError(f"model {SOURCE_SCHEMA}.{model} not found in the source warehouse")
    pii = {c.column: c for c in policy.pii_columns if c.model == model}
    rekey = {c.column: c for c in policy.rekey_columns if c.model == model}
    parts, n_params = [], 0
    for name in columns:
        if spec.pii == "clear" or (name not in pii and name not in rekey):
            parts.append(f'"{name}"')
        elif spec.pii == "drop" and name in pii:
            continue
        else:  # tokenize identifiers; re-key derived keys (also under "drop")
            parts.append(f'{_token_sql(pii.get(name) or rekey[name])} AS "{name}"')
            n_params += 1
    return ", ".join(parts), n_params


def build_replica(
    raw: Path,
    out: Path,
    spec: ReplicaSpec,
    policy: Policy,
    secret: str,
    description_overrides: dict[str, str] | None = None,
    threads: int = 4,
    memory_limit: str = "1GB",
) -> ReplicaInfo:
    raw, out = Path(raw).resolve(), Path(out).resolve()
    if not raw.exists():
        raise FileNotFoundError(f"warehouse not found: {raw} (run `python scripts/make_data.py`)")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".building.duckdb")
    tmp.unlink(missing_ok=True)
    started = time.perf_counter()
    row_counts: dict[str, int] = {}
    con = duckdb.connect(str(tmp), config={"threads": threads, "memory_limit": memory_limit})
    try:
        quoted = str(raw).replace("'", "''")  # ATTACH takes no bound parameters
        con.execute(f"ATTACH '{quoted}' AS src (READ_ONLY)")
        if description_overrides:
            con.execute("CREATE TEMP TABLE _desc_override(stock_code VARCHAR, description VARCHAR)")
            con.executemany(
                "INSERT INTO _desc_override VALUES (?, ?)", list(description_overrides.items())
            )
        for model in spec.models:
            select, n_params = _select_list(con, model, spec, policy)
            source = f"src.{SOURCE_SCHEMA}.{model}"
            if model == "dim_product" and description_overrides:
                select = select.replace(
                    '"product_description"',
                    'coalesce(o.description, p."product_description") AS "product_description"',
                )
                source = f"{source} AS p LEFT JOIN _desc_override AS o USING (stock_code)"
            order = f" ORDER BY {SORT_KEYS[model]}" if model in SORT_KEYS else ""
            con.execute(
                f'CREATE TABLE main."{model}" AS SELECT {select} FROM {source}{order}',
                [secret] * n_params,
            )
            row_counts[model] = con.execute(f'SELECT count(*) FROM main."{model}"').fetchone()[0]
        con.execute("DETACH src")
        con.execute("CHECKPOINT")
    finally:
        con.close()
    os.replace(tmp, out)
    meta = {
        "replica": spec.name,
        "format_version": REPLICA_FORMAT_VERSION,
        "pii_mode": spec.pii,
        "models": list(spec.models),
        "row_counts": row_counts,
        "source": str(raw),
        "source_fingerprint": _source_fingerprint(raw),
        "policy_fingerprint": policy.fingerprint,
        "secret_fingerprint": secret_fingerprint(secret),
        "description_overrides": sorted(description_overrides) if description_overrides else [],
        "duckdb_version": duckdb.__version__,
        "built_at": datetime.now(UTC).isoformat(),
        "build_seconds": round(time.perf_counter() - started, 2),
        "bytes": out.stat().st_size,
    }
    out.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log.info("built replica %s in %.1fs (%s)", spec.name, meta["build_seconds"], out)
    return ReplicaInfo(spec.name, out, meta)


def replica_file(replica_dir: Path, name: str) -> Path:
    return Path(replica_dir) / f"{name}.duckdb"


def is_stale(path: Path, raw: Path, policy: Policy, secret: str, overrides_keys=()) -> str | None:
    """Reason the replica must be rebuilt, or None when it is current."""
    meta_path = path.with_suffix(".json")
    if not path.exists() or not meta_path.exists():
        return "missing"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except ValueError:
        return "unreadable metadata"
    if meta.get("format_version") != REPLICA_FORMAT_VERSION:
        return "format version changed"
    if meta.get("policy_fingerprint") != policy.fingerprint:
        return "policy changed"
    if meta.get("secret_fingerprint") != secret_fingerprint(secret):
        return "PII secret changed"
    if raw.exists() and meta.get("source_fingerprint") != _source_fingerprint(raw):
        return "source warehouse changed"
    if sorted(overrides_keys) != meta.get("description_overrides", []):
        return "description overrides changed"
    return None


def ensure_replicas(
    raw: Path,
    replica_dir: Path,
    policy: Policy,
    secret: str,
    names: list[str] | None = None,
    force: bool = False,
) -> dict[str, ReplicaInfo]:
    out: dict[str, ReplicaInfo] = {}
    for name in names or list(policy.replicas):
        spec = policy.replicas[name]
        path = replica_file(replica_dir, name)
        reason = "forced" if force else is_stale(path, raw, policy, secret)
        if reason:
            log.info("building replica %s (%s)", name, reason)
            out[name] = build_replica(raw, path, spec, policy, secret)
        else:
            out[name] = ReplicaInfo(
                name, path, json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            )
    return out


def build_injection_replica(
    raw: Path,
    replica_dir: Path,
    policy: Policy,
    secret: str,
    overrides: dict[str, str],
    base: str = "masked",
    force: bool = False,
) -> ReplicaInfo:
    """TEST COPY of a replica with planted instructions in product descriptions."""
    spec = policy.replicas[base]
    test_spec = ReplicaSpec(
        f"{base}_injection", spec.models, spec.pii, "prompt-injection test copy"
    )
    path = replica_file(replica_dir, test_spec.name)
    reason = "forced" if force else is_stale(path, raw, policy, secret, overrides.keys())
    if reason is None:
        return ReplicaInfo(
            test_spec.name, path, json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        )
    return build_replica(raw, path, test_spec, policy, secret, description_overrides=overrides)
