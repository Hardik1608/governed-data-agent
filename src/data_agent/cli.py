"""`data-agent` command line.

data-agent build-replicas [--force]          per-role read-only replicas + PII fingerprints
data-agent build-index                       embed the catalog (Ollama nomic-embed-text)
data-agent snapshot-catalog                  write golden/catalog_snapshot.json (for CI)
data-agent ask "question" [--role analyst]   one question; prompts for approvals
data-agent serve [--port 8000]               FastAPI + Prometheus /metrics
data-agent mcp [--transport stdio|http]      MCP server
data-agent token --user U --role R           create an API / MCP bearer token
data-agent eval ...                          golden-set evaluation (see --help)
data-agent gate ...                          release gate (champion / challenger)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from data_agent.config import AgentConfig, Settings


def _settings(args) -> Settings:
    overrides = {}
    if getattr(args, "agent_config", None):
        overrides["agent_config"] = Path(args.agent_config)
    return Settings(**overrides)


def cmd_build_replicas(args) -> int:
    from data_agent.governance.pii import write_fingerprints
    from data_agent.governance.policy import Policy
    from data_agent.runtime import PII_FINGERPRINTS
    from data_agent.warehouse.replica import build_injection_replica, ensure_replicas

    s = _settings(args)
    if s.uses_dev_secret:
        print(
            "warning: DATA_AGENT_PII_SECRET is not set; using the dev secret (tokens are not private)",
            file=sys.stderr,
        )
    policy = Policy.load(s.policy_path)
    infos = ensure_replicas(
        s.warehouse_path, s.replica_path, policy, s.pii_secret, force=args.force
    )
    for name, info in infos.items():
        meta = info.meta
        print(
            f"{name:12} {meta['bytes'] / 1e6:6.1f} MB  built {meta['built_at']}  rows {meta['row_counts']}"
        )
    n = write_fingerprints(s.warehouse_path, s.replica_path / PII_FINGERPRINTS, s.pii_secret)
    print(f"pii fingerprints: {n} customer ids")
    if args.injections:
        from data_agent.eval.golden import load_injections

        overrides = load_injections(Path(args.injections))
        info = build_injection_replica(
            s.warehouse_path, s.replica_path, policy, s.pii_secret, overrides, force=args.force
        )
        print(
            f"{info.name:12} {info.meta['bytes'] / 1e6:6.1f} MB  ({len(overrides)} planted descriptions)"
        )
    return 0


def cmd_snapshot_catalog(args) -> int:
    from data_agent.governance.policy import Policy
    from data_agent.runtime import load_catalog
    from data_agent.safety.guard import ExecutorPool

    s = _settings(args)
    policy = Policy.load(s.policy_path)
    catalog = load_catalog(s, policy)
    pool = ExecutorPool(s.replica_path)
    try:
        counts = pool.get("masked").row_counts()
    finally:
        pool.close()
    for name, model in catalog.models.items():
        model.row_count = counts.get(name)
    catalog.save(s.catalog_snapshot_path)
    print(f"wrote {s.catalog_snapshot_path} ({len(catalog.models)} models)")
    return 0


def cmd_build_index(args) -> int:
    from data_agent.governance.policy import Policy
    from data_agent.retrieval.index import CatalogIndex, build_documents
    from data_agent.runtime import load_catalog, make_embedder
    from data_agent.warehouse.metrics import MetricsLayer

    s = _settings(args)
    policy = Policy.load(s.policy_path)
    docs = build_documents(load_catalog(s, policy), MetricsLayer.load(s.metrics_path))
    embedder = make_embedder(s)
    if embedder is None:
        print("no embedding server: nothing to build (search will use BM25)", file=sys.stderr)
        return 1
    CatalogIndex.build(docs, embedder, s.index_path)
    print(f"indexed {len(docs)} documents with {embedder.name} -> {s.index_path}")
    return 0


def _prompt_answers(interrupt: dict, policy: str) -> dict:
    answers = {}
    for req in interrupt.get("requests", []):
        kind = req.get("kind")
        print(
            f"\n[{kind}] "
            + json.dumps({k: v for k, v in req.items() if k not in ("id", "kind")}, indent=1)
        )
        if policy == "approve" and kind == "approval":
            answers[req["id"]] = {"approved": True, "by": "cli --approve"}
        elif policy == "deny" and kind == "approval":
            answers[req["id"]] = {"approved": False, "by": "cli --deny"}
        elif kind == "approval":
            ok = input("approve? [y/N] ").strip().lower() == "y"
            answers[req["id"]] = {"approved": ok, "by": os.environ.get("USERNAME", "cli")}
        else:
            answers[req["id"]] = {"reply": input("your answer: ").strip()}
    return answers


def cmd_build_mini(args) -> int:
    """Synthetic warehouse with the real schema + replicas, for CI (no real data needed)."""
    from data_agent.eval.golden import load_injections
    from data_agent.governance.pii import write_fingerprints
    from data_agent.governance.policy import Policy
    from data_agent.runtime import PII_FINGERPRINTS
    from data_agent.warehouse.catalog import Catalog
    from data_agent.warehouse.mini import build_mini_warehouse
    from data_agent.warehouse.replica import build_injection_replica, ensure_replicas

    s = _settings(args)
    out = Path(args.out)
    policy = Policy.load(s.policy_path)
    raw = build_mini_warehouse(
        out / "warehouse" / "mini.duckdb", Catalog.load(s.catalog_snapshot_path)
    )
    ensure_replicas(raw, out / "replicas", policy, s.pii_secret, force=True)
    write_fingerprints(raw, out / "replicas" / PII_FINGERPRINTS, s.pii_secret)
    plants = load_injections(s.home / "golden" / "v1")
    build_injection_replica(raw, out / "replicas", policy, s.pii_secret, plants, force=True)
    print(f"mini warehouse + replicas under {out}")
    print(
        f"use: DATA_AGENT_WAREHOUSE={raw} DATA_AGENT_REPLICA_DIR={out / 'replicas'} DATA_AGENT_DBT_DIR={out / 'no-dbt'}"
    )
    return 0


def cmd_ask(args) -> int:
    from data_agent.agent.service import INTERRUPTED, AgentService
    from data_agent.runtime import build_runtime

    s = _settings(args)
    runtime = build_runtime(s)
    service = AgentService(runtime)
    try:
        result = service.ask(
            args.question, user_id=args.user, role=args.role, thread_id=args.thread
        )
        while result.status == INTERRUPTED:
            answers = _prompt_answers(result.interrupt, args.approvals)
            result = service.resume(result.thread_id, args.user, answers)
        print(f"\n[{result.status}] {result.answer}\n")
        for src in result.sources:
            print(f"  {src['query_id']}: {src['sql']}")
        print(f"\nthread {result.thread_id} | {result.usage} | {result.latency_s}s")
        if args.json:
            print(json.dumps(result.to_dict(), indent=1, default=str))
    finally:
        service.close()
        runtime.close()
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run(
        "data_agent.api.app:create_app", factory=True, host=args.host, port=args.port, workers=1
    )
    return 0


def cmd_mcp(args) -> int:
    from data_agent.mcp_server.server import run

    run(transport=args.transport, host=args.host, port=args.port)
    return 0


def cmd_token(args) -> int:
    """Create a bearer token for the API / MCP server.

    Only the token's sha256 is stored (in the tokens file, next to the user and role); the token
    itself is printed once, on stdout.
    """
    from data_agent.api.auth import make_token
    from data_agent.governance.policy import Policy, PolicyError

    s = _settings(args)
    try:
        Policy.load(s.policy_path).role(args.role)
    except PolicyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    path = Path(args.file) if args.file else (s.tokens_file or s.state_path / "tokens.json")
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"tokens": []}
    token, entry = make_token(args.user, args.role)
    data.setdefault("tokens", []).append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    print(token)
    print(f"added {args.user} ({args.role}) to {path}; the token is not stored", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("DATA_AGENT_LOG_LEVEL", "WARNING"),
        format="%(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="data-agent", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--agent-config", help="agent configuration YAML (default configs/agents/governed-v1.yaml)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build-replicas", help="build per-role read-only replicas")
    p.add_argument("--force", action="store_true")
    p.add_argument("--injections", help="also build the prompt-injection TEST copy from this YAML")
    p.set_defaults(func=cmd_build_replicas)

    p = sub.add_parser("build-index", help="embed the catalog for hybrid search")
    p.set_defaults(func=cmd_build_index)

    p = sub.add_parser("snapshot-catalog", help="write golden/catalog_snapshot.json")
    p.set_defaults(func=cmd_snapshot_catalog)

    p = sub.add_parser("build-mini", help="synthetic warehouse + replicas for CI")
    p.add_argument("--out", default="data/mini")
    p.set_defaults(func=cmd_build_mini)

    p = sub.add_parser("ask", help="ask one question")
    p.add_argument("question")
    p.add_argument("--role", default="analyst")
    p.add_argument("--user", default="cli")
    p.add_argument("--thread")
    p.add_argument("--approvals", choices=["prompt", "approve", "deny"], default="prompt")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("serve", help="run the FastAPI service")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("mcp", help="run the MCP server")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("token", help="create an API / MCP bearer token (stores only its hash)")
    p.add_argument("--user", required=True)
    p.add_argument("--role", required=True, help="a role from configs/policy.yaml")
    p.add_argument(
        "--file", help="tokens file (default DATA_AGENT_TOKENS_FILE or data/state/tokens.json)"
    )
    p.set_defaults(func=cmd_token)

    from data_agent.eval import cli as eval_cli
    from data_agent.release import cli as release_cli

    eval_cli.register(sub)
    release_cli.register(sub)
    from data_agent.bench import bird

    bird.register(sub)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "AgentConfig"]
