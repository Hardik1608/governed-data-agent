"""Number provenance: every number in an answer must come from a query result.

The model is not trusted to cite correctly. The `check` node extracts every number the
answer states, and looks for a result cell (in any query this conversation ran) that the
number is a faithful rendering of, at the precision it is displayed:
  "£1,427,124.54" == 1427124.540 ;  "£1.43M" ~ 1427124.54 ;  "20.2%" ~ 0.2017
Matched numbers get a provenance record (query id, row, column, SQL). Unmatched numbers are
sent back to the model ("compute it in SQL or remove it"); the LLM never does arithmetic
that ends up in an answer unverified.

Numbers that are not data are skipped: years (1990-2035), dates, numbers the user wrote in
the question, query ids (q1), small integers (0-12) used as counts in prose ("top 5"), and
anything inside code blocks.
"""

from __future__ import annotations

import contextlib
import math
import re
from dataclasses import dataclass, field
from typing import Any

_CODE = re.compile(r"```.*?```|`[^`]*`", re.DOTALL)
_DATE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{2}\b"
)
_MONTHS = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|"
    "july|august|september|october|november|december"
)
_DAY_MONTH = re.compile(
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})\b|\b(?:{_MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?!\s*,?\s*\d)",
    re.IGNORECASE,
)
_NUMBER = re.compile(
    r"(?<![\w.])(?P<sign>[-−])?\s?(?P<cur>[£$€])?\s?(?P<sign2>[-−])?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s?(?P<suffix>%|percent\b|pp\b|k\b|K\b|m\b|M\b|mn\b|bn\b|million\b|thousand\b|billion\b)?"
    r"(?![\w])",
)
_SCALE = {
    "k": 1e3, "K": 1e3, "thousand": 1e3, "m": 1e6, "M": 1e6, "mn": 1e6, "million": 1e6,
    "bn": 1e9, "billion": 1e9,
}  # fmt: skip


@dataclass
class Mention:
    text: str
    value: float  # as written, before scale
    decimals: int
    scale: float = 1.0
    percent: bool = False

    @property
    def magnitude(self) -> float:
        return self.value * self.scale


@dataclass
class Provenance:
    text: str
    query_id: str
    row: int
    column: str
    cell: Any

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "query_id": self.query_id,
            "row": self.row,
            "column": self.column,
            "value": self.cell,
        }


@dataclass
class CheckResult:
    traced: list[Provenance] = field(default_factory=list)
    untraced: list[str] = field(default_factory=list)
    numbers: int = 0

    @property
    def ok(self) -> bool:
        return not self.untraced

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "numbers": self.numbers,
            "traced": [p.to_dict() for p in self.traced],
            "untraced": self.untraced,
        }


def extract_numbers(text: str, ignore: str = "", keep_small: bool = False) -> list[Mention]:
    clean = _CODE.sub(" ", text or "")
    clean = re.sub(r"\[\s*q\d+(?:\s*,\s*q\d+)*\s*\]", " ", clean)  # citations [q1, q2]
    clean = _DATE.sub(" ", clean)
    clean = _DAY_MONTH.sub(" ", clean)
    ignored = {
        m.group("num").replace(",", "") for m in _NUMBER.finditer(_DATE.sub(" ", ignore or ""))
    }
    out = []
    for m in _NUMBER.finditer(clean):
        raw = m.group("num")
        digits = raw.replace(",", "")
        value = float(digits)
        suffix = m.group("suffix") or ""
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        is_int = decimals == 0 and "," not in raw
        if digits in ignored:
            continue
        if is_int and not suffix and not m.group("cur") and 1990 <= value <= 2035:
            continue  # a year
        if is_int and not suffix and not m.group("cur") and value <= 12 and not keep_small:
            continue  # small counts in prose ("top 5", "3 tiers")
        if m.group("sign") or m.group("sign2"):
            value = -value
        out.append(
            Mention(
                text=m.group(0).strip(),
                value=value,
                decimals=decimals,
                scale=_SCALE.get(suffix, 1.0),
                percent=suffix in ("%", "percent", "pp"),
            )
        )
    return out


def _renders(mention: Mention, cell: float) -> bool:
    """Is `mention` a faithful rendering of `cell` at the precision it is displayed?"""
    targets = [cell]
    if mention.percent:
        targets = [cell * 100.0, cell]
    for t in targets:
        for v in (t, -t):  # "cancellations of £174k" for a negative cell
            shown = v / mention.scale
            if round(shown, mention.decimals) == round(mention.value, mention.decimals):
                return True
            # half-up vs banker's rounding and float noise: allow one unit of the last digit
            unit = 10 ** (-mention.decimals)
            if abs(shown - mention.value) <= unit * 0.5 + 1e-9:
                return True
            denom = max(abs(v), 1e-12)
            if abs(abs(v) - abs(mention.magnitude)) / denom <= 0.0005:  # 0.05% float slack
                return True
    return False


def numeric_cells(queries: dict[str, dict]) -> list[tuple[str, int, str, float, Any]]:
    cells = []
    for qid, q in queries.items():
        cols = q.get("columns") or []
        for r, row in enumerate(q.get("rows") or []):
            for c, value in enumerate(row):
                if isinstance(value, bool) or value is None:
                    continue
                if isinstance(value, int | float):
                    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                        continue
                    cells.append(
                        (qid, r, cols[c] if c < len(cols) else str(c), float(value), value)
                    )
                elif isinstance(value, str):
                    with contextlib.suppress(ValueError):
                        cells.append((qid, r, cols[c], float(value.replace(",", "")), value))
    return cells


def check_answer(answer: str, queries: dict[str, dict], question: str = "") -> CheckResult:
    mentions = extract_numbers(answer, ignore=question)
    cells = numeric_cells(queries)
    result = CheckResult(numbers=len(mentions))
    for mention in mentions:
        match = next((c for c in cells if _renders(mention, c[3])), None)
        if match is None:
            result.untraced.append(mention.text)
        else:
            qid, row, col, _, raw = match
            result.traced.append(Provenance(mention.text, qid, row, col, raw))
    return result


def number_matches(
    expected: float, text: str, rel_tol: float = 0.005, abs_tol: float = 0.0, ignore: str = ""
) -> bool:
    """Grader helper: does `text` state `expected` within `rel_tol` (default 0.5%)?

    Stricter than the traceability check: "£1.4M" does not match 1,427,124.54 (1.9% off).
    A ratio may be stated as a percentage (0.2017 ~ "20.17%"), and a negative amount may be
    stated as a positive one ("cancellations of £174,091" for -174,091.36). A percentage
    shown with at least one decimal also matches a ratio that rounds to it at that precision
    ("5.3%" for 0.0526, "-0.38%" for -0.003778): for small shares a relative tolerance would
    demand more decimals than any report shows.
    """
    for mention in extract_numbers(text, ignore=ignore, keep_small=True):
        candidates = [mention.magnitude]
        if mention.percent:
            candidates.append(mention.magnitude / 100.0)
        for value in candidates:
            for target in (expected, -expected):
                if target == 0:
                    if abs(value) <= max(abs_tol, 1e-9):
                        return True
                    continue
                if abs(value - target) <= max(abs(target) * rel_tol, abs_tol):
                    return True
        if mention.percent and mention.decimals >= 1 and mention.scale == 1.0:
            half_unit = 0.5 * 10 ** (-mention.decimals)
            for target in (expected * 100.0, -expected * 100.0):
                if abs(mention.value - target) <= half_unit + 1e-9:
                    return True
    return False
