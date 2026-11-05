import chess

from coach.engine import aggregate_accuracy, classify, fmt_eval, move_accuracy, win_pct, win_pct_from_cp


def test_win_probability_model():
    assert win_pct_from_cp(0) == 50.0
    assert abs(win_pct_from_cp(300) + win_pct_from_cp(-300) - 100) < 1e-9  # symmetric
    assert win_pct_from_cp(100) < win_pct_from_cp(300) < win_pct_from_cp(900)
    assert win_pct(None, 3) == 100.0 and win_pct(None, -2) == 0.0


def test_classification_bands_follow_chesscom_expected_points():
    assert classify(25, False) == "blunder"
    assert classify(12, False) == "mistake"
    assert classify(6, False) == "inaccuracy"
    assert classify(2, False) == "good"
    assert classify(40, True) == "best"


def test_accuracy_helpers():
    assert move_accuracy(0) > 99.9
    assert move_accuracy(30) < move_accuracy(10)
    assert aggregate_accuracy([]) is None
    # one disaster drags the harmonic component down more than the arithmetic mean
    assert aggregate_accuracy([100, 100, 100, 10]) < 77.5


def test_eval_formatting():
    assert fmt_eval(170, None) == "+1.7"
    assert fmt_eval(-40, None) == "-0.4"
    assert fmt_eval(None, 3) == "#3"
    assert fmt_eval(None, -2) == "#-2"


def test_analysis_is_internally_consistent(session):
    assert len(session.games) == 6
    for g in session.games:
        a = session.analyses[g.gid]
        assert len(a.evals) == len(g.plies) + 1
        assert len(a.moves) == len(g.plies)
        for m in a.moments:
            ply = g.plies[m.ply]
            assert m.color == g.player_color, "critical moments are only the coached player's moves"
            assert m.played_san == ply.san and m.ref == f"{g.gid} {ply.label}"
            assert m.win_drop >= 5 and m.classification in ("inaccuracy", "mistake", "blunder")
            board = chess.Board(m.fen_before)
            for line in m.best_lines:  # every engine line replays legally
                b = board.copy()
                for u in line.uci:
                    mv = chess.Move.from_uci(u)
                    assert b.is_legal(mv)
                    b.push(mv)
            assert m.best_san != m.played_san
            assert a.moves[m.ply].classification == m.classification


def test_clock_tags_follow_the_pgn(session):
    """Clock-based tags come from the PGN, so they must match the recorded clocks exactly."""
    seen = 0
    for g in session.games:
        for m in session.analyses[g.gid].moments:
            pressure = m.clock < max(15.0, 0.10 * g.base_s)
            assert ("time_pressure" in m.tags) == pressure
            quick = not pressure and m.time_spent is not None and m.time_spent <= 2.0
            assert ("played_quickly" in m.tags) == quick
            seen += 1
    assert seen >= 6


def test_profile_numbers_add_up(session):
    p = session.profile
    assert p["sample"]["games"] == 6
    r = p["sample"]["record"]
    assert r["win"] + r["loss"] + r["draw"] == 6
    totals = p["errors"]["totals"]
    by_phase = p["errors"]["by_phase"]
    assert totals["blunder"] == sum(v["blunders"] for v in by_phase.values())
    assert totals["mistake"] == sum(v["mistakes"] for v in by_phase.values())
    assert p["ratings"]["blitz"]["current"] == 2885


def test_analysis_is_reproducible(service, settings):
    """Node/depth limits + a hash reset per game: the same game always gets the same verdicts."""
    from coach.chesscom import FixtureClient
    from coach.engine import analyse_game
    from coach.pgn import parse_game

    raw = FixtureClient(settings.fixtures_dir).recent_games("gothamchess", 6)[2]
    g = parse_game(raw, "gothamchess", "G3")
    runs = [analyse_game(service.pool, g, settings.engine_depth, settings.deep_nodes, settings.refutation_nodes) for _ in range(2)]
    sig = [[(m.ref, m.best_san, m.eval_before, m.eval_after, tuple(m.tags)) for m in r.moments] for r in runs]
    assert sig[0] == sig[1]
