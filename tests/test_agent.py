import json

import httpx

from coach import agent
from coach.llm import AnthropicProvider, LLMResponse, OpenAIProvider, ScriptedProvider, ToolCall


def test_tool_loop_runs_engine_and_feeds_results_back(session, service, settings, g1_moment):
    best = g1_moment["best"]
    provider = ScriptedProvider(
        [
            LLMResponse(text="Let me check that.", tool_calls=[ToolCall("t1", "evaluate_move", {"ref": "G1 22...Qc7", "move": best})]),
            LLMResponse(text=f"Yes: in [[G1 22...Qc7]] the engine's choice was {best}, which keeps your edge."),
        ]
    )
    out = agent.chat(session, provider, service.pool, settings, f"Was {best} better than Qc7 in G1?")
    assert out["trace"][0]["tool"] == "evaluate_move"
    assert "this IS the engine's top choice" in out["trace"][0]["result"]
    # the tool result was sent back to the model as a tool message
    second_call_history = provider.calls[1]["history"]
    assert second_call_history[-1]["role"] == "tool"
    assert out["verification"]["score"] == 1.0 and not out["repaired"]
    assert session.chat_history[-1]["role"] == "assistant"


def test_verifier_triggers_one_repair_pass(session, service, settings, g1_moment):
    provider = ScriptedProvider(
        [
            LLMResponse(text="In [[G1 22...Qd7]] you should have played Nxh8."),
            LLMResponse(text=f"In [[G1 22...Qc7]] the engine preferred {g1_moment['best']}."),
        ]
    )
    out = agent.chat(session, provider, service.pool, settings, "What went wrong in G1?")
    assert out["repaired"] is True
    assert out["verification_before_repair"]["score"] < 1.0
    assert out["verification"]["score"] == 1.0
    repair_prompt = provider.calls[1]["history"][-1]["text"]
    assert "Nxh8" in repair_prompt and "22...Qc7" in repair_prompt


def test_illegal_tool_input_is_reported_not_raised(session, service, settings):
    tb = agent.Toolbox(session, service.pool, settings)
    assert "not a legal move" in tb.call(ToolCall("x", "evaluate_move", {"ref": "G1 22...Qc7", "move": "Qh5"}))
    assert "does not match any move" in tb.call(ToolCall("y", "analyze_position", {"ref": "G42 1.e4"}))
    assert "Unknown tool" in tb.call(ToolCall("z", "rm_rf", {}))


def test_template_report_is_fully_grounded(session, service, settings):
    rep = agent.generate_report(session, None, service.pool, settings)
    assert rep["mode"] == "template"
    assert rep["verification"]["score"] == 1.0, rep["verification"]["flagged"]
    assert rep["verification"]["citations"]["total"] >= 5


# --- provider adapters against mocked HTTP --------------------------------------------------------
def _anthropic_with(handler):
    p = AnthropicProvider("sk-test", "claude-test", 500, 10)
    p.http = httpx.Client(base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler))
    return p


def test_anthropic_wire_format():
    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "checking"},
                    {"type": "tool_use", "id": "tu_1", "name": "get_moment", "input": {"ref": "G1 22...Qc7"}},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 90},
            },
        )

    p = _anthropic_with(handler)
    history = [
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": "", "tool_calls": [ToolCall("tu_0", "get_game_moves", {"game_id": "G1"})]},
        {"role": "tool", "results": [{"id": "tu_0", "name": "get_game_moves", "content": "1.e4"}]},
    ]
    r = p.complete("SYSTEM", history, agent.TOOLS)
    body = seen["body"]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][1]["content"][0] == {"type": "tool_use", "id": "tu_0", "name": "get_game_moves", "input": {"game_id": "G1"}}
    assert body["messages"][2]["content"][0]["type"] == "tool_result"
    assert {t["name"] for t in body["tools"]} == {"get_game_moves", "get_moment", "analyze_position", "evaluate_move"}
    assert r.text == "checking" and r.tool_calls[0].arguments == {"ref": "G1 22...Qc7"}
    assert r.input_tokens == 100


def test_anthropic_error_is_readable():
    p = _anthropic_with(lambda req: httpx.Response(401, json={"error": {"message": "invalid x-api-key"}}))
    try:
        p.complete("s", [{"role": "user", "text": "x"}], [])
    except Exception as exc:
        assert "401" in str(exc) and "invalid x-api-key" in str(exc)
    else:
        raise AssertionError("expected an error")


def test_openai_wire_format():
    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "evaluate_move", "arguments": '{"ref":"G1 22...Qc7","move":"Nxe4"}'}}],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    p = OpenAIProvider("sk-test", "gpt-test", 500, 10)
    p.http = httpx.Client(base_url="https://api.openai.com/v1", transport=httpx.MockTransport(handler))
    history = [
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": "", "tool_calls": [ToolCall("c0", "get_moment", {"ref": "G1 22...Qc7"})]},
        {"role": "tool", "results": [{"id": "c0", "name": "get_moment", "content": "detail"}]},
    ]
    r = p.complete("SYSTEM", history, agent.TOOLS)
    msgs = seen["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "SYSTEM"}
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "get_moment"
    assert msgs[3] == {"role": "tool", "tool_call_id": "c0", "content": "detail"}
    assert r.tool_calls[0].name == "evaluate_move" and r.tool_calls[0].arguments["move"] == "Nxe4"
