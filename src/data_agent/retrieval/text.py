"""Tokenisation shared by the BM25 index and the query side.

Ported from text-to-sql-llm-platform v1 (`src/retrieval/text.py`, MIT, same author), with
business-vocabulary additions (synonyms) for the sales catalog.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[A-Za-z]+|\d+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

STOPWORDS = frozenset(
    """
    a an the of in on at for to from by with and or not no is are was were be been being
    what which who whom whose when where how many much all each every any some that this
    these those there their them they it its do does did have has had give show list find
    return tell me us we our please as than more most less least also both either per into
    out up down over under about between among other only same such very can could would
    should will shall may might must if then else so just one ones s whats
    """.split()  # noqa: SIM905 (one readable block beats a 100-line list)
)

# Business words -> catalog words. Applied to queries only, so documents keep their wording.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "sale": ("revenue",),
    "revenue": ("sale",),
    "turnover": ("revenue",),
    "client": ("customer",),
    "buyer": ("customer",),
    "shopper": ("customer",),
    "item": ("product",),
    "sku": ("product", "stock", "code"),
    "nation": ("country",),
    "market": ("country", "region"),
    "refund": ("cancellation",),
    "return": ("cancellation",),
    "cancel": ("cancellation",),
    "basket": ("order", "invoice"),
    "order": ("invoice",),
    "invoice": ("order",),
    "tier": ("loyalty",),
    "segment": ("loyalty", "tier"),
    "month": ("monthly",),
    "churn": ("retention",),
    "cohort": ("retention",),
}


def stem(token: str) -> str:
    """Tiny plural stemmer: singers->singer, countries->country, classes->class."""
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("sses"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def split_identifier(name: str) -> list[str]:
    """net_merchandise_revenue -> [net, merchandise, revenue]; firstInvoice -> [first, invoice]."""
    words = []
    for part in re.split(r"[^A-Za-z0-9]+", name):
        if part:
            words.extend(_CAMEL_BOUNDARY.sub(" ", part).split())
    return [stem(w.lower()) for w in words if w]


def tokenize(text: str, remove_stopwords: bool = True) -> list[str]:
    out = []
    for raw in _TOKEN.findall(_CAMEL_BOUNDARY.sub(" ", text or "")):
        tok = raw.lower()
        if remove_stopwords and tok in STOPWORDS:
            continue
        out.append(stem(tok))
    return out


def expand_query(tokens: list[str]) -> list[str]:
    out = list(tokens)
    for tok in tokens:
        out.extend(SYNONYMS.get(tok, ()))
    return out
