import json

import httpx
from fastapi.testclient import TestClient

from coach import agent
from coach.llm import AnthropicProvider, LLMResponse, OpenAIProvider, ScriptedProvider, ToolCall
from coach.server import create_app


def _sse(events):
    return "".join(f"event: {e.get('type', 'x')}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def test_anthropic_stream_parses_text_and_tool_calls():
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 12, "cache_read_input_tokens": 100}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Let me "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "check."}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu_1", "name": "evaluate_move", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"ref": "G1 22...Qc7", '}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"move": "Nxe4"}'}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 30}},
        {"type": "message_stop"},
    ]
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=_sse(events), headers={"content-type": "text/event-stream"})

    p = AnthropicProvider("k", "m", 100, 10)
    p.http = httpx.Client(base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler))
    chunks = []
    r = p.stream("S", [{"role": "user", "text": "hi"}], agent.TOOLS, chunks.append)
    assert seen["body"]["stream"] is True
    assert chunks == ["Let me ", "check."] and r.text == "Let me check."
    assert r.tool_calls[0].name == "evaluate_move" and r.tool_calls[0].arguments == {"ref": "G1 22...Qc7", "move": "Nxe4"}
    assert r.input_tokens == 112 and r.output_tokens == 30


def test_openai_stream_parses_text_and_tool_calls():
    chunks = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "get_moment", "arguments": '{"ref":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"G1 22...Qc7"}'}}]}}]},
        {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    p = OpenAIProvider("k", "m", 100, 10)
    p.http = httpx.Client(base_url="https://api.openai.com/v1", transport=httpx.MockTransport(lambda req: httpx.Response(200, content=body.encode())))
    got = []
    r = p.stream("S", [{"role": "user", "text": "hi"}], agent.TOOLS, got.append)
    assert "".join(got) == "Hello" == r.text
    assert r.tool_calls[0].name == "get_moment" and r.tool_calls[0].arguments == {"ref": "G1 22...Qc7"}
    assert (r.input_tokens, r.output_tokens) == (9, 4)


def test_stream_errors_are_readable():
    p = AnthropicProvider("k", "m", 100, 10)
    p.http = httpx.Client(base_url="https://api.anthropic.com", transport=httpx.MockTransport(lambda req: httpx.Response(529, json={"error": {"message": "overloaded"}})))
    try:
        p.stream("S", [{"role": "user", "text": "x"}], [], lambda t: None)
    except Exception as exc:
        assert "529" in str(exc) and "overloaded" in str(exc)
    else:
        raise AssertionError("expected an error")


def test_agent_emits_round_delta_tool_and_status_events(session, service, settings, crit):
    provider = ScriptedProvider(
        [
            LLMResponse(text="Checking.", tool_calls=[ToolCall("t1", "get_moment", {"ref": crit["ref"]})]),
            LLMResponse(text=f"In [[{crit['ref']}]] the engine preferred {crit['best']}."),
        ]
    )
    events = []
    out = agent.chat(session, provider, service.pool, settings, "why?", on_event=events.append)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "round" and "tool" in kinds and kinds.count("round") == 2 and "status" in kinds
    streamed = "".join(e["text"] for e in events[kinds.index("tool") :] if e["type"] == "delta")
    assert streamed == out["markdown"]


def _events(resp):
    return [json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]


def test_sse_endpoints(service, session, crit):
    client = TestClient(create_app(service))
    rep = client.post(f"/api/sessions/{session.sid}/report/stream")
    assert rep.headers["content-type"].startswith("text/event-stream")
    evs = _events(rep)
    assert evs[-1]["type"] == "done" and "## The big picture" in evs[-1]["result"]["markdown"]

    old = service.provider
    service.provider = ScriptedProvider([LLMResponse(text=f"In [[{crit['ref']}]] the engine preferred {crit['best']}.")])
    try:
        evs = _events(client.post(f"/api/sessions/{session.sid}/chat/stream", json={"message": "why?"}))
        assert [e["type"] for e in evs][0] == "round" and evs[-1]["type"] == "done"
        assert evs[-1]["result"]["verification"]["score"] == 1.0
    finally:
        service.provider = old


def test_rate_limit_protects_public_deployments(service, session, settings):
    client = TestClient(create_app(service))
    old_provider, old_limit = service.provider, settings.llm_rate_limit_per_hour
    service.provider = ScriptedProvider([LLMResponse(text="ok"), LLMResponse(text="ok")])
    settings.llm_rate_limit_per_hour = 1
    try:
        headers = {"x-forwarded-for": "203.0.113.7"}
        assert client.post(f"/api/sessions/{session.sid}/chat", json={"message": "a"}, headers=headers).status_code == 200
        assert client.post(f"/api/sessions/{session.sid}/chat", json={"message": "b"}, headers=headers).status_code == 429
        other = {"x-forwarded-for": "198.51.100.9"}
        assert client.post(f"/api/sessions/{session.sid}/chat", json={"message": "c"}, headers=other).status_code == 200
    finally:
        service.provider, settings.llm_rate_limit_per_hour = old_provider, old_limit
