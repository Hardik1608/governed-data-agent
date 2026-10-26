"""Shared fixtures.

Tests run on a laptop or in CI with no GPU, Docker, network, API key or real data:
  - `mini_env` builds a synthetic warehouse with the real schema (from the committed catalog
    snapshot), then the per-role replicas with the real policy, in a temp directory;
  - LLMs are scripted fakes; embeddings use the deterministic hashing embedder.
Tests that need the real warehouse, Ollama or a model server carry a marker and skip when
the resource is missing.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
os.environ["DATA_AGENT_TRACING"] = "off"

ROOT = Path(__file__).resolve().parents[1]
REAL_WAREHOUSE = ROOT / "data" / "warehouse" / "sales_real_replay.duckdb"
REAL_REPLICAS = ROOT / "data" / "replicas" / "masked.duckdb"


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config, items):
    have_wh = REAL_WAREHOUSE.exists() and REAL_REPLICAS.exists()
    have_ollama = _port_open("127.0.0.1", 11434)
    have_llm = bool(os.environ.get("DATA_AGENT_LLM_BASE_URL"))
    for item in items:
        if "warehouse" in item.keywords and not have_wh:
            item.add_marker(
                pytest.mark.skip(
                    reason="real warehouse not present (run scripts/make_data.py + data-agent build-replicas)"
                )
            )
        if "ollama" in item.keywords and not have_ollama:
            item.add_marker(pytest.mark.skip(reason="Ollama not running on localhost:11434"))
        if "llm" in item.keywords and not have_llm:
            item.add_marker(pytest.mark.skip(reason="DATA_AGENT_LLM_BASE_URL not set"))


@pytest.fixture(scope="session")
def snapshot_catalog():
    from data_agent.warehouse.catalog import Catalog

    return Catalog.load(ROOT / "golden" / "catalog_snapshot.json")


@pytest.fixture(scope="session")
def policy():
    from data_agent.governance.policy import Policy

    return Policy.load(ROOT / "configs" / "policy.yaml")


@pytest.fixture(scope="session")
def mini_env(tmp_path_factory, snapshot_catalog, policy):
    """Synthetic raw warehouse + per-role replicas + PII fingerprints, built once per session."""
    from data_agent.config import Settings
    from data_agent.governance.pii import write_fingerprints
    from data_agent.runtime import PII_FINGERPRINTS
    from data_agent.warehouse.mini import build_mini_warehouse
    from data_agent.warehouse.replica import ensure_replicas

    base = tmp_path_factory.mktemp("mini")
    raw = build_mini_warehouse(base / "warehouse" / "mini.duckdb", snapshot_catalog)
    settings = Settings(
        home=ROOT,
        warehouse=raw,
        replica_dir=base / "replicas",
        dbt_dir=base / "no-dbt",  # forces the committed catalog snapshot
        index_db=base / "index" / "catalog.duckdb",
        state_dir=base / "state",
        pii_secret="test-secret",
        embeddings="fake",
        tracing="off",
    )
    ensure_replicas(raw, settings.replica_path, policy, settings.pii_secret)
    write_fingerprints(raw, settings.replica_path / PII_FINGERPRINTS, settings.pii_secret)
    return settings


@pytest.fixture
def make_runtime(mini_env, tmp_path):
    """Factory: a full runtime on the mini warehouse with a given (fake) LLM."""
    from data_agent.retrieval.embeddings import HashingEmbedder
    from data_agent.runtime import build_runtime

    built = []

    def _make(llm, config=None, **kw):
        rt = build_runtime(
            mini_env,
            config=config,
            llm=llm,
            embedder=HashingEmbedder(),
            audit_path=tmp_path / f"audit{len(built)}.sqlite",
            **kw,
        )
        built.append(rt)
        return rt

    yield _make
    for rt in built:
        rt.close()


@pytest.fixture
def make_service(make_runtime, tmp_path):
    from data_agent.agent.service import AgentService

    services = []

    def _make(llm, config=None, max_question_chars=2_000, **kw):
        svc = AgentService(
            make_runtime(llm, config, **kw),
            checkpoint_path=tmp_path / f"ckpt{len(services)}.sqlite",
            max_question_chars=max_question_chars,
        )
        services.append(svc)
        return svc

    yield _make
    for svc in services:
        svc.close()
