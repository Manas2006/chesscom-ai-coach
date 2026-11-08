"""Cross-game aggregation. Every number the coach quotes about patterns is computed here, in code,
so the LLM never has to count, average or estimate anything."""

from __future__ import annotations

from collections import Counter, defaultdict

from .engine import GameAnalysis, aggregate_accuracy
from .motifs import TAG_LABELS
from .pgn import ParsedGame

RATING_KEYS = {"chess_bullet": "bullet", "chess_blitz": "blitz", "chess_rapid": "rapid", "chess_daily": "daily"}


def _score(w: int, d: int, n: int) -> float | None:
    return round(100.0 * (w + 0.5 * d) / n, 1) if n else None


def rating_band(rating: int | None) -> str:
    if rating is None:
        return "unknown"
    if rating < 1000:
        return "beginner"
    if rating < 1500:
        return "intermediate"
    if rating < 2000:
        return "advanced club"
    if rating < 2400:
        return "expert"
    return "master"


def build_profile(username: str, profile: dict, stats: dict, games: list[ParsedGame], analyses: dict[str, GameAnalysis]) -> dict:
    profile, stats = profile or {}, stats or {}
    ratings = {}
    for key, label in RATING_KEYS.items():
        s = stats.get(key)
        if isinstance(s, dict) and isinstance(s.get("last"), dict):
            ratings[label] = {
                "current": s["last"].get("rating"),
                "best": (s.get("best") or {}).get("rating"),
                "record": s.get("record", {}),
            }
    record = Counter(g.outcome for g in games)
    by_color: dict[str, dict] = {}
    for color in ("white", "black"):
        gs = [g for g in games if g.player_color == color]
        c = Counter(g.outcome for g in gs)
        by_color[color] = {"games": len(gs), "win": c["win"], "loss": c["loss"], "draw": c["draw"], "score": _score(c["win"], c["draw"], len(gs))}

    all_moves = [m for g in games for m in analyses[g.gid].moves if m.color == g.player_color]
    phase_acc = {}
    phase_err = {}
    for phase in ("opening", "middlegame", "endgame"):
        ms = [m for m in all_moves if m.phase == phase]
        phase_acc[phase] = aggregate_accuracy([m.accuracy for m in ms])
        phase_err[phase] = {
            "moves": len(ms),
            "mistakes": sum(m.classification == "mistake" for m in ms),
            "misses": sum(m.classification == "miss" for m in ms),
            "blunders": sum(m.classification == "blunder" for m in ms),
            "inaccuracies": sum(m.classification == "inaccuracy" for m in ms),
        }
    n = len(games) or 1
    totals = Counter()
    for g in games:
        totals.update(analyses[g.gid].counts)

    moments = [m for g in games for m in analyses[g.gid].moments]
    tag_counts = Counter(t for m in moments for t in m.tags)
    serious = [m for m in moments if m.classification in ("mistake", "miss", "blunder")]
    pressure = [m for m in serious if "time_pressure" in m.tags]
    quick = [m for m in serious if "played_quickly" in m.tags]

    thrown = [g.gid for g in games if analyses[g.gid].peak_win >= 80 and g.outcome != "win"]
    comebacks = [g.gid for g in games if analyses[g.gid].low_win <= 20 and g.outcome != "loss"]
    lost_on_time = [g.gid for g in games if g.outcome == "loss" and g.end_reason == "timeout"]

    openings: dict[tuple[str, str], list[ParsedGame]] = defaultdict(list)
    for g in games:
        openings[(g.player_color, g.opening_family)].append(g)
    opening_rows = []
    for (color, fam), gs in openings.items():
        c = Counter(g.outcome for g in gs)
        opening_rows.append(
            {
                "color": color,
                "opening": fam,
                "games": len(gs),
                "win": c["win"],
                "loss": c["loss"],
                "draw": c["draw"],
                "score": _score(c["win"], c["draw"], len(gs)),
                "opening_accuracy": aggregate_accuracy(
                    [m.accuracy for g in gs for m in analyses[g.gid].moves if m.color == g.player_color and m.phase == "opening"]
                ),
                "gids": [g.gid for g in gs],
            }
        )
    opening_rows.sort(key=lambda r: (-r["games"], r["opening"]))

    worst = sorted(moments, key=lambda m: m.win_drop, reverse=True)[:8]
    ratings_in_sample = [g.player_rating for g in games if g.player_rating]
    avg_rating = round(sum(ratings_in_sample) / len(ratings_in_sample)) if ratings_in_sample else None
    chesscom_accs = [g.chesscom_accuracy for g in games if g.chesscom_accuracy is not None]

    worst_phase = None
    candidates = [(p, a) for p, a in phase_acc.items() if a is not None and phase_err[p]["moves"] >= 15]
    if candidates:
        worst_phase = min(candidates, key=lambda x: x[1])[0]

    return {
        "username": profile.get("username", username),
        "name": profile.get("name"),
        "title": profile.get("title"),
        "avatar": profile.get("avatar"),
        "profile_url": profile.get("url", f"https://www.chess.com/member/{username}"),
        "ratings": ratings,
        "tactics_highest": ((stats.get("tactics") or {}).get("highest") or {}).get("rating"),
        "puzzle_rush_best": ((stats.get("puzzle_rush") or {}).get("best") or {}).get("score"),
        "sample": {
            "games": len(games),
            "time_classes": dict(Counter(g.time_class for g in games)),
            "record": {"win": record["win"], "loss": record["loss"], "draw": record["draw"]},
            "score": _score(record["win"], record["draw"], len(games)),
            "by_color": by_color,
            "avg_rating": avg_rating,
            "rating_band": rating_band(avg_rating),
            "date_range": [min((g.date for g in games), default=""), max((g.date for g in games), default="")],
        },
        "accuracy": {
            "overall": aggregate_accuracy([m.accuracy for m in all_moves]),
            "by_phase": phase_acc,
            "chesscom_avg": round(sum(chesscom_accs) / len(chesscom_accs), 1) if chesscom_accs else None,
            "worst_phase": worst_phase,
        },
        "errors": {
            "per_game": {k: round(totals[k] / n, 2) for k in ("inaccuracy", "mistake", "miss", "blunder")},
            "totals": {k: totals[k] for k in ("inaccuracy", "mistake", "miss", "blunder")},
            "by_phase": phase_err,
        },
        "time": {
            "serious_errors": len(serious),
            "in_time_pressure": len(pressure),
            "played_quickly": len(quick),
            "lost_on_time": lost_on_time,
        },
        "patterns": [
            {"tag": t, "label": TAG_LABELS.get(t, t), "count": c, "refs": [m.ref for m in moments if t in m.tags]}
            for t, c in tag_counts.most_common()
        ],
        "conversion": {"thrown_wins": thrown, "comebacks": comebacks},
        "openings": opening_rows,
        "worst_moments": [m.ref for m in worst],
    }
