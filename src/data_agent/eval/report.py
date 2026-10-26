"""Write summary.json and summary.md for an eval run."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from data_agent.config import AgentConfig
from data_agent.eval.golden import GoldenItem
from data_agent.eval.metrics import summarize


def _pct(x) -> str:
    return "n/a" if x is None or x != x else f"{100 * x:.1f}%"


def write_reports(
    out_dir: Path, trials: list[dict], items: list[GoldenItem], meta: dict, config: AgentConfig
) -> dict:
    out_dir = Path(out_dir)
    k = meta["eval"]["k"]
    item_ids = {i.id for i in items}
    trials = [t for t in trials if t["item_id"] in item_ids]
    metrics = summarize(
        trials, k, config.model.eur_per_mtok_input, config.model.eur_per_mtok_output
    )
    per_item: dict[str, dict] = defaultdict(lambda: {"passes": 0, "trials": 0, "reasons": []})
    for t in trials:
        rec = per_item[t["item_id"]]
        rec["category"] = t["category"]
        rec["trials"] += 1
        rec["passes"] += int(t["grade"]["passed"])
        if not t["grade"]["passed"]:
            rec["reasons"] += t["grade"]["reasons"][:2]
    summary = {"meta": meta, "metrics": metrics, "items": dict(sorted(per_item.items()))}
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=1, default=str) + "\n", encoding="utf-8"
    )
    (out_dir / "summary.md").write_text(_markdown(summary, k), encoding="utf-8")
    return summary


def _markdown(summary: dict, k: int) -> str:
    m, meta = summary["metrics"], summary["meta"]
    agent = meta["agent"]
    lines = [
        f"# Eval run `{meta['run']}`",
        "",
        f"- Agent: `{agent['name']}` v{agent['version']} (config fingerprint `{agent['fingerprint']}`), model `{agent['model']}`, "
        f"temperature {agent.get('temperature')}, top_p {agent.get('top_p')}, top_k {agent.get('top_k')}",
        f"- Golden set: {meta['eval']['items']} items x k={k} trials = {m['trials']} trials; approver policy: {meta['eval']['approver']}; "
        f"model calls: {meta['eval']['llm']}",
        f"- Expected values: {json.dumps(meta['golden'], default=str)[:300]}",
        f"- Started {meta['started_at']}, finished {meta['finished_at']}",
        "",
        "## Headline",
        "",
        "| Metric | Value |",
        "|---|---|",
    ]
    ci1 = m["pass_hat_k_ci95"].get("pass^1", [None, None])
    cik = m["pass_hat_k_ci95"].get(f"pass^{k}", [None, None])
    lines += [
        f"| pass^1 (95% CI over tasks) | {_pct(m['pass_hat_k']['pass^1'])} [{_pct(ci1[0])}, {_pct(ci1[1])}] |",
        *(
            [
                f"| pass^{k} (95% CI) | {_pct(m['pass_hat_k'][f'pass^{k}'])} [{_pct(cik[0])}, {_pct(cik[1])}] |"
            ]
            if k > 1
            else []
        ),
        f"| Unsafe actions that reached the database | {m['safety']['unsafe_executed']} |",
        f"| Raw customer identifiers in answers | {m['safety']['pii_leaks']} |",
        f"| Unsafe SQL attempts (blocked by the validator) | {m['safety']['unsafe_attempts']} in {m['safety']['trials_with_unsafe_attempts']} trials |",
        f"| Prompt-injection attack success | {_pct(m['safety']['injection_attack_success_rate'])} ({m['safety']['injection_followed']}/{m['safety']['injection_trials']} trials) |",
        f"| Clarification precision / recall | {_pct(m['clarification']['precision'])} / {_pct(m['clarification']['recall'])} |",
        f"| Refusal precision / recall | {_pct(m['refusal']['precision'])} / {_pct(m['refusal']['recall'])} |",
        f"| Tokens per successful trial | {m['cost']['tokens_per_success']:,} |"
        if m["cost"]["tokens_per_success"]
        else "| Tokens per successful trial | n/a |",
        f"| Model calls per trial | {m['cost']['llm_calls_per_trial']} |",
        f"| Latency per trial p50 / p95 | {m['latency_s']['p50']} s / {m['latency_s']['p95']} s |",
        f"| Trials whose answer had untraced numbers | {m['untraced_number_trials']} |",
        "",
        f"pass^j for j = 1..{k}: "
        + ", ".join(f"{key} {_pct(v)}" for key, v in m["pass_hat_k"].items()),
        "",
        "## By category",
        "",
        f"| Category | Tasks | Trials | pass^1 | pass^{k} |"
        if k > 1
        else "| Category | Tasks | Trials | pass^1 |",
        "|---|---|---|---|---|" if k > 1 else "|---|---|---|---|",
    ]
    for cat, c in m["by_category"].items():
        extra = f" {_pct(c[f'pass^{k}'])} |" if k > 1 else ""
        lines.append(f"| {cat} | {c['tasks']} | {c['trials']} | {_pct(c['pass^1'])} |{extra}")
    lines += ["", "## Final statuses", "", "| Status | Trials |", "|---|---|"]
    lines += [f"| {s} | {n} |" for s, n in m["statuses"].items()]
    failed = [(i, r) for i, r in summary["items"].items() if r["passes"] < r["trials"]]
    lines += [
        "",
        f"## Tasks with at least one failed trial ({len(failed)})",
        "",
        "| Task | Passed | First reason |",
        "|---|---|---|",
    ]
    for item_id, r in failed:
        reason = (r["reasons"][0] if r["reasons"] else "").replace("|", "/")[:140]
        lines.append(f"| {item_id} | {r['passes']}/{r['trials']} | {reason} |")
    lines.append("")
    return "\n".join(lines)
