"""Governed Data Agent: a LangGraph agent over a dbt/DuckDB sales warehouse.

Guarantees, each enforced in code and tested:
  - read-only by construction (validator + read-only DuckDB replica, external access off);
  - customer identifiers never leave the warehouse unmasked (tokenised replicas per role);
  - every number in an answer traceable to the SQL that produced it (query ids + check node);
  - cost and latency budgets per question (EXPLAIN estimate, timeouts, token and step caps);
  - a release gate on a golden-set eval (pass^k, unsafe actions, cost per success).
"""

__version__ = "0.1.0"
