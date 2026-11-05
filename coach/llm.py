"""Thin, dependency-free LLM provider adapters (raw HTTPS via httpx).

Conversation history is kept in a neutral format so the agent loop does not care which provider runs:
    {"role": "user", "text": str}
    {"role": "assistant", "text": str, "tool_calls": [ToolCall, ...]}
    {"role": "tool", "results": [{"id": str, "name": str, "content": str}, ...]}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .config import Settings


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON schema


class Provider(Protocol):
    name: str
    model: str

    def complete(self, system: str, history: list[dict], tools: list[ToolSpec]) -> LLMResponse: ...


def _raise_for(resp: httpx.Response, who: str) -> None:
    if resp.status_code == 200:
        return
    try:
        detail = resp.json().get("error", {})
        msg = detail.get("message") if isinstance(detail, dict) else str(detail)
    except ValueError:
        msg = resp.text[:300]
    raise LLMError(f"{who} API error {resp.status_code}: {msg}")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, max_tokens: int, timeout: float):
        self.model, self.max_tokens = model, max_tokens
        self.http = httpx.Client(
            base_url="https://api.anthropic.com",
            timeout=timeout,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        )

    @staticmethod
    def to_messages(history: list[dict]) -> list[dict]:
        msgs: list[dict] = []
        for h in history:
            if h["role"] == "user":
                msgs.append({"role": "user", "content": h["text"]})
            elif h["role"] == "assistant":
                content: list[dict] = []
                if h.get("text"):
                    content.append({"type": "text", "text": h["text"]})
                for tc in h.get("tool_calls", []):
                    content.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
                msgs.append({"role": "assistant", "content": content or [{"type": "text", "text": "(no text)"}]})
            elif h["role"] == "tool":
                msgs.append(
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]} for r in h["results"]],
                    }
                )
        return msgs

    def complete(self, system: str, history: list[dict], tools: list[ToolSpec]) -> LLMResponse:
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            # The system prompt carries the (large, stable) game context: cache it across chat turns.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": self.to_messages(history),
        }
        if tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        try:
            resp = self.http.post("/v1/messages", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"Anthropic request failed: {exc}") from exc
        _raise_for(resp, "Anthropic")
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        calls = [ToolCall(b["id"], b["name"], b.get("input") or {}) for b in data.get("content", []) if b.get("type") == "tool_use"]
        usage = data.get("usage", {})
        return LLMResponse(
            text=text,
            tool_calls=calls,
            input_tokens=usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str, max_tokens: int, timeout: float, base_url: str = "https://api.openai.com/v1"):
        self.model, self.max_tokens = model, max_tokens
        self.http = httpx.Client(
            base_url=base_url, timeout=timeout, headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
        )

    @staticmethod
    def to_messages(system: str, history: list[dict]) -> list[dict]:
        msgs: list[dict] = [{"role": "system", "content": system}]
        for h in history:
            if h["role"] == "user":
                msgs.append({"role": "user", "content": h["text"]})
            elif h["role"] == "assistant":
                m: dict = {"role": "assistant", "content": h.get("text") or None}
                if h.get("tool_calls"):
                    m["tool_calls"] = [
                        {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                        for tc in h["tool_calls"]
                    ]
                msgs.append(m)
            elif h["role"] == "tool":
                for r in h["results"]:
                    msgs.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
        return msgs

    def complete(self, system: str, history: list[dict], tools: list[ToolSpec]) -> LLMResponse:
        body: dict = {"model": self.model, "messages": self.to_messages(system, history), "max_completion_tokens": self.max_tokens * 4}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]
        try:
            resp = self.http.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"OpenAI request failed: {exc}") from exc
        _raise_for(resp, "OpenAI")
        data = resp.json()
        msg = data["choices"][0]["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append(ToolCall(tc["id"], tc["function"]["name"], args))
        usage = data.get("usage", {})
        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=calls,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )


class ScriptedProvider:
    """Deterministic provider for tests and the eval smoke-run: replays canned responses in order."""

    name = "scripted"
    model = "scripted"

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, system: str, history: list[dict], tools: list[ToolSpec]) -> LLMResponse:
        self.calls.append({"system": system, "history": [dict(h) for h in history], "tools": [t.name for t in tools]})
        if not self.responses:
            return LLMResponse(text="(no more scripted responses)")
        return self.responses.pop(0)


def make_provider(settings: Settings, override: str | None = None) -> Provider | None:
    choice = (override or settings.llm_provider or "auto").lower()
    if choice == "none":
        return None
    if choice in ("auto", "anthropic") and settings.anthropic_api_key:
        return AnthropicProvider(settings.anthropic_api_key, settings.anthropic_model, settings.llm_max_tokens, settings.llm_timeout_s)
    if choice in ("auto", "openai") and settings.openai_api_key:
        return OpenAIProvider(settings.openai_api_key, settings.openai_model, settings.llm_max_tokens, settings.llm_timeout_s)
    return None
