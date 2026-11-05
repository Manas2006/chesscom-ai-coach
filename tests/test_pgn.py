from coach.chesscom import FixtureClient, PlayerNotFound, normalize_username
from coach.config import ROOT
from coach.pgn import opening_from_eco_url, parse_game, parse_time_control

import pytest


def test_time_control_parsing():
    assert parse_time_control("180") == (180.0, 0.0)
    assert parse_time_control("180+2") == (180.0, 2.0)
    assert parse_time_control("1/86400") == (None, 0.0)
    assert parse_time_control("") == (None, 0.0)


@pytest.mark.parametrize(
    "url,name,family",
    [
        ("https://www.chess.com/openings/Caro-Kann-Defense-Exchange-Variation...5.c3-Qc7-6.Ne2-Bg4", "Caro Kann Defense Exchange Variation", "Caro Kann Defense"),
        ("https://www.chess.com/openings/Queens-Pawn-Opening-1...c6-2.Nf3", "Queens Pawn Opening", "Queens Pawn Opening"),
        ("https://www.chess.com/openings/Modern-Defense-with-1-d4-2.Nf3-Bg7-3.c3", "Modern Defense with 1 d4", "Modern Defense"),
        ("https://www.chess.com/openings/Undefined", "Unknown opening", "Unknown opening"),
        (None, "Unknown opening", "Unknown opening"),
    ],
)
def test_opening_names(url, name, family):
    assert opening_from_eco_url(url) == (name, family)


def test_username_validation():
    assert normalize_username(" @GothamChess ") == "gothamchess"
    with pytest.raises(PlayerNotFound):
        normalize_username("bad name!")


def test_parse_real_game_from_the_player_perspective():
    fc = FixtureClient(ROOT / "fixtures")
    raws = fc.recent_games("gothamchess", 6)
    assert len(raws) == 6
    assert raws[0]["end_time"] >= raws[-1]["end_time"]  # most recent first
    g = parse_game(raws[0], "GothamChess", "G1")
    assert g.player_color == "black" and g.opponent == "Brown_Mamba24"
    assert g.outcome == "loss" and g.end_reason == "resigned"
    assert g.base_s == 180 and g.is_live
    assert len(g.plies) == 64
    first = g.plies[0]
    assert first.label == "1.Nf3" and first.clock == 177.2 and first.time_spent == 2.8
    black = g.plies[1]
    assert black.label == "1...d5" and black.color == "black"
    assert g.find_ply(22, "black").san == "Qc7"


def test_only_standard_chess_is_kept():
    fc = FixtureClient(ROOT / "fixtures")
    assert all(g["rules"] == "chess" for g in fc.recent_games("gothamchess", 10))
    assert fc.recent_games("gothamchess", 10, "rapid") == []
