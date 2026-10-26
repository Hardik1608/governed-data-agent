"""Wire the components together from Settings (used by the CLI, the API, MCP and the eval)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from data_agent.config import AgentConfig, Settings
from data_agent.governance.audit import AuditLog
from data_agent.governance.pii import PiiRedactor
from data_agent.governance.policy import Policy
from data_agent.llm.client import ChatModel, OpenAICompatibleClient
from data_agent.observability.tracing import Tracer, make_tracer
from data_agent.retrieval.embeddings import Embedder, HashingEmbedder, OpenAIEmbedder
from data_agent.retrieval.index import CatalogIndex, build_documents
from data_agent.safety.guard import ExecutorPool, SqlGuard
from data_agent.tools.toolkit import ToolKit
from data_agent.warehouse.catalog import Catalog, build_catalog
from data_agent.warehouse.metrics import MetricsLayer

log = logging.getLogger(__name__)

PII_FINGERPRINTS = "pii_fingerprints.json"


def load_catalog(settings: Settings, policy: Policy) -> Catalog:
    """dbt artifacts when present (local), else the committed snapshot (CI)."""
    overlay = settings.home / "configs" / "catalog_overlay.yaml"
    if (settings.dbt_path / "manifest.json").exists():
        return build_catalog(settings.dbt_path, policy, overlay if overlay.exists() else None)
    if settings.catalog_snapshot_path.exists():
        return Catalog.load(settings.catalog_snapshot_path)
    raise FileNotFoundError(
        "no dbt artifacts under data/dbt and no golden/catalog_snapshot.json; "
        "run `python scripts/make_data.py`"
    )


def make_embedder(settings: Settings) -> Embedder | None:
    mode = settings.embeddings
    if mode == "off":
        return None
    if mode == "fake":
        return HashingEmbedder()
    embedder = OpenAIEmbedder(settings.embed_base_url, settings.embed_model, settings.embed_api_key)
    if mode == "ollama" or embedder.available():
        return embedder
    log.warning(
        "embedding server unavailable at %s: catalog search falls back to BM25",
        settings.embed_base_url,
    )
    return None


def make_llm(settings: Settings, config: AgentConfig) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model or config.model.name,
        api_key=settings.llm_api_key,
        timeout_s=settings.llm_timeout_s or config.model.timeout_s,
        max_retries=config.model.max_retries,
        temperature=config.model.temperature,
        max_tokens=config.model.max_tokens,
        extra_body=config.model.extra_body,
        max_concurrency=config.model.max_concurrency,
        sampling={
            "top_p": config.model.top_p,
            "top_k": config.model.top_k,
            "presence_penalty": config.model.presence_penalty,
        },
    )


@dataclass
class Runtime:
    settings: Settings
    config: AgentConfig
    policy: Policy
    metrics: MetricsLayer
    catalog: Catalog
    index: CatalogIndex
    pool: ExecutorPool
    toolkits: dict[str, ToolKit]
    audit: AuditLog
    llm: ChatModel
    tracer: Tracer
    redactor: PiiRedactor
    extras: dict = field(default_factory=dict)

    def close(self) -> None:
        self.pool.close()
        self.audit.close()
        close = getattr(self.llm, "close", None)
        if callable(close):
            close()


def build_runtime(
    settings: Settings,
    config: AgentConfig | None = None,
    llm: ChatModel | None = None,
    replica_overrides: dict[str, Path] | None = None,
    embedder: Embedder | None | str = "settings",
    audit_path: Path | str | None = None,
    tracer: Tracer | None = None,
) -> Runtime:
    config = config or AgentConfig.load(settings.agent_config_path)
    policy = Policy.load(settings.policy_path)
    metrics = MetricsLayer.load(settings.metrics_path)
    catalog = load_catalog(settings, policy)
    if embedder == "settings":
        embedder = make_embedder(settings)
    index = CatalogIndex.build(
        build_documents(catalog, metrics), embedder, settings.index_path if embedder else None
    )
    pool = ExecutorPool(
        settings.replica_path,
        timeout_s=config.budgets.sql.timeout_s,
        max_rows=config.budgets.sql.max_rows,
        overrides=replica_overrides,
    )
    toolkits: dict[str, ToolKit] = {}
    for role_name, role in policy.roles.items():
        replica_path = pool.path_of(role.replica)
        if not replica_path.exists():
            log.warning(
                "replica %s missing (%s): role %s disabled", role.replica, replica_path, role_name
            )
            continue
        schema = pool.get(role.replica).tables()
        role_catalog = catalog.for_models(role.models)
        guard = SqlGuard(policy, role, pool, config.budgets.sql, schema)
        toolkits[role_name] = ToolKit(policy, role, role_catalog, metrics, index, guard)
    if not toolkits:
        raise FileNotFoundError(
            f"no replicas under {settings.replica_path}; run `data-agent build-replicas`"
        )
    audit = AuditLog(audit_path or settings.state_path / "audit.sqlite")
    redactor = PiiRedactor.load(settings.replica_path / PII_FINGERPRINTS, settings.pii_secret)
    return Runtime(
        settings=settings,
        config=config,
        policy=policy,
        metrics=metrics,
        catalog=catalog,
        index=index,
        pool=pool,
        toolkits=toolkits,
        audit=audit,
        llm=llm or make_llm(settings, config),
        tracer=tracer or make_tracer(settings.tracing, settings.mlflow_experiment),
        redactor=redactor,
    )
