"""Where does the agent gain or lose against the single-shot baseline on BIRD? (paired subset)

Reads reports/bench/bird/<run>/{single_shot,agent}.jsonl and splits the paired questions by
outcome and by the baseline's failure type (no SQL / SQL error / wrong result).
Output: reports/bench/bird/<run>/analysis.json and analysis.md
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path) -> dict[str, dict]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["item_id"]] = r
    return out


DIFFICULTY = ["simple", "moderate", "challenging"]


def tok(recs: dict[str, dict], ids: list[str]) -> float:
    if not ids:
        return 0.0
    return sum(
        recs[q].get("prompt_tokens", 0) + recs[q].get("completion_tokens", 0) for q in ids
    ) / len(ids)


def mean(recs: dict[str, dict], ids: list[str], key: str) -> float:
    return round(sum(recs[q].get(key, 0) for q in ids) / len(ids), 2) if ids else 0.0


def pct(recs: dict[str, dict], ids: list[str]) -> dict:
    lat = sorted(recs[q].get("latency_s", 0.0) for q in ids)
    if not lat:
        return {}
    return {"p50": lat[len(lat) // 2], "p95": lat[int(0.95 * (len(lat) - 1))]}


def failure(r: dict) -> str:
    why = r.get("why") or ""
    if r.get("correct"):
        return "correct"
    if why.startswith("no sql"):
        return "no SQL"
    if why.startswith("pred failed"):
        return "SQL error or timeout"
    if r.get("sql_rejected"):
        return "rejected by the validator"
    return "wrong result"


def main(run: str = "bird-qwen35-9b") -> int:
    d = ROOT / "reports" / "bench" / "bird" / run
    ss, ag = load(d / "single_shot.jsonl"), load(d / "agent.jsonl")
    common = sorted(set(ss) & set(ag), key=int)
    quad = Counter()
    gain_from = Counter()
    loss_to = Counter()
    by_diff: dict[str, Counter] = {}
    for q in common:
        a, b = ag[q]["correct"], ss[q]["correct"]
        quad[(a, b)] += 1
        by_diff.setdefault(ag[q]["difficulty"], Counter())[(a, b)] += 1
        if a and not b:
            gain_from[failure(ss[q])] += 1
        if b and not a:
            loss_to[failure(ag[q]) + f" (status {ag[q].get('status')})"] += 1
    statuses = Counter(ag[q].get("status") for q in common)
    stopped = [q for q in common if ag[q].get("status") == "budget_exceeded"]
    report = {
        "paired": len(common),
        "both_correct": quad[(True, True)],
        "agent_only": quad[(True, False)],
        "single_shot_only": quad[(False, True)],
        "both_wrong": quad[(False, False)],
        "agent_gains_where_single_shot_had": dict(gain_from),
        "agent_losses_with": dict(loss_to),
        "agent_statuses": dict(statuses),
        "by_difficulty": {
            k: {
                "n": sum(v.values()),
                "agent_ex": round((v[(True, True)] + v[(True, False)]) / sum(v.values()), 4),
                "single_shot_ex": round((v[(True, True)] + v[(False, True)]) / sum(v.values()), 4),
                "agent_only": v[(True, False)],
                "single_shot_only": v[(False, True)],
            }
            for k, v in sorted(by_diff.items(), key=lambda kv: DIFFICULTY.index(kv[0]))
        },
        "budget_stops": {
            "n": len(stopped),
            "share": round(len(stopped) / len(common), 4) if common else None,
            "still_correct": sum(1 for q in stopped if ag[q]["correct"]),
        },
        "single_shot_ex_full_set": round(sum(r["correct"] for r in ss.values()) / len(ss), 4),
        "single_shot_ex_this_subset": round(sum(ss[q]["correct"] for q in common) / len(common), 4)
        if common
        else None,
        "tokens_per_question": {"agent": tok(ag, common), "single_shot": tok(ss, common)},
        "model_calls_per_question": {
            "agent": mean(ag, common, "llm_calls"),
            "single_shot": mean(ss, common, "llm_calls"),
        },
        "agent_queries_per_question": mean(ag, common, "queries_run"),
        "latency_s": {"agent": pct(ag, common), "single_shot": pct(ss, common)},
    }
    (d / "analysis.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    lines = [
        f"# BIRD: agent vs single-shot, paired ({len(common)} questions)",
        "",
        "| | Single-shot correct | Single-shot wrong |",
        "|---|---|---|",
        f"| Agent correct | {quad[(True, True)]} | {quad[(True, False)]} |",
        f"| Agent wrong | {quad[(False, True)]} | {quad[(False, False)]} |",
        "",
        "Where the agent won, the single-shot answer was: "
        + ", ".join(f"{k} {v}" for k, v in gain_from.most_common())
        + ".",
        "",
        "Where the agent lost: " + ", ".join(f"{k} {v}" for k, v in loss_to.most_common()) + ".",
        "",
        "Agent final statuses: " + ", ".join(f"{k} {v}" for k, v in statuses.most_common()) + ".",
        f"Model-call budget reached on {report['budget_stops']['n']} questions "
        f"({100 * (report['budget_stops']['share'] or 0):.1f}%); the last executed query was "
        f"scored and was correct on {report['budget_stops']['still_correct']} of them.",
        "",
        "| Difficulty | n | Agent EX | Single-shot EX | Agent only | Single-shot only |",
        "|---|---|---|---|---|---|",
    ]
    for k, v in report["by_difficulty"].items():
        lines.append(
            f"| {k} | {v['n']} | {100 * v['agent_ex']:.1f}% | {100 * v['single_shot_ex']:.1f}% "
            f"| {v['agent_only']} | {v['single_shot_only']} |"
        )
    lines += [
        "",
        f"Single-shot EX on this subset {100 * report['single_shot_ex_this_subset']:.1f}% vs "
        f"{100 * report['single_shot_ex_full_set']:.1f}% on all {len(ss)} questions. Tokens per "
        f"question: agent {report['tokens_per_question']['agent']:,.0f}, single-shot "
        f"{report['tokens_per_question']['single_shot']:,.0f}. Model calls per question: agent "
        f"{report['model_calls_per_question']['agent']}, single-shot "
        f"{report['model_calls_per_question']['single_shot']}; the agent ran "
        f"{report['agent_queries_per_question']} queries per question. Latency p50 / p95: agent "
        f"{report['latency_s']['agent'].get('p50')} s / {report['latency_s']['agent'].get('p95')} s, "
        f"single-shot {report['latency_s']['single_shot'].get('p50')} s / "
        f"{report['latency_s']['single_shot'].get('p95')} s (wall clock per question; concurrency and model-server load varied between runs).",
    ]
    (d / "analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
