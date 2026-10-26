"""`data-agent golden ...` and `data-agent eval ...` subcommands (registered by data_agent.cli)."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _golden_dir(args) -> Path:
    from data_agent.config import Settings

    return Path(args.golden) if args.golden else Settings().home / "golden" / "v1"


def cmd_golden_freeze(args) -> int:
    from data_agent.config import Settings
    from data_agent.eval.golden import GoldenError, freeze, load_golden, write_expected
    from data_agent.governance.policy import Policy
    from data_agent.safety.executor import SafeDuckDBExecutor
    from data_agent.safety.validator import SQLValidator
    from data_agent.warehouse.replica import replica_file

    s = Settings()
    gdir = _golden_dir(args)
    items = load_golden(gdir)
    policy = Policy.load(s.policy_path)
    executors: dict[str, SafeDuckDBExecutor] = {}

    def ex(replica: str) -> SafeDuckDBExecutor:
        if replica not in executors:
            executors[replica] = SafeDuckDBExecutor(
                replica_file(s.replica_path, replica), timeout_s=120, max_rows=1000
            )
        return executors[replica]

    schema = ex("masked").tables()
    pii: dict[str, set[str]] = {}
    for col in policy.pii_columns:
        pii.setdefault(col.model, set()).add(col.column)
    validator = SQLValidator(
        {t: c for t, c in schema.items() if t in policy.role("analyst").models}, pii=pii
    )
    try:
        records = freeze(
            items, lambda r, sql: ex(r).execute(sql, max_rows=1000), validator.validate
        )
    except GoldenError as e:
        print(f"golden freeze failed: {e}", file=sys.stderr)
        return 1
    fingerprints = {}
    for replica in executors:
        meta = json.loads(
            replica_file(s.replica_path, replica).with_suffix(".json").read_text(encoding="utf-8")
        )
        fingerprints[replica] = {
            "source_fingerprint": meta["source_fingerprint"],
            "policy_fingerprint": meta["policy_fingerprint"],
            "description_overrides": len(meta.get("description_overrides", [])),
        }
    write_expected(gdir / "expected.json", records, fingerprints)
    for ex_ in executors.values():
        ex_.close()
    print(f"froze {len(records)} answerable items -> {gdir / 'expected.json'}")
    return 0


def cmd_golden_stats(args) -> int:
    from data_agent.eval.golden import CATEGORY_COUNTS, load_golden

    items = load_golden(_golden_dir(args))
    counts = {c: sum(1 for i in items if i.category == c) for c in CATEGORY_COUNTS}
    print(
        json.dumps({"items": len(items), "by_category": counts, "spec": CATEGORY_COUNTS}, indent=1)
    )
    return 0 if counts == CATEGORY_COUNTS else 1


def register(sub) -> None:
    p = sub.add_parser("golden", help="golden set: freeze expected values, stats")
    gsub = p.add_subparsers(dest="golden_command", required=True)
    f = gsub.add_parser(
        "freeze", help="run gold + alternative SQL, check agreement, write expected.json"
    )
    f.add_argument("--golden", help="golden set directory (default golden/v1)")
    f.set_defaults(func=cmd_golden_freeze)
    st = gsub.add_parser("stats", help="item counts per category")
    st.add_argument("--golden")
    st.set_defaults(func=cmd_golden_stats)

    from data_agent.eval.harness import register_eval

    register_eval(sub)
