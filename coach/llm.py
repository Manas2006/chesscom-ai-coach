"""Thin, dependency-free LLM provider adapters (raw HTTPS via httpx).

Conversation history is kept in a neutral format so the agent loop does not care which provider runs:
    {"role": "user", "text": str}
    {"role": "assistant", "text": str, "tool_calls": [ToolCall, ...]}
    {"role": "tool", "results": [{"id": str, "name": str, "content": str}, ...]}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Protocol

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
    truncated: bool = False  # the model hit max_tokens: the text is cut off, not finished


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON schema


OnText = Callable[[str], None]


class Provider(Protocol):
    """`force_text=True` asks the model to answer in prose without calling tools (used on the last agent round)."""

    name: str
    model: str

    def complete(self, system: str, history: list[dict], tools: list[ToolSpec], force_text: bool = False) -> LLMResponse: ...

    def stream(
        self, system: str, history: list[dict], tools: list[ToolSpec], on_text: OnText, force_text: bool = False
    ) -> LLMResponse: ...


def _sse_data(lines):
    """Yield parsed JSON payloads from a server-sent-events line iterator."""
    for line in lines:
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        try:
            yield json.loads(payload)
        except ValueError:
            continue


def _raise_for(resp: httpx.Response, who: str) -> None:
    if resp.status_code == 200:
        return
    try:
        data = resp.json()
        detail = data.get("error", data) if isinstance(data, dict) else data
        msg = (detail.get("message") or str(detail)) if isinstance(detail, dict) else str(detail)
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

        def add(role: str, content: list[dict]) -> None:
            # Anthropic wants alternating roles: a tool-result turn followed by a user text turn
            # (e.g. "tool budget used up") is one user message with several content blocks.
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"].extend(content)
            else:
                msgs.append({"role": role, "content": content})

        for h in history:
            if h["role"] == "user":
                add("user", [{"type": "text", "text": h["text"] or "(empty)"}])
            elif h["role"] == "assistant":
                content: list[dict] = []
                if h.get("text"):
                    content.append({"type": "text", "text": h["text"]})
                for tc in h.get("tool_calls") or []:
                    content.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
                add("assistant", content or [{"type": "text", "text": "(no text)"}])
            elif h["role"] == "tool":
                add("user", [{"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]} for r in h["results"]])
        return msgs

    def _body(self, system: str, history: list[dict], tools: list[ToolSpec], force_text: bool = False) -> dict:
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            # The system prompt carries the (large, stable) game context: cache it across chat turns.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": self.to_messages(history),
        }
        if tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
            if force_text:
                body["tool_choice"] = {"type": "none"}
        return body

    def stream(self, system: str, history: list[dict], tools: list[ToolSpec], on_text: OnText, force_text: bool = False) -> LLMResponse:
        body = self._body(system, history, tools, force_text) | {"stream": True}
        blocks: dict[int, dict] = {}
        text: list[str] = []
        usage = {"in": 0, "out": 0}
        truncated = False
        try:
            with self.http.stream("POST", "/v1/messages", json=body) as resp:
                if resp.status_code != 200:
                    resp.read()
                    _raise_for(resp, "Anthropic")
                for ev in _sse_data(resp.iter_lines()):
                    kind = ev.get("type")
                    if kind == "message_start":
                        u = ev.get("message", {}).get("usage", {})
                        usage["in"] = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                    elif kind == "content_block_start":
                        cb = ev.get("content_block", {})
                        blocks[ev["index"]] = {"type": cb.get("type"), "id": cb.get("id"), "name": cb.get("name"), "json": ""}
                    elif kind == "content_block_delta":
                        d = ev.get("delta", {})
                        if d.get("type") == "text_delta":
                            text.append(d.get("text", ""))
                            on_text(d.get("text", ""))
                        elif d.get("type") == "input_json_delta" and ev.get("index") in blocks:
                            blocks[ev["index"]]["json"] += d.get("partial_json", "")
                    elif kind == "message_delta":
                        usage["out"] = ev.get("usage", {}).get("output_tokens", usage["out"])
                        if (ev.get("delta") or {}).get("stop_reason") == "max_tokens":
                            truncated = True
                    elif kind == "error":
                        raise LLMError(f"Anthropic stream error: {ev.get('error', {}).get('message')}")
        except httpx.HTTPError as exc:
            raise LLMError(f"Anthropic request failed: {exc}") from exc
        calls = []
        for _, b in sorted(blocks.items()):
            if b["type"] == "tool_use":
                try:
                    args = json.loads(b["json"] or "{}")
                except ValueError:
                    args = {}
                calls.append(ToolCall(b["id"], b["name"], args))
        return LLMResponse(text="".join(text), tool_calls=calls, input_tokens=usage["in"], output_tokens=usage["out"], truncated=truncated)

    def complete(self, system: str, history: list[dict], tools: list[ToolSpec], force_text: bool = False) -> LLMResponse:
        body = self._body(system, history, tools, force_text)
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
            truncated=data.get("stop_reason") == "max_tokens",
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
                calls = h.get("tool_calls") or []
                # content may only be null when tool calls are present
                m: dict = {"role": "assistant", "content": h.get("text") or (None if calls else "")}
                if calls:
                    m["tool_calls"] = [
                        {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                        for tc in calls
                    ]
                msgs.append(m)
            elif h["role"] == "tool":
                for r in h["results"]:
                    msgs.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
        return msgs

    def _body(self, system: str, history: list[dict], tools: list[ToolSpec], force_text: bool = False) -> dict:
        # Reasoning models spend completion tokens on hidden reasoning, hence the headroom.
        body: dict = {"model": self.model, "messages": self.to_messages(system, history), "max_completion_tokens": self.max_tokens * 4}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]
            if force_text:
                body["tool_choice"] = "none"
        return body

    def stream(self, system: str, history: list[dict], tools: list[ToolSpec], on_text: OnText, force_text: bool = False) -> LLMResponse:
        body = self._body(system, history, tools, force_text) | {"stream": True, "stream_options": {"include_usage": True}}
        text: list[str] = []
        slots: dict[int, dict] = {}
        truncated = False
        usage = {}
        try:
            with self.http.stream("POST", "/chat/completions", json=body) as resp:
                if resp.status_code != 200:
                    resp.read()
                    _raise_for(resp, "OpenAI")
                for chunk in _sse_data(resp.iter_lines()):
                    usage = chunk.get("usage") or usage
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta", {})
                        if delta.get("content"):
                            text.append(delta["content"])
                            on_text(delta["content"])
                        if choice.get("finish_reason") == "length":
                            truncated = True
                        for tc in delta.get("tool_calls") or []:
                            slot = slots.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            slot["name"] = fn.get("name") or slot["name"]
                            slot["args"] += fn.get("arguments") or ""
        except httpx.HTTPError as exc:
            raise LLMError(f"OpenAI request failed: {exc}") from exc
        calls = []
        for _, s in sorted(slots.items()):
            try:
                args = json.loads(s["args"] or "{}")
            except ValueError:
                args = {}
            calls.append(ToolCall(s["id"], s["name"], args))
        return LLMResponse(
            text="".join(text), tool_calls=calls, input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0), truncated=truncated
        )

    def complete(self, system: str, history: list[dict], tools: list[ToolSpec], force_text: bool = False) -> LLMResponse:
        body = self._body(system, history, tools, force_text)
        try:
            resp = self.http.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"OpenAI request failed: {exc}") from exc
        _raise_for(resp, "OpenAI")
        data = resp.json()
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("OpenAI returned no choices") from exc
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
            truncated=data["choices"][0].get("finish_reason") == "length",
        )


class ScriptedProvider:
    """Deterministic provider for tests and the eval smoke-run: replays canned responses in order."""

    name = "scripted"
    model = "scripted"

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, system: str, history: list[dict], tools: list[ToolSpec], force_text: bool = False) -> LLMResponse:
        self.calls.append({"system": system, "history": [dict(h) for h in history], "tools": [t.name for t in tools], "force_text": force_text})
        if not self.responses:
            return LLMResponse(text="(no more scripted responses)")
        resp = self.responses.pop(0)
        if force_text and resp.tool_calls:
            # A real provider is told `tool_choice: none` on the last round; mimic that so scripted
            # runs exercise the same path (text only, tool calls dropped).
            resp = LLMResponse(text=resp.text, input_tokens=resp.input_tokens, output_tokens=resp.output_tokens)
        return resp

    def stream(self, system: str, history: list[dict], tools: list[ToolSpec], on_text: OnText, force_text: bool = False) -> LLMResponse:
        resp = self.complete(system, history, tools, force_text)
        for i in range(0, len(resp.text), 24):
            on_text(resp.text[i : i + 24])
        return resp


def make_provider(settings: Settings, override: str | None = None) -> Provider | None:
    choice = (override or settings.llm_provider or "auto").lower()
    if choice == "none":
        return None
    if choice in ("auto", "anthropic") and settings.anthropic_api_key:
        return AnthropicProvider(settings.anthropic_api_key, settings.anthropic_model, settings.llm_max_tokens, settings.llm_timeout_s)
    if choice in ("auto", "openai") and settings.openai_api_key:
        return OpenAIProvider(settings.openai_api_key, settings.openai_model, settings.llm_max_tokens, settings.llm_timeout_s)
    return None
