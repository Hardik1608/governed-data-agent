"""LLM judge for narrative answers, and its calibration against human labels.

The numeric graders check *what* the answer states. For multi-step diagnostic questions the
narrative also matters ("why did December drop?"), so a judge model grades whether the
explanation is consistent with the reference facts and makes no unsupported claim.

A judge is only useful if it agrees with people, so it is calibrated:
    data-agent eval judge --run R             judge every multi-step trial (checkpointed)
    data-agent eval label-sheet --run R       write a blind sheet of 60 answers for a human
    (a person fills the human_label column with pass / fail)
    data-agent eval judge-agreement --run R   TPR = P(judge pass | human pass),
                                              TNR = P(judge fail | human fail), Wilson 95% CIs
The sheet never shows the judge's verdict (no anchoring).
"""

from __future__ import annotations

import csv
import json
import math
import random
import sys
from pathlib import Path

from data_agent.eval.harness import TrialStore

JUDGE_SYSTEM = """You grade answers written by an analytics assistant for an online retailer (UK, GBP).
You get the business question, reference facts computed by hand-written SQL on the warehouse, and the assistant's answer.
Grade the answer's narrative. Reply PASS only if all of these hold:
1. The figures it states are consistent with the reference facts (small rounding is fine).
2. Its conclusion or explanation follows from those facts and answers the question asked.
3. It makes no claim that contradicts the facts or that the facts cannot support.
Otherwise reply FAIL. Judge substance, not style or length.
Reply with one JSON object: {"verdict": "pass" or "fail", "reason": "<one sentence>"}"""


def reference_facts(expected: dict) -> str:
    lines = [f"- {v['label']}: {v['value']}" for v in expected.get("values", [])]
    lines += [
        f"- {', '.join(e['columns'])}: {e['any_of'][0]}" for e in expected.get("entities", [])
    ]
    return "\n".join(lines)


def judge_messages(question: str, facts: str, answer: str) -> list[dict]:
    user = f"Question:\n{question}\n\nReference facts:\n{facts}\n\nAssistant's answer:\n{answer}"
    return [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}]


def parse_verdict(text: str) -> tuple[str | None, str]:
    import re

    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            verdict = str(data.get("verdict", "")).strip().lower()
            if verdict in ("pass", "fail"):
                return verdict, str(data.get("reason", ""))[:300]
        except json.JSONDecodeError:
            pass
    lowered = (text or "").lower()
    if "fail" in lowered and "pass" not in lowered:
        return "fail", "unparsed"
    if "pass" in lowered and "fail" not in lowered:
        return "pass", "unparsed"
    return None, "unparseable judge output"


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def agreement(pairs: list[tuple[str, str]]) -> dict:
    """pairs of (reference, judge) labels in {pass, fail}; the reference is a person's label,
    or the numeric grader's verdict for the proxy comparison in judge.json."""
    hp = [j for h, j in pairs if h == "pass"]
    hf = [j for h, j in pairs if h == "fail"]
    tp, tn = sum(1 for j in hp if j == "pass"), sum(1 for j in hf if j == "fail")
    return {
        "n": len(pairs),
        "reference_pass": len(hp),
        "reference_fail": len(hf),
        "tpr": round(tp / len(hp), 4) if hp else None,
        "tpr_ci95": [round(x, 4) for x in wilson(tp, len(hp))] if hp else None,
        "tnr": round(tn / len(hf), 4) if hf else None,
        "tnr_ci95": [round(x, 4) for x in wilson(tn, len(hf))] if hf else None,
        "accuracy": round((tp + tn) / len(pairs), 4) if pairs else None,
    }


def _run_dir(args) -> Path:
    from data_agent.config import Settings

    return Path(args.out or Settings().home / "reports" / "eval" / args.run)


def cmd_judge(args) -> int:
    from data_agent.config import AgentConfig, Settings
    from data_agent.eval.golden import load_expected, load_golden
    from data_agent.runtime import make_llm

    settings = Settings()
    run_dir = _run_dir(args)
    gdir = Path(args.golden or settings.home / "golden" / "v1")
    all_trials = TrialStore(run_dir / "trials.jsonl").latest()
    trials = [t for t in all_trials if t["category"] in args.categories.split(",")]
    # agreement uses the run's current grades (a regrade may have changed them)
    current = {(t["item_id"], t["trial"]): t["grade"]["passed"] for t in all_trials}
    expected = load_expected(gdir / "expected.json")["items"]
    golden_items = {i.id: i for i in load_golden(gdir)}
    config = AgentConfig.load(settings.agent_config_path)
    llm = make_llm(settings, config)
    store = TrialStore(run_dir / "judge.jsonl")
    done = {(j["item_id"], j["trial"]) for j in store.load()}
    todo = [t for t in trials if (t["item_id"], t["trial"]) not in done and t.get("answer")]
    print(f"judging {len(todo)} answers ({len(done)} already judged)", file=sys.stderr)
    for t in todo:
        item = golden_items[t["item_id"]]
        msgs = judge_messages(
            item.question, reference_facts(expected.get(t["item_id"], {})), t["answer"]
        )
        resp = llm.chat(msgs, purpose="judge", temperature=0.0, max_tokens=200, json_mode=True)
        verdict, reason = parse_verdict(resp.content)
        store.append(
            {
                "item_id": t["item_id"],
                "trial": t["trial"],
                "verdict": verdict,
                "reason": reason,
                "grader_passed": t["grade"]["passed"],
                "judge_model": llm.model,
                "tokens": resp.prompt_tokens + resp.completion_tokens,
            }
        )
    judged = store.load()
    rate = (
        sum(1 for j in judged if j["verdict"] == "pass") / len(judged) if judged else float("nan")
    )
    (run_dir / "judge.json").write_text(
        json.dumps(
            {
                "judged": len(judged),
                "judge_pass_rate": round(rate, 4),
                "agreement_with_numeric_grader": agreement(
                    [
                        (
                            "pass"
                            if current.get((j["item_id"], j["trial"]), j["grader_passed"])
                            else "fail",
                            j["verdict"],
                        )
                        for j in judged
                        if j["verdict"]
                    ]
                ),
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"judge pass rate {rate:.3f} over {len(judged)} answers -> {run_dir / 'judge.json'}")
    return 0


def cmd_label_sheet(args) -> int:
    from data_agent.config import Settings
    from data_agent.eval.golden import load_expected, load_golden

    settings = Settings()
    run_dir = _run_dir(args)
    gdir = Path(args.golden or settings.home / "golden" / "v1")
    items = {i.id: i for i in load_golden(gdir)}
    expected = load_expected(gdir / "expected.json")["items"]
    trials = [
        t
        for t in TrialStore(run_dir / "trials.jsonl").latest()
        if t["category"] == "multi_step" and t.get("answer")
    ]
    rng = random.Random(args.seed)
    by_item: dict[str, list[dict]] = {}
    for t in trials:
        by_item.setdefault(t["item_id"], []).append(t)
    sample = []
    for item_id in sorted(by_item):
        group = sorted(by_item[item_id], key=lambda t: t["trial"])
        sample += rng.sample(group, min(len(group), max(1, args.n // max(1, len(by_item)))))
    sample = sample[: args.n]
    rng.shuffle(sample)
    out = run_dir / "labels"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "labelling_sheet.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "sample_id",
                "item_id",
                "trial",
                "question",
                "reference_facts",
                "answer",
                "human_label",
                "human_note",
            ]
        )
        for n, t in enumerate(sample, start=1):
            w.writerow(
                [
                    f"s{n:03d}",
                    t["item_id"],
                    t["trial"],
                    items[t["item_id"]].question,
                    reference_facts(expected.get(t["item_id"], {})),
                    t["answer"],
                    "",
                    "",
                ]
            )
    (out / "labelling_instructions.txt").write_text(
        "For each row, read the question, the reference facts (computed by hand-written SQL) and the answer.\n"
        "Write pass in human_label if the answer's figures agree with the facts (rounding is fine), its conclusion\n"
        "follows from them and answers the question, and it claims nothing the facts contradict or cannot support.\n"
        "Otherwise write fail. Judge substance, not style. Use human_note for anything unclear.\n"
        "Then run: data-agent eval judge-agreement --run <run>\n",
        encoding="utf-8",
    )
    print(f"wrote {len(sample)} rows -> {path} (fill human_label with pass/fail)")
    return 0


def cmd_judge_agreement(args) -> int:
    run_dir = _run_dir(args)
    sheet = run_dir / "labels" / "labelling_sheet.csv"
    judged = {
        (j["item_id"], int(j["trial"])): j["verdict"]
        for j in TrialStore(run_dir / "judge.jsonl").latest()
    }
    pairs = []
    with sheet.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            human = (row.get("human_label") or "").strip().lower()
            judge = judged.get((row["item_id"], int(row["trial"])))
            if human in ("pass", "fail") and judge in ("pass", "fail"):
                pairs.append((human, judge))
    if not pairs:
        print("no labelled rows yet: fill human_label in " + str(sheet))
        return 1
    result = agreement(pairs)
    (run_dir / "judge_agreement.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result, indent=1))
    return 0


def register_judge(esub) -> None:
    j = esub.add_parser("judge", help="LLM-judge the narrative answers of a run")
    j.add_argument("--run", required=True)
    j.add_argument("--categories", default="multi_step")
    j.add_argument("--golden")
    j.add_argument("--out")
    j.set_defaults(func=cmd_judge)
    s = esub.add_parser("label-sheet", help="blind labelling sheet for the judge calibration")
    s.add_argument("--run", required=True)
    s.add_argument("--n", type=int, default=60)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--golden")
    s.add_argument("--out")
    s.set_defaults(func=cmd_label_sheet)
    a = esub.add_parser("judge-agreement", help="judge TPR / TNR against the human labels")
    a.add_argument("--run", required=True)
    a.add_argument("--out")
    a.set_defaults(func=cmd_judge_agreement)
