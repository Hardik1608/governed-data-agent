"""The `run_sql` gate: validation + cost estimate + policy decision, then guarded execution.

`SqlGuard.check()` is pure with respect to the warehouse (parse, validate, EXPLAIN), so the
agent graph can call it again when a node re-runs after a LangGraph interrupt. It returns a
decision:
  - allow           : run it on the role's replica;
  - needs_approval  : over the soft cost budget, or a request for clear identifiers by a
                      role allowed to ask for them -> the graph interrupts for a human;
  - reject          : invalid or unsafe SQL, over the hard budget, or a PII request the role
                      may not make -> the reason goes back to the model.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from data_agent.config import SqlBudget
from data_agent.governance.policy import Policy, RolePolicy
from data_agent.safety.executor import (
    CostEstimate,
    ExecutionError,
    ExecutionResult,
    SafeDuckDBExecutor,
)
from data_agent.safety.validator import SQLValidator, ValidationResult
from data_agent.warehouse.replica import replica_file

ALLOW = "allow"
NEEDS_APPROVAL = "needs_approval"
REJECT = "reject"


@dataclass
class GuardDecision:
    decision: str
    sql: str
    safe_sql: str | None = None
    validation: ValidationResult | None = None
    estimate: CostEstimate | None = None
    reasons: list[str] = field(default_factory=list)
    approval_kinds: list[str] = field(default_factory=list)  # "budget" | "pii"
    reveal_pii: bool = False

    @property
    def unsafe(self) -> bool:
        return bool(self.validation and self.validation.unsafe)

    def to_dict(self) -> dict:
        out = {
            "decision": self.decision,
            "reasons": self.reasons,
            "approval_kinds": self.approval_kinds,
            "safe_sql": self.safe_sql,
        }
        if self.validation is not None:
            out["violations"] = [v.to_dict() for v in self.validation.violations]
            out["pii_columns"] = [f"{t}.{c}" for t, c in self.validation.pii_columns]
        if self.estimate is not None:
            out["estimate"] = self.estimate.to_dict()
        return out


class ExecutorPool:
    """One read-only connection per replica, shared by all requests of the process."""

    def __init__(
        self,
        replica_dir: Path,
        timeout_s: float = 15.0,
        max_rows: int = 200,
        threads: int = 4,
        memory_limit: str = "1GB",
        overrides: dict[str, Path] | None = None,
    ):
        self.replica_dir = Path(replica_dir)
        self.timeout_s = timeout_s
        self.max_rows = max_rows
        self.threads = threads
        self.memory_limit = memory_limit
        self.overrides = dict(overrides or {})
        self._executors: dict[str, SafeDuckDBExecutor] = {}
        self._lock = threading.Lock()

    def path_of(self, replica: str) -> Path:
        return self.overrides.get(replica) or replica_file(self.replica_dir, replica)

    def get(self, replica: str) -> SafeDuckDBExecutor:
        with self._lock:
            ex = self._executors.get(replica)
            if ex is None:
                ex = SafeDuckDBExecutor(
                    self.path_of(replica),
                    timeout_s=self.timeout_s,
                    max_rows=self.max_rows,
                    threads=self.threads,
                    memory_limit=self.memory_limit,
                )
                self._executors[replica] = ex
            return ex

    def close(self) -> None:
        with self._lock:
            for ex in self._executors.values():
                ex.close()
            self._executors.clear()


class SqlGuard:
    def __init__(
        self,
        policy: Policy,
        role: RolePolicy,
        pool: ExecutorPool,
        budget: SqlBudget,
        schema: dict[str, dict[str, str]],
    ):
        self.policy = policy
        self.role = role
        self.pool = pool
        self.budget = budget
        allowed = {m.lower() for m in role.models}
        self.schema = {t: cols for t, cols in schema.items() if t.lower() in allowed}
        pii = {}
        for col in policy.pii_columns:
            if col.model in self.schema:
                pii.setdefault(col.model, set()).add(col.column)
        self.validator = SQLValidator(self.schema, dialect="duckdb", pii=pii)

    def check(self, sql: str, reveal_pii: bool = False) -> GuardDecision:
        validation = self.validator.validate(sql)
        decision = GuardDecision(REJECT, sql, validation=validation, reveal_pii=reveal_pii)
        if not validation.ok:
            decision.reasons.append(validation.feedback())
            return decision
        decision.safe_sql = validation.safe_sql
        try:
            estimate = self.pool.get(self.role.replica).explain(validation.safe_sql)
        except ExecutionError as e:  # binder errors: wrong types, unknown functions, ...
            decision.reasons.append(f"Database rejected the query: {e}")
            return decision
        decision.estimate = estimate
        # Budget on the largest operator's estimated output: robust to plan shape (the sum
        # over operators double-counts pipelines). Thresholds: reports/budget/calibration.md.
        cost = estimate.max_rows
        if cost > self.budget.est_rows_reject:
            decision.reasons.append(
                f"Estimated cost {cost:,.0f} rows (largest plan step) exceeds the hard budget of "
                f"{self.budget.est_rows_reject:,.0f}. Avoid cross joins; aggregate earlier or add filters."
            )
            return decision
        if cost > self.budget.est_rows_approval:
            decision.approval_kinds.append("budget")
            decision.reasons.append(
                f"Estimated cost {cost:,.0f} rows (largest plan step) exceeds the approval threshold "
                f"of {self.budget.est_rows_approval:,.0f}."
            )
        if reveal_pii:
            if not validation.pii_columns:
                decision.reasons.append("reveal_pii ignored: the query reads no PII column.")
                decision.reveal_pii = False
            elif not self.role.can_request_clear_pii:
                decision.reasons.append(
                    f"Role '{self.role.name}' may not see clear customer identifiers; "
                    "customers are shown as stable tokens (cust_...)."
                )
                return decision
            else:
                decision.approval_kinds.append("pii")
                cols = ", ".join(f"{t}.{c}" for t, c in validation.pii_columns)
                decision.reasons.append(f"Clear identifiers requested for PII columns: {cols}.")
        decision.decision = NEEDS_APPROVAL if decision.approval_kinds else ALLOW
        return decision

    def execute(
        self, decision: GuardDecision, approved: bool = False, max_rows: int | None = -1
    ) -> ExecutionResult:
        if decision.safe_sql is None or decision.decision == REJECT:
            raise ValueError("refusing to execute a rejected query")
        if decision.decision == NEEDS_APPROVAL and not approved:
            raise PermissionError("this query needs a recorded human approval")
        replica = self.role.replica
        if decision.reveal_pii and "pii" in decision.approval_kinds:
            replica = self.role.clear_replica  # only reachable after an approval
        return self.pool.get(replica).execute(decision.safe_sql, max_rows=max_rows)
