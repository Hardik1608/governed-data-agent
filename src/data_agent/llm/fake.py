"""Deterministic stand-ins for a model server (tests, CI smoke eval, harness self-checks).

`ScriptedLLM` replays a script per purpose ("plan", "agent", "judge"). A step is a
ChatResponse, a dict {"content": ..., "tool_calls": [(name, args), ...]}, or a callable
(messages, tools) -> step. Token counts are deterministic functions of the text so budgets
and cost accounting can be tested.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from data_agent.llm.client import ChatResponse, ToolCall

Step = ChatResponse | dict | Callable[[list[dict], list[dict] | None], Any]


def _count(messages: list[dict]) -> int:
    return sum(len(str(msg.get("content") or "")) for msg in messages) // 4 + 1


def response(
    content: str = "", tool_calls: list[tuple[str, dict]] | None = None, **kw
) -> ChatResponse:
    calls = [
        ToolCall(id=f"call_{i}_{name}", name=name, arguments=args, raw_arguments=json.dumps(args))
        for i, (name, args) in enumerate(tool_calls or [])
    ]
    return ChatResponse(
        content=content, tool_calls=calls, model=kw.get("model", "fake"), finish_reason="stop"
    )


class ScriptedLLM:
    def __init__(
        self,
        agent: list[Step] | None = None,
        plan: list[Step] | None = None,
        judge: list[Step] | None = None,
        model: str = "fake-scripted",
        default_plan: dict | None = None,
    ):
        self.model = model
        self.scripts: dict[str, list[Step]] = {
            "agent": list(agent or []),
            "plan": list(plan or []),
            "judge": list(judge or []),
        }
        self.default_plan = default_plan or {"action": "answer", "reason": "scripted", "steps": []}
        self.calls: list[dict] = []

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
        self.calls.append({"purpose": purpose, "messages": messages, "tools": tools})
        script = self.scripts.setdefault(purpose, [])
        if not script:
            if purpose == "plan":
                step: Any = {"content": json.dumps(self.default_plan)}
            else:
                raise AssertionError(f"ScriptedLLM: no scripted step left for purpose '{purpose}'")
        else:
            step = script.pop(0)
        while callable(step) and not isinstance(step, ChatResponse):
            step = step(messages, tools)
        if isinstance(step, dict):
            step = response(step.get("content", ""), step.get("tool_calls"))
        if not isinstance(step, ChatResponse):
            raise TypeError(f"bad scripted step: {step!r}")
        step.prompt_tokens = step.prompt_tokens or _count(messages)
        step.completion_tokens = step.completion_tokens or max(
            1, len(step.content) // 4 + 5 * len(step.tool_calls)
        )
        step.model = step.model or self.model
        return step


def last_tool_result(messages: list[dict], name: str | None = None) -> dict:
    """Parsed content of the most recent tool message (optionally of a given tool)."""
    for msg in reversed(messages):
        if msg.get("role") == "tool" and (name is None or msg.get("name") == name):
            return json.loads(msg["content"])
    raise LookupError(f"no tool result for {name or 'any tool'}")
