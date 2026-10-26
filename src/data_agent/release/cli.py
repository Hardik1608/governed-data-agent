"""`data-agent gate ...` subcommands (registered by data_agent.cli)."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def cmd_register(args) -> int:
    from data_agent.release.gate import register

    out = register(Path(args.config), Path(args.eval), args.tracking_uri)
    print(json.dumps(out, indent=1, default=str))
    return 0


def cmd_refresh(args) -> int:
    from data_agent.release.gate import refresh

    print(
        json.dumps(
            refresh(str(args.version), Path(args.eval), args.tracking_uri), indent=1, default=str
        )
    )
    return 0


def cmd_decide(args) -> int:
    from data_agent.config import Settings
    from data_agent.release.gate import decide_version

    s = Settings()
    result = decide_version(
        str(args.version),
        Path(args.rules or s.home / "configs" / "release_gate.yaml"),
        Path(args.out or s.home / "reports" / "gate"),
        args.tracking_uri,
        apply=not args.dry_run,
    )
    print(
        json.dumps(
            {k: result[k] for k in ("decision", "reason", "candidate_version", "champion_version")},
            indent=1,
        )
    )
    return 0 if result["decision"] != "reject" else 3


def cmd_check_smoke(args) -> int:
    from data_agent.release.gate import check_smoke

    ok, detail = check_smoke(Path(args.summary))
    print(("PASS " if ok else "FAIL ") + detail)
    return 0 if ok else 1


def register(sub) -> None:
    p = sub.add_parser("gate", help="release gate: register agent configs, champion / challenger")
    gsub = p.add_subparsers(dest="gate_command", required=True)
    r = gsub.add_parser("register", help="log a config + its eval in MLflow and register a version")
    r.add_argument("--config", required=True)
    r.add_argument("--eval", required=True, help="eval run directory (with summary.json)")
    r.add_argument("--tracking-uri")
    r.set_defaults(func=cmd_register)
    f = gsub.add_parser("refresh", help="re-log a regraded eval summary to a registered version")
    f.add_argument("--version", required=True)
    f.add_argument("--eval", required=True)
    f.add_argument("--tracking-uri")
    f.set_defaults(func=cmd_refresh)
    d = gsub.add_parser("decide", help="compare a registered version with the champion")
    d.add_argument("--version", required=True)
    d.add_argument("--rules")
    d.add_argument("--out")
    d.add_argument("--tracking-uri")
    d.add_argument("--dry-run", action="store_true")
    d.set_defaults(func=cmd_decide)
    c = gsub.add_parser("check-smoke", help="CI: the oracle self-test must be perfect")
    c.add_argument("--summary", required=True)
    c.set_defaults(func=cmd_check_smoke)
    _ = sys  # keep the import used for future output redirection
