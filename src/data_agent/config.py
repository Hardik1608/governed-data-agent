"""Runtime settings (environment variables prefixed DATA_AGENT_) and the agent configuration.

Two layers:
  - `Settings`: where things live on this machine (paths, endpoints, secrets). Environment.
  - `AgentConfig`: what the agent *is* (prompt, tools, model, budgets). Versioned YAML under
    configs/agents/, registered in MLflow and promoted by the release gate. Two runs with the
    same AgentConfig fingerprint are comparable.
"""

from __future__ import annotations

import hashlib
import json
import os
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parents[1]

DEV_PII_SECRET = "dev-only-pii-secret-change-me"  # noqa: S105 - documented dev default


def _root() -> Path:
    return Path(os.environ.get("DATA_AGENT_HOME", PROJECT_ROOT)).resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATA_AGENT_", extra="ignore")

    home: Path = Field(default_factory=_root)

    # --- data ---------------------------------------------------------------------
    warehouse: Path | None = None  # raw warehouse (only the replica builder opens it)
    replica_dir: Path | None = None
    dbt_dir: Path | None = None
    policy_file: Path | None = None
    metrics_file: Path | None = None
    index_db: Path | None = None
    state_dir: Path | None = None  # checkpoints + audit log
    pii_secret: str = DEV_PII_SECRET

    # --- models -----------------------------------------------------------------------
    agent_config: Path | None = None
    # Optional env file with LOCAL_LLM_BASE_URL / LOCAL_LLM_API_KEY / LOCAL_LLM_MODEL (the
    # shared model server's file); read at start-up so the key never goes through a shell.
    llm_env_file: Path | None = None
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str | None = None  # overrides AgentConfig.model.name when set
    llm_api_key: str = "EMPTY"
    llm_timeout_s: float | None = None
    embed_base_url: str = "http://localhost:11434/v1"
    embed_model: str = "nomic-embed-text"
    embed_api_key: str = "EMPTY"
    embeddings: str = "auto"  # auto | ollama | fake | off

    # --- serving ------------------------------------------------------------------------
    tokens_file: Path | None = None  # API / MCP bearer tokens -> (user, role)
    auth_disabled: bool = False  # local development only; role defaults to analyst
    rate_limit_per_minute: int = 30
    tracing: str = "auto"  # auto | on | off  (auto = on when MLFLOW_TRACKING_URI is set)
    shadow_config: Path | None = None  # challenger agent config replayed on sampled traffic
    shadow_rate: float = 0.0  # share of answered questions replayed in shadow
    mlflow_experiment: str = "governed-data-agent"

    @field_validator("embeddings", "tracing")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()

    def model_post_init(self, __context: Any) -> None:
        if self.llm_env_file is None:
            return
        values = _read_env_file(Path(self.llm_env_file))
        if values.get("LOCAL_LLM_BASE_URL"):
            self.llm_base_url = values["LOCAL_LLM_BASE_URL"]
        if values.get("LOCAL_LLM_API_KEY"):
            self.llm_api_key = values["LOCAL_LLM_API_KEY"]
        if values.get("LOCAL_LLM_MODEL") and not self.llm_model:
            self.llm_model = values["LOCAL_LLM_MODEL"]

    def _p(self, value: Path | None, default: str) -> Path:
        path = value if value is not None else Path(default)
        return path if path.is_absolute() else (self.home / path)

    @property
    def warehouse_path(self) -> Path:
        return self._p(self.warehouse, "data/warehouse/sales_real_replay.duckdb")

    @property
    def replica_path(self) -> Path:
        return self._p(self.replica_dir, "data/replicas")

    @property
    def dbt_path(self) -> Path:
        return self._p(self.dbt_dir, "data/dbt")

    @property
    def policy_path(self) -> Path:
        return self._p(self.policy_file, "configs/policy.yaml")

    @property
    def metrics_path(self) -> Path:
        return self._p(self.metrics_file, "configs/metrics.yaml")

    @property
    def index_path(self) -> Path:
        return self._p(self.index_db, "data/index/catalog_index.duckdb")

    @property
    def state_path(self) -> Path:
        return self._p(self.state_dir, "data/state")

    @property
    def agent_config_path(self) -> Path:
        return self._p(self.agent_config, "configs/agents/governed-v1.yaml")

    @property
    def catalog_snapshot_path(self) -> Path:
        """Committed schema + docs snapshot (lets CI validate gold SQL without the data)."""
        return self.home / "golden" / "catalog_snapshot.json"

    @property
    def uses_dev_secret(self) -> bool:
        return self.pii_secret == DEV_PII_SECRET


# ----------------------------------------------------------------------------------
# Agent configuration (versioned, registered, gated)
# ----------------------------------------------------------------------------------


class ModelConfig(BaseModel):
    name: str = "llama3.2"
    temperature: float = 0.0
    top_p: float | None = None
    top_k: int | None = None  # vLLM / Ollama extension
    presence_penalty: float | None = None
    max_tokens: int = 1024
    timeout_s: float = 180.0
    max_retries: int = 6
    max_concurrency: int = 4  # shared model server: at most 4 requests in flight per process
    max_context_tokens: int = 12_000  # trim history above this (the KV cache is shared)
    # Optional price for cost-per-success (EUR per 1M tokens). Local models have no
    # marginal API price, so the default is 0 and reports show tokens per success instead.
    eur_per_mtok_input: float = 0.0
    eur_per_mtok_output: float = 0.0
    extra_body: dict[str, Any] = Field(default_factory=dict)  # e.g. vLLM chat_template_kwargs


class SqlBudget(BaseModel):
    max_rows: int = 200  # rows returned to the model per query
    timeout_s: float = 15.0  # wall-clock per query (DuckDB interrupt)
    # EXPLAIN estimate of the largest plan operator. Calibrated in reports/budget/: the 220
    # golden queries peak at ~6.5e8; fact-table cross products estimate 2.9e9 .. 1.1e12.
    est_rows_approval: float = 1e9  # above this a human must approve
    est_rows_reject: float = 1e11  # above this the query is rejected outright
    max_queries: int = 12  # per question


class Budgets(BaseModel):
    max_llm_calls: int = 12
    max_total_tokens: int = 80_000
    max_wall_s: float = 300.0
    max_check_retries: int = 2  # traceability repair rounds
    sql: SqlBudget = Field(default_factory=SqlBudget)


class AgentConfig(BaseModel):
    name: str
    version: str
    description: str = ""
    prompt: str  # file under src/data_agent/prompts/
    plan_prompt: str = "plan_v1.md"
    tools: list[str]
    model: ModelConfig = Field(default_factory=ModelConfig)
    budgets: Budgets = Field(default_factory=Budgets)
    dialect: str = "duckdb"
    retrieval_k: int = 8
    use_planner: bool = True
    # When non-empty, the planner may refuse only by naming one of these missing-data
    # categories; any other refusal is overruled and the tool loop decides with evidence.
    planner_refuse_categories: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> AgentConfig:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def prompt_text(self, name: str | None = None) -> str:
        return (PACKAGE_DIR / "prompts" / (name or self.prompt)).read_text(encoding="utf-8")

    @cached_property
    def fingerprint(self) -> str:
        """Hash of everything that changes behaviour: config + prompt texts."""
        payload = {
            "config": self.model_dump(mode="json"),
            "prompt": self.prompt_text(),
            "plan_prompt": self.prompt_text(self.plan_prompt) if self.use_planner else None,
        }
        blob = json.dumps(payload, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"LLM env file not found: {path}")
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def get_settings(**overrides: Any) -> Settings:
    return Settings(**overrides)
