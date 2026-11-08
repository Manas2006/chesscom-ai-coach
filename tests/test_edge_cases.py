"""Edge cases that need no Stockfish: parsing, scoring maths, the verifier's regexes, the Chess.com
client's failure modes, the LLM adapters' wire quirks, and the agent loop's budget handling."""

import json
import os

import chess
import httpx
import pytest

from coach import agent, config
from coach.chesscom import API, ChessComClient, ChessComError, PlayerNotFound, _filter_games, _retry_after, normalize_username
from coach.engine import (
    GameAnalysis,
    MoveAnalysis,
    aggregate_accuracy,
    classify,
    fmt_eval,
    label_misses,
    move_accuracy,
    pick_candidate_moments,
    win_pct,
    win_pct_from_cp,
)
from coach.llm import AnthropicProvider, LLMError, LLMResponse, OpenAIProvider, ScriptedProvider, ToolCall, _raise_for, _sse_data
from coach.motifs import _even_prefix, _fmt_secs
from coach.pgn import _outcome, opening_from_eco_url, parse_game, parse_time_control
from coach.profile import build_profile, rating_band
from coach.session import CoachSession, clean_san
from coach.verify import CITE_RE, EVAL_RE, SAN_RE, Evidence, problems_for_repair, verify

HEADERS = '[Event "x"]\n[Site "Chess.com"]\n[White "Alice"]\n[Black "Bob"]\n[Result "*"]\n'


def _raw(movetext: str, **over) -> dict:
    raw = {
        "pgn": HEADERS + "\n" + movetext,
        "white": {"username": "Alice", "rating": 1500, "result": "win"},
        "black": {"username": "Bob", "rating": 1480, "result": "resigned"},
        "time_class": "blitz",
        "time_control": "180+2",
        "rules": "chess",
        "rated": True,
        "end_time": 1_700_000_000,
        "url": "https://www.chess.com/game/live/1",
        "uuid": "uuid-1",
    }
    raw.update(over)
    return raw


def _empty_analysis(gid: str) -> GameAnalysis:
    return GameAnalysis(gid, 0, [], [], [], None, None, {}, {}, 50.0, 50.0)


def _session(*games) -> CoachSession:
    return CoachSession(sid="s", username="alice", games=list(games), analyses={g.gid: _empty_analysis(g.gid) for g in games}, profile={})


# ---------------------------------------------------------------------------------------------
# PGN parsing
# ---------------------------------------------------------------------------------------------
def test_zero_move_game_parses_to_no_plies():
    g = parse_game(_raw("*"), "alice", "G1")
    assert g.plies == [] and g.player_plies() == [] and g.find_ply(1, "white") is None
    assert g.start_fen == chess.STARTING_FEN


def test_missing_or_empty_pgn_is_a_value_error_not_a_key_error():
    with pytest.raises(ValueError):
        parse_game({"white": {"username": "Alice"}, "black": {"username": "Bob"}}, "alice", "G1")
    with pytest.raises(ValueError):
        parse_game(_raw("", pgn=""), "alice", "G1")


def test_player_must_be_one_of_the_two_sides():
    with pytest.raises(ValueError):
        parse_game(_raw("1. e4 *"), "carol", "G1")
    with pytest.raises(ValueError):
        parse_game(_raw("1. e4 *"), "", "G1")


def test_null_usernames_from_the_api_do_not_crash():
    raw = _raw("1. e4 e5 *", white={"username": None, "rating": None}, black={"username": "Bob", "result": "win"})
    g = parse_game(raw, "bob", "G1")
    assert g.player_color == "black" and g.white == "?" and g.opponent == "?" and g.white_rating is None


def test_username_match_is_case_insensitive_and_sets_perspective():
    g = parse_game(_raw("1. e4 e5 2. Nf3 *"), "BOB", "G7")
    assert g.player == "Bob" and g.player_color == "black" and g.opponent == "Alice"
    assert g.outcome == "loss" and g.end_reason == "resigned"
    assert [p.label for p in g.player_plies()] == ["1...e5"]
    assert g.find_ply(2, "white").san == "Nf3" and g.find_ply(2, "black") is None


def test_daily_games_have_no_clocks_and_are_not_live():
    pgn = HEADERS + '[TimeControl "1/86400"]\n\n1. e4 {[%clk 23:59:00]} e5 {[%clk 23:58:00]} *'
    g = parse_game(_raw("", pgn=pgn, time_class="daily", time_control="1/86400", end_time=None), "alice", "G1")
    assert g.base_s is None and g.inc_s == 0.0 and not g.is_live
    assert all(p.clock is None and p.time_spent is None for p in g.plies)
    assert g.date == "" and g.end_time == 0


def test_time_spent_accounts_for_increment_and_never_goes_negative():
    pgn = HEADERS + "\n1. e4 {[%clk 0:03:02]} e5 {[%clk 0:02:58]} 2. Nf3 {[%clk 0:02:55]} Nc6 *"
    g = parse_game(_raw("", pgn=pgn), "alice", "G1")
    e4, e5, nf3, nc6 = g.plies
    assert e4.clock == 182.0 and e4.time_spent == 0.0  # pre-move added the increment: clamp, do not go negative
    assert e5.time_spent == pytest.approx(4.0)  # 180 - 178 + 2
    assert nf3.time_spent == pytest.approx(9.0)  # 182 - 175 + 2
    assert nc6.clock is None and nc6.time_spent is None  # no clock comment on the last move


def test_illegal_move_truncates_the_mainline_instead_of_raising():
    g = parse_game(_raw("1. e4 e5 2. Nf3 Qh4xx 3. Bc4 *"), "alice", "G1")
    assert [p.san for p in g.plies] == ["e4", "e5", "Nf3", "Qh4", "Bc4"]


def test_games_from_a_set_up_position_keep_their_fen():
    fen = "k7/2Q5/8/8/8/8/8/K7 w - - 0 1"
    pgn = HEADERS + f'[SetUp "1"]\n[FEN "{fen}"]\n\n1. Ka2 1/2-1/2'
    g = parse_game(_raw("", pgn=pgn), "alice", "G1")
    assert g.start_fen == fen and g.plies[0].move_number == 1 and chess.Board(g.plies[0].fen_after).is_stalemate()


def test_time_control_parsing_edge_inputs():
    assert parse_time_control("") == (None, 0.0)
    assert parse_time_control("-") == (None, 0.0)
    assert parse_time_control("abc") == (None, 0.0)
    assert parse_time_control("+") == (None, 0.0)
    assert parse_time_control("600+") == (600.0, 0.0)
    assert parse_time_control(" 60+1 ") == (60.0, 1.0)
    assert parse_time_control("1/259200") == (None, 0.0)


@pytest.mark.parametrize(
    "url,name,family",
    [
        (None, "Unknown opening", "Unknown opening"),
        ("", "Unknown opening", "Unknown opening"),
        ("https://www.chess.com/openings/", "Unknown opening", "Unknown opening"),
        ("https://www.chess.com/openings/Undefined", "Unknown opening", "Unknown opening"),
        ("https://www.chess.com/openings/Sicilian-Defense-Open-2...Nc6-3.d4", "Sicilian Defense Open", "Sicilian Defense"),
        ("https://www.chess.com/openings/Kings-Indian-Attack", "Kings Indian Attack", "Kings Indian Attack"),
        ("https://www.chess.com/openings/Queens-Gambit-Declined-3...Nf6", "Queens Gambit Declined", "Queens Gambit"),
    ],
)
def test_opening_slug_edge_inputs(url, name, family):
    assert opening_from_eco_url(url) == (name, family)


@pytest.mark.parametrize("code", ["agreed", "repetition", "stalemate", "insufficient", "50move", "timevsinsufficient"])
def test_every_chesscom_draw_code_is_a_draw(code):
    assert _outcome({"result": code}, {"result": code}) == ("draw", code)


def test_unknown_and_missing_result_codes_are_losses():
    assert _outcome({"result": "abandoned"}, {"result": "win"}) == ("loss", "abandoned")
    assert _outcome({}, {"result": "win"}) == ("loss", "unknown")
    assert _outcome({"result": "win"}, {}) == ("win", "unknown")


def test_username_normalisation_edges():
    assert normalize_username("  @Hikaru ") == "hikaru"
    assert normalize_username("a-b_C9") == "a-b_c9"
    for bad in ["", "a", "x" * 41, "nice name", "émile", "../etc", None]:
        with pytest.raises(PlayerNotFound):
            normalize_username(bad)


# ---------------------------------------------------------------------------------------------
# Scoring maths
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "drop,expected",
    [(0.0, "excellent"), (1.9, "excellent"), (2.0, "good"), (4.9, "good"), (5.0, "inaccuracy"), (9.9, "inaccuracy"), (10.0, "mistake"), (19.9, "mistake"), (20.0, "blunder"), (100.0, "blunder")],
)
def test_classification_thresholds_are_inclusive_at_the_boundary(drop, expected):
    assert classify(drop, False) == expected


def test_best_move_wins_regardless_of_measured_drop():
    assert classify(30.0, True) == "best"


def test_win_probability_is_clamped_and_symmetric():
    assert win_pct_from_cp(1e9) == pytest.approx(win_pct_from_cp(1000))
    assert win_pct_from_cp(-1e9) == pytest.approx(win_pct_from_cp(-1000))
    assert win_pct_from_cp(0) == 50.0
    assert win_pct_from_cp(300) + win_pct_from_cp(-300) == pytest.approx(100.0)
    assert win_pct(None, 1) == 100.0 and win_pct(None, -1) == 0.0 and win_pct(None, None) == 50.0


def test_accuracy_helpers_handle_degenerate_input():
    assert aggregate_accuracy([]) is None
    assert aggregate_accuracy([100.0]) == 100.0
    assert 0.0 <= aggregate_accuracy([0.0, 0.0]) <= 1.0  # harmonic mean guards against division by zero
    assert move_accuracy(0) == pytest.approx(100.0, abs=1e-3) and move_accuracy(1e6) == 0.0 and move_accuracy(-5) == move_accuracy(0)
    assert aggregate_accuracy([100.0, 0.0]) < 50.0  # one disaster drags the harmonic mean down


def test_eval_formatting_edges():
    assert fmt_eval(None, None) == "+0.0"
    assert fmt_eval(0, None) == "+0.0"
    assert fmt_eval(-50, None) == "-0.5" and fmt_eval(1234, None) == "+12.3"
    assert fmt_eval(None, -3) == "#-3" and fmt_eval(None, 12) == "#12"


def test_rating_bands():
    assert rating_band(None) == "unknown"
    assert [rating_band(r) for r in (999, 1000, 1499, 1500, 1999, 2000, 2399, 2400)] == [
        "beginner", "intermediate", "intermediate", "advanced club", "advanced club", "expert", "expert", "master",
    ]


def _ma(ply: int, color: str, cls: str, drop: float = 10.0) -> MoveAnalysis:
    return MoveAnalysis(ply, color, f"{ply // 2 + 1}{'.' if color == 'white' else '...'}x", "x", 50.0, 50.0 - drop, drop, "", "", None, cls, 50.0, "middlegame")


def test_label_misses_handles_chains_and_degenerate_lists():
    assert label_misses([]) == []
    assert label_misses([_ma(0, "white", "blunder")]) == []
    ms = [_ma(0, "white", "blunder"), _ma(1, "black", "blunder"), _ma(2, "white", "mistake"), _ma(3, "black", "inaccuracy"), _ma(4, "white", "mistake")]
    assert label_misses(ms) == [1, 2]
    assert [m.classification for m in ms] == ["blunder", "miss", "miss", "inaccuracy", "mistake"]
    # an inaccuracy is never relabelled and never counts as an error to punish
    assert label_misses([_ma(0, "white", "inaccuracy"), _ma(1, "black", "blunder")]) == []


def test_candidate_moments_fall_back_to_inaccuracies_and_respect_colour():
    ms = [_ma(0, "white", "inaccuracy", 6), _ma(1, "black", "blunder", 40), _ma(2, "white", "inaccuracy", 8)]
    assert [m.ply for m in pick_candidate_moments(ms, "white")] == [2, 0]
    assert [m.ply for m in pick_candidate_moments(ms, "black")] == [1]
    assert pick_candidate_moments([_ma(0, "white", "good", 3)], "white") == []


def test_motif_helpers():
    assert _even_prefix(None) == []
    assert _fmt_secs(5) == "5s" and _fmt_secs(59.6) == "60s" and _fmt_secs(60) == "1:00" and _fmt_secs(125.9) == "2:05"


# ---------------------------------------------------------------------------------------------
# Profile aggregation
# ---------------------------------------------------------------------------------------------
def test_profile_with_no_games_and_no_stats_is_well_formed():
    p = build_profile("alice", None, None, [], {})
    assert p["sample"]["games"] == 0 and p["sample"]["score"] is None and p["sample"]["date_range"] == ["", ""]
    assert p["accuracy"]["overall"] is None and p["accuracy"]["worst_phase"] is None
    assert p["errors"]["per_game"] == {"inaccuracy": 0, "mistake": 0, "miss": 0, "blunder": 0}
    assert p["ratings"] == {} and p["openings"] == [] and p["patterns"] == [] and p["worst_moments"] == []
    assert p["profile_url"].endswith("/member/alice")


def test_profile_ignores_malformed_stats_blocks():
    stats = {"chess_blitz": {"last": {"rating": 1200}}, "chess_rapid": "nope", "chess_bullet": {"last": None}, "tactics": None}
    p = build_profile("alice", {"username": "Alice"}, stats, [], {})
    assert p["ratings"] == {"blitz": {"current": 1200, "best": None, "record": {}}}
    assert p["tactics_highest"] is None and p["username"] == "Alice"


def test_profile_with_games_of_one_colour_only():
    g1 = parse_game(_raw("1. e4 e5 *"), "alice", "G1")
    g2 = parse_game(_raw("1. d4 d5 *", url="u2", uuid="uuid-2", white={"username": "Alice", "result": "agreed"}, black={"username": "Bob", "result": "agreed"}), "alice", "G2")
    p = build_profile("alice", {}, {}, [g1, g2], {"G1": _empty_analysis("G1"), "G2": _empty_analysis("G2")})
    assert p["sample"]["by_color"]["white"] == {"games": 2, "win": 1, "loss": 0, "draw": 1, "score": 75.0}
    assert p["sample"]["by_color"]["black"]["games"] == 0 and p["sample"]["by_color"]["black"]["score"] is None
    assert p["sample"]["rating_band"] == "advanced club" and p["sample"]["avg_rating"] == 1500
    assert len(p["openings"]) == 1 and p["openings"][0]["games"] == 2  # both unknown openings, same family


# ---------------------------------------------------------------------------------------------
# Verifier regexes and anchoring (synthetic games, no engine)
# ---------------------------------------------------------------------------------------------
def test_bare_citations_without_san_are_still_checked():
    g = parse_game(_raw("1. e4 e5 2. Nf3 Nc6 *"), "alice", "G1")
    s = _session(g)
    assert CITE_RE.findall("[[G1 2.]] [[G1 2...]] [[ G1 1. e4 ]]") == ["G1 2.", "G1 2...", "G1 1. e4"]
    res = verify("Compare [[G1 2.]] and [[G1 2...]] with [[G1 9.]] and [[G1 1...e4]].", s, "")
    assert res["citations"]["ok"] == 2 and res["citations"]["total"] == 4
    assert any("no such move" in f for f in res["flagged"]) and any("the move played was 1...e5" in f for f in res["flagged"])


def test_castling_and_promotion_suffixes_are_single_tokens():
    assert [m.group(0) for m in SAN_RE.finditer("He played O-O-O+ then O-O.")] == ["O-O-O+", "O-O"]
    assert [m.group(0) for m in SAN_RE.finditer("e8=Q# ends it, or exd8=N+")] == ["e8=Q#", "exd8=N+"]
    assert [m.group(0) for m in SAN_RE.finditer("the file-Nf3 Nf3-ish Nf3. d4-d5 /Nf3 [Nf3")] == ["Nf3"]
    assert clean_san(" O-O-O+!? ") == "O-O-O" and clean_san("e8=Q#") == "e8=Q"


def test_castling_is_verified_against_the_real_position():
    g = parse_game(_raw("1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5 4. O-O Nf6 *"), "alice", "G1")
    s = _session(g)
    good = verify("In [[G1 4.O-O]] castling (O-O) was natural; Bxf7+ was also possible.", s, "")
    assert good["moves"]["ok"] == 2 and good["flagged"] == []
    bad = verify("In [[G1 4.O-O]] you could have played O-O-O instead.", s, "")
    assert "O-O-O" in bad["flagged"]


def test_squares_and_bare_pawn_moves_are_only_moves_when_numbered():
    g = parse_game(_raw("1. e4 e5 2. Nf3 Nc6 *"), "alice", "G1")
    s = _session(g)
    res = verify("The e4 square and the d5 break matter; 1.e4 was played, 1.Kd3 was not. [[G1 1.e4]]", s, "")
    assert res["moves"]["total"] == 2 and res["flagged"] == ["1.Kd3"]


def test_eval_regex_edges():
    found = EVAL_RE.findall("eval +1.7 then -0.4, mate #3 and #-2, but not +100.0, 2.5, -12.34, v1.2 or x+0.5")
    assert found == ["+1.7", "-0.4", "#3", "#-2"]
    g = parse_game(_raw("1. e4 e5 *"), "alice", "G1")
    res = verify("After [[G1 1.e4]] the eval was +0.3; the model also claims #4.", _session(g), "engine data: +0.3")
    assert res["evals"]["ok"] == 1 and "#4" in res["flagged"] and "+0.3" not in res["flagged"]


def test_moves_anchor_to_the_cited_paragraph_only():
    pgn = "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 7. Bb3 d6 8. c3 O-O 9. h3 Nb8 10. d4 Nbd7 *"
    g = parse_game(_raw(pgn), "alice", "G1")
    s = _session(g)
    # Nc3 is legal around move 2 but impossible once c3 holds a pawn (from move 8 on).
    text = "Paragraph one cites [[G1 2.Nf3]] where Nc3 was also fine.\n\nParagraph two cites [[G1 10...Nbd7]] where Nc3 is impossible."
    res = verify(text, s, "")
    assert res["moves"]["total"] == 2 and res["moves"]["ok"] == 1 and res["flagged"] == ["Nc3"]


def test_tool_evidence_can_legitimise_moves_outside_the_games():
    g = parse_game(_raw("1. e4 e5 *"), "alice", "G1")
    s = _session(g)
    text = "Considering [[G1 1...e5]], Bxf7+ is a threat to remember."
    assert verify(text, s, "")["flagged"] == ["Bxf7+"], "no bishop can take on f7 in this two-ply game"
    # A tool analysed the Italian position (Bc4 on the board): now the move has a position to live in.
    italian = chess.Board()
    for mv in ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"]:
        italian.push_san(mv)
    res = verify(text, s, "", Evidence(fens={italian.fen()}))
    assert res["flagged"] == [] and res["moves"]["ok"] == 1


def test_repair_prompt_lists_every_failure_kind():
    res = {
        "citations": {"failures": ["[[G1 9.]] (no such move in that game)"]},
        "moves": {"failures": ["Nxh8"]},
        "recommendations": {"failures": ["Qa1"]},
        "evals": {"failures": ["#7"]},
    }
    txt = problems_for_repair(res)
    assert "citation [[G1 9.]]" in txt and "'Nxh8' is not legal" in txt and "'Qa1' is recommended" in txt and "evaluation '#7'" in txt
    assert txt.count("\n") == 3


def test_verify_on_prose_without_claims_scores_perfect():
    g = parse_game(_raw("1. e4 e5 *"), "alice", "G1")
    res = verify("Keep practising. You are improving.", _session(g), "")
    assert res == {"score": 1.0, "checked": 0, "citations": {"total": 0, "ok": 0, "failures": []}, "moves": {"total": 0, "ok": 0, "failures": []}, "recommendations": {"total": 0, "ok": 0, "failures": []}, "evals": {"total": 0, "ok": 0, "failures": []}, "flagged": []}


# ---------------------------------------------------------------------------------------------
# Chess.com client failure modes
# ---------------------------------------------------------------------------------------------
def _client(tmp_path, handler):
    http = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "t"})
    return ChessComClient("t", tmp_path, http=http)


def test_non_json_and_non_object_bodies_become_chesscom_errors(tmp_path):
    c = _client(tmp_path, lambda req: httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(ChessComError, match="non-JSON"):
        c.profile("alice")
    c2 = _client(tmp_path, lambda req: httpx.Response(200, json=["not", "an", "object"]))
    with pytest.raises(ChessComError, match="unexpected payload"):
        c2.stats("alice")
    assert not list((tmp_path / "http").glob("*.json")), "nothing bad was cached"


def test_retry_after_header_parsing_is_safe():
    assert _retry_after(None, 1.5) == 1.5
    assert _retry_after("3", 1.0) == 3.0
    assert _retry_after("Wed, 21 Oct 2026 07:28:00 GMT", 2.0) == 2.0
    assert _retry_after("999999", 1.0) == 30.0
    assert _retry_after("-5", 1.0) == 0.0


def test_gives_up_after_repeated_rate_limits(tmp_path, monkeypatch):
    waits = []
    monkeypatch.setattr("time.sleep", lambda s: waits.append(s))
    c = _client(tmp_path, lambda req: httpx.Response(429, headers={"Retry-After": "junk"}))
    with pytest.raises(ChessComError, match="rate limit"):
        c.profile("alice")
    assert waits == [1.0, 2.0, 4.0], "exponential backoff when the header is unusable"


def test_other_http_errors_and_transport_failures_are_reported(tmp_path):
    c = _client(tmp_path, lambda req: httpx.Response(500))
    with pytest.raises(ChessComError, match="HTTP 500"):
        c.archives("alice")

    def boom(req):
        raise httpx.ConnectError("no route")

    with pytest.raises(ChessComError, match="Could not reach"):
        _client(tmp_path, boom).archives("alice")
    with pytest.raises(PlayerNotFound):
        _client(tmp_path, lambda req: httpx.Response(410)).profile("alice")


def test_recent_games_degenerate_requests_and_payloads(tmp_path):
    def handler(req):
        if req.url.path.endswith("/archives"):
            return httpx.Response(200, json={"archives": [f"{API}/player/a/games/2026/10"]})
        return httpx.Response(200, json={"games": ["junk", None, {"rules": "chess", "time_class": "blitz", "pgn": "", "end_time": 1}, {"rules": "chess", "time_class": "blitz", "pgn": "1. e4", "end_time": 2}]})

    c = _client(tmp_path, handler)
    assert c.recent_games("alice", 0) == [] and c.recent_games("alice", 5, max_months=0) == []
    got = c.recent_games("alice", 5)
    assert got == [{"rules": "chess", "time_class": "blitz", "pgn": "1. e4", "end_time": 2}]
    assert c.recent_games("alice", 5, time_class="rapid") == []
    assert _filter_games([{"rules": "chess960", "pgn": "x"}, {"rules": "chess", "pgn": "x", "time_class": "bullet"}], "all") == [{"rules": "chess", "pgn": "x", "time_class": "bullet"}]


def test_empty_archives_means_no_games(tmp_path):
    c = _client(tmp_path, lambda req: httpx.Response(200, json={"archives": []}))
    assert c.recent_games("alice", 10) == []


def test_corrupt_cache_entries_are_ignored(tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json={"username": "alice"})

    c = _client(tmp_path, handler)
    c.profile("alice")
    cache_file = next((tmp_path / "http").glob("*.json"))
    cache_file.write_text("{not json")
    assert c.profile("alice") == {"username": "alice"} and len(calls) == 2
    cache_file.write_text(json.dumps({"data": {"username": "alice"}}))  # missing timestamp
    assert c.profile("alice") == {"username": "alice"} and len(calls) == 3


# ---------------------------------------------------------------------------------------------
# LLM adapters
# ---------------------------------------------------------------------------------------------
def test_anthropic_merges_consecutive_user_turns_into_one_message():
    history = [
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": "", "tool_calls": [ToolCall("t0", "get_moment", {"ref": "G1 2.Nf3"})]},
        {"role": "tool", "results": [{"id": "t0", "name": "get_moment", "content": "detail"}]},
        {"role": "user", "text": "Tool budget used up."},
        {"role": "assistant", "text": "", "tool_calls": []},
        {"role": "user", "text": ""},
    ]
    msgs = AnthropicProvider.to_messages(history)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    assert [b["type"] for b in msgs[2]["content"]] == ["tool_result", "text"]
    assert msgs[3]["content"] == [{"type": "text", "text": "(no text)"}]
    assert msgs[4]["content"] == [{"type": "text", "text": "(empty)"}]


def test_openai_never_sends_null_content_without_tool_calls():
    msgs = OpenAIProvider.to_messages("S", [{"role": "user", "text": "q"}, {"role": "assistant", "text": "", "tool_calls": []}])
    assert msgs[2] == {"role": "assistant", "content": ""}
    msgs = OpenAIProvider.to_messages("S", [{"role": "assistant", "text": "", "tool_calls": [ToolCall("c", "get_moment", {"ref": "G1 2.Nf3"})]}])
    assert msgs[1]["content"] is None and msgs[1]["tool_calls"][0]["function"]["arguments"] == '{"ref": "G1 2.Nf3"}'


def test_forcing_a_text_answer_sets_tool_choice_none_on_both_providers():
    seen = {}

    def handler(req):
        seen[req.url.host] = json.loads(req.content)
        if "anthropic" in req.url.host:
            return httpx.Response(200, json={"content": [{"type": "text", "text": "done"}], "usage": {}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}], "usage": {}})

    a = AnthropicProvider("k", "m", 100, 5)
    a.http = httpx.Client(base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler))
    o = OpenAIProvider("k", "m", 100, 5)
    o.http = httpx.Client(base_url="https://api.openai.com/v1", transport=httpx.MockTransport(handler))
    hist = [{"role": "user", "text": "x"}]
    assert a.complete("S", hist, agent.TOOLS, force_text=True).text == "done"
    assert o.complete("S", hist, agent.TOOLS, force_text=True).text == "done"
    assert seen["api.anthropic.com"]["tool_choice"] == {"type": "none"} and "tools" in seen["api.anthropic.com"]
    assert seen["api.openai.com"]["tool_choice"] == "none"
    a.complete("S", hist, [], force_text=True)
    assert "tool_choice" not in seen["api.anthropic.com"], "no tools, nothing to disable"
    a.complete("S", hist, agent.TOOLS)
    assert "tool_choice" not in seen["api.anthropic.com"]


def test_error_bodies_of_any_shape_are_readable():
    for body in [{"error": {"message": "bad key"}}, {"error": "bad key"}, {"message": "bad key"}, ["bad key"]]:
        with pytest.raises(LLMError, match="bad key"):
            _raise_for(httpx.Response(400, json=body), "X")
    with pytest.raises(LLMError, match="plain text"):
        _raise_for(httpx.Response(502, text="plain text gateway error"), "X")


def test_openai_without_choices_is_an_llm_error():
    p = OpenAIProvider("k", "m", 100, 5)
    p.http = httpx.Client(base_url="https://api.openai.com/v1", transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"choices": []})))
    with pytest.raises(LLMError, match="no choices"):
        p.complete("S", [{"role": "user", "text": "x"}], [])


def test_sse_parser_skips_junk_and_stops_at_done():
    lines = ["", ": comment", "event: ping", "data: not json", 'data: {"a": 1}', "data: [DONE]", 'data: {"b": 2}']
    assert list(_sse_data(lines)) == [{"a": 1}]


def test_anthropic_stream_tolerates_malformed_tool_json_and_unknown_events():
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
        {"type": "mystery"},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tu", "name": "get_moment"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"ref": "G1 2.N'}},
        {"type": "content_block_delta", "index": 7, "delta": {"type": "input_json_delta", "partial_json": "ignored: no block"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "hi"}},
        {"type": "message_delta", "usage": {"output_tokens": 2}},
    ]
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    p = AnthropicProvider("k", "m", 100, 5)
    p.http = httpx.Client(base_url="https://api.anthropic.com", transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body)))
    got = []
    r = p.stream("S", [{"role": "user", "text": "x"}], agent.TOOLS, got.append)
    assert r.text == "hi" and got == ["hi"] and r.input_tokens == 3 and r.output_tokens == 2
    assert r.tool_calls[0].name == "get_moment" and r.tool_calls[0].arguments == {}, "truncated JSON degrades to empty arguments"


def test_scripted_provider_runs_out_gracefully_and_honours_force_text():
    p = ScriptedProvider([LLMResponse(text="a", tool_calls=[ToolCall("t", "get_moment", {"ref": "x"})])])
    r = p.complete("S", [], agent.TOOLS, force_text=True)
    assert r.text == "a" and r.tool_calls == []
    assert p.complete("S", [], []).text == "(no more scripted responses)"
    chunks = []
    p2 = ScriptedProvider([LLMResponse(text="x" * 50)])
    p2.stream("S", [], [], chunks.append)
    assert "".join(chunks) == "x" * 50 and len(chunks) == 3


# ---------------------------------------------------------------------------------------------
# Agent loop budget (the toolbox is never reached: the scripted replies carry no real tool calls)
# ---------------------------------------------------------------------------------------------
class _NoToolbox:
    """Stand-in toolbox: records nothing, needs no engine."""

    def call(self, tc):
        return f"ran {tc.name}"


def test_agent_loop_stops_at_the_budget_and_forces_a_text_answer():
    always_tools = [LLMResponse(text="", tool_calls=[ToolCall(f"t{i}", "get_moment", {"ref": "G1 2.Nf3"})]) for i in range(10)]
    p = ScriptedProvider(always_tools)
    res = agent.run_agent(p, "S", [{"role": "user", "text": "q"}], _NoToolbox(), max_rounds=2)
    assert res.rounds == 3 and len(res.trace) == 2
    last = p.calls[-1]
    assert last["force_text"] is True and last["history"][-1]["role"] == "user" and "budget" in last["history"][-1]["text"]
    assert all(c["force_text"] is False for c in p.calls[:-1])


def test_agent_loop_with_zero_budget_answers_immediately_in_prose():
    p = ScriptedProvider([LLMResponse(text="answer", tool_calls=[ToolCall("t", "get_moment", {"ref": "x"})])])
    res = agent.run_agent(p, "S", [{"role": "user", "text": "q"}], _NoToolbox(), max_rounds=0)
    assert res.text == "answer" and res.rounds == 1 and res.trace == []
    assert p.calls[0]["force_text"] is True
    assert not any("budget" in h.get("text", "") for h in p.calls[0]["history"]), "no tool was ever offered, so no budget note"


def test_agent_loop_without_tools_never_mentions_them():
    p = ScriptedProvider([LLMResponse(text="plain")])
    res = agent.run_agent(p, "S", [{"role": "user", "text": "q"}], _NoToolbox(), max_rounds=3, use_tools=False)
    assert res.text == "plain" and p.calls[0]["tools"] == [] and p.calls[0]["force_text"] is False


def test_int_coercion_helper():
    assert agent._to_int("12", 1) == 12 and agent._to_int(7.9, 1) == 7 and agent._to_int(None, 4) == 4 and agent._to_int("x", 4) == 4


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------
def test_env_parsing_falls_back_on_junk(monkeypatch):
    monkeypatch.setenv("ENGINE_DEPTH", "twelve")
    monkeypatch.setenv("LLM_RATE_LIMIT_PER_HOUR", "5")
    monkeypatch.setenv("ENGINE_WORKERS", "0")
    monkeypatch.setenv("LLM_PROVIDER", "OpenAI")
    monkeypatch.setenv("COACH_OFFLINE", "TRUE")
    monkeypatch.setenv("COACH_CACHE_DIR", "/tmp/coach-cache-test")
    s = config.Settings.from_env()
    assert s.engine_depth == 12 and s.llm_rate_limit_per_hour == 5 and s.engine_workers == 1
    assert s.llm_provider == "openai" and s.offline is True and str(s.cache_dir) == "/tmp/coach-cache-test"


def test_dotenv_loader_respects_quotes_comments_and_existing_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nFOO_TEST_KEY="quoted value"\nBAR_TEST_KEY=\'single\'\nBAZ_TEST_KEY=already\nmalformed line\n\n')
    monkeypatch.setenv("BAZ_TEST_KEY", "from-shell")
    for k in ("FOO_TEST_KEY", "BAR_TEST_KEY"):
        monkeypatch.delenv(k, raising=False)
    config._load_dotenv(env)
    assert os.environ["FOO_TEST_KEY"] == "quoted value" and os.environ["BAR_TEST_KEY"] == "single"
    assert os.environ["BAZ_TEST_KEY"] == "from-shell", "the shell wins over .env"
    config._load_dotenv(tmp_path / "missing.env")  # no error


# ---------------------------------------------------------------------------------------------
# Service without an engine, and the eval harness
# ---------------------------------------------------------------------------------------------
def test_service_stays_up_without_stockfish(tmp_path):
    from fastapi.testclient import TestClient

    from coach.server import create_app
    from coach.service import CoachService

    s = config.Settings(stockfish_path="/nonexistent/stockfish", cache_dir=tmp_path, llm_provider="none")
    svc = CoachService(s)
    try:
        h = svc.health()
        assert h["engine"]["ok"] is False and h["engine"]["error"] and h["llm"] == {"provider": None, "model": None}
        client = TestClient(create_app(svc))
        r = client.post("/api/analyze", json={"username": "gothamchess", "demo": True})
        assert r.status_code == 503 and "stockfish" in r.json()["detail"].lower()
        assert client.get("/api/health").json()["engine"]["ok"] is False
    finally:
        svc.close()

    with pytest.raises(RuntimeError, match="Stockfish not found"):
        from coach.engine import EnginePool

        EnginePool(None)


def test_llm_rate_limit_window_slides(monkeypatch, tmp_path):
    from coach.service import CoachService

    s = config.Settings(stockfish_path="/nonexistent", cache_dir=tmp_path, llm_provider="none", llm_rate_limit_per_hour=2)
    svc = CoachService(s)
    try:
        assert svc.allow_llm("a"), "no provider configured: nothing to protect"
        svc.provider = ScriptedProvider([])
        now = [1_000_000.0]
        monkeypatch.setattr("coach.service.time.time", lambda: now[0])
        assert svc.allow_llm("a") and svc.allow_llm("a") and not svc.allow_llm("a")
        assert svc.allow_llm("b"), "limits are per client"
        now[0] += 3601
        assert svc.allow_llm("a"), "old calls fall out of the one-hour window"
    finally:
        svc.close()


def test_eval_harness_metrics_and_summary():
    from evals.run_eval import _metrics, summarize

    v = {"score": 0.5, "checked": 2, "citations": {"total": 0, "ok": 0, "failures": []}, "moves": {"total": 2, "ok": 1, "failures": ["Nxh8"]}, "recommendations": {"total": 0, "ok": 0, "failures": []}, "evals": {"total": 0, "ok": 0, "failures": []}, "flagged": ["Nxh8"]}
    m = _metrics(v, "one two three", 1.26, {"input_tokens": 10, "output_tokens": 5})
    assert m["citation_validity"] is None and m["move_legality"] == 0.5 and m["words"] == 3 and m["latency_s"] == 1.3 and m["tokens"] == 15
    row = {"player": "p", "games": 6, "run": 1, "ungrounded": m, "grounded": dict(m, grounding_score=1.0, flagged=[]), "grounded_repair": dict(m, grounding_score=1.0, flagged=[])}
    md = summarize({"model": "m", "date": "d", "rows": [row, dict(row, run=2)]})
    assert "| grounding_score | 0.5 | 1.0 | 1.0 |" in md and "| citation_validity | None | None | None |" in md
    assert md.count("- p / ") == 3, "flagged items are listed once per condition for the first run only"
    assert "p (6 games)" in md
