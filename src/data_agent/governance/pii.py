"""Egress filter: redact raw customer identifiers from anything the agent returns.

The masked replica holds no raw identifier, so the agent cannot read one. It can still
*write* one: echoing a number from the question, or hallucinating digits that happen to be
a real id. Before an answer leaves the service, every candidate token (5-digit numbers for
this dataset) is hashed with the PII secret and looked up in a set of keyed hashes of the
real identifiers, written by `data-agent build-replicas`. Matches are redacted unless the
user supplied that identifier in the question, or the role is allowed clear identifiers and
a human approved it. The fingerprint file holds keyed hashes only (no raw ids).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import duckdb

CANDIDATE = re.compile(r"(?<![\w.,])(\d{5})(?:\.0)?(?![\w.]|,\d)")
REDACTED = "[REDACTED-ID]"


def _fp(secret: str, value: str) -> str:
    return hashlib.sha256(f"{secret}:detect:{value}".encode()).hexdigest()[:20]


def write_fingerprints(raw_warehouse: Path, out: Path, secret: str) -> int:
    con = duckdb.connect(str(raw_warehouse), read_only=True)
    try:
        ids = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT customer_id FROM gold.dim_customer "
                "WHERE customer_id IS NOT NULL AND customer_id <> '-1'"
            ).fetchall()
        ]
    finally:
        con.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"kind": "customer_id", "fingerprints": sorted(_fp(secret, i) for i in ids)}),
        encoding="utf-8",
    )
    tmp.replace(out)
    return len(ids)


@dataclass
class RedactionResult:
    text: str
    redacted: list[str]


class PiiRedactor:
    def __init__(self, fingerprints: set[str], secret: str):
        self._fps = fingerprints
        self._secret = secret

    @classmethod
    def load(cls, path: Path, secret: str) -> PiiRedactor:
        if not Path(path).exists():
            return cls(set(), secret)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(set(data["fingerprints"]), secret)

    @property
    def active(self) -> bool:
        return bool(self._fps)

    def is_identifier(self, value: str) -> bool:
        return _fp(self._secret, value) in self._fps

    def find(self, text: str) -> list[str]:
        return [
            m.group(1) for m in CANDIDATE.finditer(text or "") if self.is_identifier(m.group(1))
        ]

    def redact(self, text: str, allow: str = "") -> RedactionResult:
        allowed = set(CANDIDATE.findall(allow or ""))
        hits: list[str] = []

        def _sub(m: re.Match) -> str:
            value = m.group(1)
            if value in allowed or not self.is_identifier(value):
                return m.group(0)
            hits.append(value)
            return REDACTED

        return RedactionResult(CANDIDATE.sub(_sub, text or ""), hits)
