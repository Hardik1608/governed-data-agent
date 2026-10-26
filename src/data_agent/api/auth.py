"""Bearer tokens -> (user, role). Shared by the HTTP API and the MCP Streamable HTTP server.

The tokens file stores SHA-256 hashes of tokens, never the tokens themselves:
    {"tokens": [{"sha256": "<hex>", "user": "alice", "role": "analyst"}, ...]}
`data-agent` reads it from DATA_AGENT_TOKENS_FILE. `make_token()` prints a new token and its
file entry. In production this is replaced by an identity provider issuing JWTs (verified
against the IdP's JWKS); the full OAuth 2.1 authorisation server is out of scope.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Principal:
    user: str
    role: str


class TokenStore:
    def __init__(self, entries: dict[str, Principal]):
        self._by_hash = entries

    @classmethod
    def load(cls, path: Path | None) -> TokenStore:
        if path is None or not Path(path).exists():
            return cls({})
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls({e["sha256"]: Principal(e["user"], e["role"]) for e in data.get("tokens", [])})

    def __len__(self) -> int:
        return len(self._by_hash)

    def verify(self, token: str | None) -> Principal | None:
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        for known, principal in self._by_hash.items():
            if hmac.compare_digest(known, digest):
                return principal
        return None


def make_token(user: str, role: str) -> tuple[str, dict]:
    token = "gda_" + secrets.token_urlsafe(32)
    return token, {"sha256": hashlib.sha256(token.encode()).hexdigest(), "user": user, "role": role}


class RateLimiter:
    """Token bucket per user: `per_minute` requests, refilled continuously."""

    def __init__(self, per_minute: int):
        self.capacity = max(1, per_minute)
        self.rate = self.capacity / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True
