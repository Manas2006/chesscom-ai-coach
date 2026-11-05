"""Turn a raw Chess.com game object into a typed, player-centric structure."""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import chess
import chess.pgn

DRAW_CODES = {"agreed", "repetition", "stalemate", "insufficient", "50move", "timevsinsufficient"}
FAMILY_WORDS = ("Opening", "Defense", "Defence", "Attack", "Game", "Gambit", "System")
_MOVE_NUM = re.compile(r"(?:-|\.\.\.)\d+\.")


@dataclass
class Ply:
    index: int  # 0-based ply index
    move_number: int  # full-move number
    color: str  # "white" | "black"
    san: str
    uci: str
    fen_before: str
    fen_after: str
    clock: float | None  # seconds left on the mover's clock after the move
    time_spent: float | None  # seconds the mover spent on this move

    @property
    def label(self) -> str:
        return f"{self.move_number}.{self.san}" if self.color == "white" else f"{self.move_number}...{self.san}"


@dataclass
class ParsedGame:
    gid: str
    url: str
    uuid: str
    date: str
    end_time: int
    time_class: str
    time_control: str
    base_s: float | None
    inc_s: float
    rated: bool
    white: str
    black: str
    white_rating: int | None
    black_rating: int | None
    player: str
    player_color: str
    opponent: str
    player_rating: int | None
    opponent_rating: int | None
    outcome: str  # win | loss | draw (from the player's side)
    end_reason: str  # checkmated, resigned, timeout, agreed, ...
    termination: str
    opening: str
    opening_family: str
    eco: str
    chesscom_accuracy: float | None
    start_fen: str
    plies: list[Ply] = field(default_factory=list)

    @property
    def is_live(self) -> bool:
        return self.time_class != "daily" and self.base_s is not None

    def player_plies(self) -> list[Ply]:
        return [p for p in self.plies if p.color == self.player_color]

    def find_ply(self, move_number: int, color: str) -> Ply | None:
        for p in self.plies:
            if p.move_number == move_number and p.color == color:
                return p
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["plies"] = [asdict(p) | {"label": p.label} for p in self.plies]
        return d


def parse_time_control(tc: str) -> tuple[float | None, float]:
    """'180' -> (180, 0); '180+2' -> (180, 2); daily '1/86400' -> (None, 0)."""
    tc = (tc or "").strip()
    if not tc or "/" in tc or tc == "-":
        return None, 0.0
    base, _, inc = tc.partition("+")
    try:
        return float(base), float(inc or 0)
    except ValueError:
        return None, 0.0


def opening_from_eco_url(eco_url: str | None) -> tuple[str, str]:
    """Chess.com encodes the opening as a slug: '.../openings/Caro-Kann-Defense-Exchange-Variation...5.c3'."""
    if not eco_url or "/openings/" not in eco_url:
        return "Unknown opening", "Unknown opening"
    slug = eco_url.split("/openings/", 1)[1]
    m = _MOVE_NUM.search(slug)
    if m:
        slug = slug[: m.start()]
    name = slug.replace("-", " ").strip() or "Unknown opening"
    if name.lower() == "undefined":
        name = "Unknown opening"
    words = name.split()
    family = name
    for i, w in enumerate(words):
        if w in FAMILY_WORDS:
            family = " ".join(words[: i + 1])
            break
    return name, family


def _outcome(player_side: dict, opp_side: dict) -> tuple[str, str]:
    code = player_side.get("result", "")
    if code == "win":
        return "win", opp_side.get("result", "unknown")
    if code in DRAW_CODES:
        return "draw", code
    return "loss", code or "unknown"


def parse_game(raw: dict, username: str, gid: str) -> ParsedGame:
    pgn = chess.pgn.read_game(io.StringIO(raw["pgn"]))
    if pgn is None:
        raise ValueError(f"unparseable PGN for {raw.get('url')}")
    w, b = raw.get("white", {}), raw.get("black", {})
    uname = username.lower()
    if w.get("username", "").lower() == uname:
        color, me, opp = "white", w, b
    elif b.get("username", "").lower() == uname:
        color, me, opp = "black", b, w
    else:
        raise ValueError(f"{username} did not play {raw.get('url')}")
    outcome, reason = _outcome(me, opp)
    base, inc = parse_time_control(raw.get("time_control", ""))
    if raw.get("time_class") == "daily":
        base, inc = None, 0.0
    opening, family = opening_from_eco_url(raw.get("eco") or pgn.headers.get("ECOUrl"))
    acc = (raw.get("accuracies") or {}).get(color)

    board = pgn.board()
    start_fen = board.fen()
    last_clock = {"white": base, "black": base}
    plies: list[Ply] = []
    for i, node in enumerate(pgn.mainline()):
        mover = "white" if board.turn == chess.WHITE else "black"
        fen_before = board.fen()
        move_number = board.fullmove_number
        san = board.san(node.move)
        board.push(node.move)
        clk = node.clock() if base is not None else None
        spent = None
        if clk is not None and last_clock[mover] is not None:
            spent = max(0.0, round(last_clock[mover] - clk + inc, 1))
        if clk is not None:
            last_clock[mover] = clk
        plies.append(
            Ply(
                index=i,
                move_number=move_number,
                color=mover,
                san=san,
                uci=node.move.uci(),
                fen_before=fen_before,
                fen_after=board.fen(),
                clock=clk,
                time_spent=spent,
            )
        )
    end_time = int(raw.get("end_time") or 0)
    date = datetime.fromtimestamp(end_time, tz=timezone.utc).strftime("%Y-%m-%d") if end_time else ""
    return ParsedGame(
        gid=gid,
        url=raw.get("url", ""),
        uuid=raw.get("uuid", raw.get("url", gid)),
        date=date,
        end_time=end_time,
        time_class=raw.get("time_class", "unknown"),
        time_control=raw.get("time_control", ""),
        base_s=base,
        inc_s=inc,
        rated=bool(raw.get("rated", False)),
        white=w.get("username", "?"),
        black=b.get("username", "?"),
        white_rating=w.get("rating"),
        black_rating=b.get("rating"),
        player=me.get("username", username),
        player_color=color,
        opponent=opp.get("username", "?"),
        player_rating=me.get("rating"),
        opponent_rating=opp.get("rating"),
        outcome=outcome,
        end_reason=reason,
        termination=pgn.headers.get("Termination", ""),
        opening=opening,
        opening_family=family,
        eco=pgn.headers.get("ECO", ""),
        chesscom_accuracy=acc,
        start_fen=start_fen,
        plies=plies,
    )
