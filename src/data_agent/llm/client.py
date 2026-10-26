"""OpenAI-compatible chat + embeddings client (vLLM, Ollama, LiteLLM, OpenAI, ...).

Every model call in the project goes through `ChatModel`, so swapping the local model for an
API model is a configuration change (base URL, model name, key). Responsibilities:
  - tool calling in the OpenAI format, with a fallback parser for models that print
    `<tool_call>{...}</tool_call>` blocks in the content instead (Hermes/Qwen style);
  - retries with exponential backoff and jitter on connection errors, timeouts, 429 and 5xx
    (2, 4, 8, 16, 32 s, then at most 60 s, within a total retry budget); never on other 4xx
    (chat completions have no side effects, so retrying is safe);
  - a per-process concurrency cap (a semaphore, default 4): the shared model server's KV
    cache is small, so every caller in the process shares the same limit;
  - token usage and latency on every response, for budgets, tracing and cost per success.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


class LLMError(Exception):
    """Generation failed. `kind`: timeout | connection | http_error | bad_response."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""
    parse_error: str | None = None

    def to_message(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.raw_arguments or json.dumps(self.arguments),
            },
        }


@dataclass
class ChatResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    finish_reason: str | None = None
    latency_ms: float = 0.0
    usage_reported: bool = True

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def assistant_message(self) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [tc.to_message() for tc in self.tool_calls]
        return msg


class ChatModel(Protocol):
    model: str

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        purpose: str = "agent",
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ChatResponse: ...


_TOOL_CALL_BLOCK = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def _parse_arguments(raw: Any) -> tuple[dict, str, str | None]:
    if isinstance(raw, dict):
        return raw, json.dumps(raw), None
    text = raw if isinstance(raw, str) else json.dumps(raw)
    if not text.strip():
        return {}, text, None
    try:
        value = json.loads(text)
    except json.JSONDecodeError as e:
        return {}, text, f"arguments are not valid JSON: {e.msg}"
    if not isinstance(value, dict):
        return {}, text, "arguments must be a JSON object"
    return value, text, None


def parse_tool_calls(message: dict) -> tuple[str, list[ToolCall]]:
    """Tool calls from an OpenAI message, falling back to <tool_call> blocks in content."""
    content = message.get("content") or ""
    if isinstance(content, list):  # content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    content = _THINK.sub("", content).strip()
    calls = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args, raw, err = _parse_arguments(fn.get("arguments", "{}"))
        calls.append(
            ToolCall(
                id=tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                name=fn.get("name", ""),
                arguments=args,
                raw_arguments=raw,
                parse_error=err,
            )
        )
    if not calls and "<tool_call>" in content:
        for block in _TOOL_CALL_BLOCK.findall(content):
            try:
                obj = json.loads(block)
            except json.JSONDecodeError:
                continue
            args, raw, err = _parse_arguments(obj.get("arguments", obj.get("parameters", {})))
            calls.append(
                ToolCall(f"call_{uuid.uuid4().hex[:12]}", obj.get("name", ""), args, raw, err)
            )
        if calls:
            content = _TOOL_CALL_BLOCK.sub("", content).strip()
    return content, calls


_SEMAPHORES: dict[str, threading.BoundedSemaphore] = {}
_SEMAPHORES_LOCK = threading.Lock()


def _semaphore(base_url: str, limit: int) -> threading.BoundedSemaphore:
    with _SEMAPHORES_LOCK:
        key = f"{base_url}|{limit}"
        if key not in _SEMAPHORES:
            _SEMAPHORES[key] = threading.BoundedSemaphore(limit)
        return _SEMAPHORES[key]


class OpenAICompatibleClient:
    """Synchronous client; the agent graph and the eval harness run it in worker threads."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "EMPTY",
        timeout_s: float = 120.0,
        max_retries: int = 6,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        extra_body: dict | None = None,
        transport: httpx.BaseTransport | None = None,
        max_concurrency: int = 4,
        retry_budget_s: float = 180.0,
        backoff_base_s: float = 2.0,
        backoff_cap_s: float = 60.0,
        sampling: dict | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_retries = max_retries
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.extra_body = dict(extra_body or {})
        self.sampling = {k: v for k, v in (sampling or {}).items() if v is not None}
        self.retry_budget_s = retry_budget_s
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self._slots = _semaphore(self.base_url, max_concurrency)
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ chat
    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        purpose: str = "agent",
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            **self.sampling,
            **self.extra_body,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        start = time.perf_counter()
        data = self._post("/chat/completions", payload)
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("bad_response", f"unexpected response shape: {e!r}") from e
        content, calls = parse_tool_calls(message)
        usage = data.get("usage") or {}
        return ChatResponse(
            content=content,
            tool_calls=calls,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            model=data.get("model", self.model),
            finish_reason=choice.get("finish_reason"),
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
            usage_reported=bool(usage),
        )

    # ------------------------------------------------------------------ embeddings
    def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        data = self._post("/embeddings", {"model": model or self.model, "input": texts})
        try:
            items = sorted(data["data"], key=lambda d: d.get("index", 0))
            return [list(map(float, d["embedding"])) for d in items]
        except (KeyError, TypeError) as e:
            raise LLMError("bad_response", f"unexpected embeddings response: {e!r}") from e

    def health(self) -> bool:
        try:
            self._request("GET", "/models")
            return True
        except LLMError:
            return False

    # ------------------------------------------------------------------ transport
    def _post(self, path: str, payload: dict) -> dict:
        attempt, started = 0, time.monotonic()
        while True:
            try:
                with self._slots:
                    response = self._request("POST", path, json=payload)
                return response.json()
            except LLMError as e:
                retryable = e.kind in ("connection", "timeout") or (
                    e.kind == "http_error" and str(e).startswith(("HTTP 5", "HTTP 429"))
                )
                delay = min(self.backoff_cap_s, self.backoff_base_s * 2**attempt)
                delay *= 0.75 + 0.5 * random.random()  # noqa: S311 - jitter
                spent = time.monotonic() - started
                if (
                    not retryable
                    or attempt >= self.max_retries
                    or spent + delay > self.retry_budget_s
                ):
                    raise
                attempt += 1
                time.sleep(delay)
            except ValueError as e:  # invalid JSON body
                raise LLMError("bad_response", f"response is not JSON: {e}") from e

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as e:
            raise LLMError("timeout", f"LLM request timed out: {e!r}") from e
        except httpx.TransportError as e:
            raise LLMError("connection", f"cannot reach {self.base_url}: {e!r}") from e
        if response.status_code >= 400:
            raise LLMError("http_error", f"HTTP {response.status_code}: {response.text[:300]}")
        return response
