"""A coaching session: one player's analysed games plus the conversation about them."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import chess

from .engine import CriticalMoment, GameAnalysis
from .pgn import ParsedGame, Ply

REF_RE = re.compile(r"^\s*(G\d+)\s+(\d+)\s*(\.\.\.|\.)\s*([^\s\]]*)\s*$")
_ANNOT = re.compile(r"[!?+#]+$")


def clean_san(san: str) -> str:
    return _ANNOT.sub("", san.strip())


@dataclass
class RefTarget:
    game: ParsedGame
    ply: Ply
    san_matches: bool  # the SAN written in the reference equals the move actually played (or none was given)


@dataclass
class CoachSession:
    sid: str
    username: str
    games: list[ParsedGame]
    analyses: dict[str, GameAnalysis]
    profile: dict
    engine_name: str = "Stockfish"
    source: str = "live"  # live | fixture
    created: float = field(default_factory=time.time)
    report: dict | None = None
    chat_history: list[dict] = field(default_factory=list)  # neutral, text-only turns

    def game(self, gid: str) -> ParsedGame | None:
        gid = gid.strip().upper()
        return next((g for g in self.games if g.gid == gid), None)

    def moments(self) -> list[CriticalMoment]:
        return [m for g in self.games for m in self.analyses[g.gid].moments]

    def moment_at(self, gid: str, ply_index: int) -> CriticalMoment | None:
        return next((m for m in self.analyses[gid].moments if m.ply == ply_index), None)

    def resolve_ref(self, ref: str) -> RefTarget | None:
        """'G1 22...Qc7' -> the game and ply it points at, or None if no such move exists."""
        m = REF_RE.match(ref or "")
        if not m:
            return None
        gid, num, dots, san = m.group(1).upper(), int(m.group(2)), m.group(3), m.group(4)
        game = self.game(gid)
        if not game:
            return None
        ply = game.find_ply(num, "black" if dots == "..." else "white")
        if not ply:
            return None
        return RefTarget(game=game, ply=ply, san_matches=(not san) or clean_san(san) == clean_san(ply.san))

    def evidence_fens(self, target: RefTarget) -> list[str]:
        """Every position a statement anchored to this move may legitimately talk about."""
        fens = [target.ply.fen_before, target.ply.fen_after]
        g = target.game
        # The real game around the moment (a few moves either side).
        for p in g.plies[max(0, target.ply.index - 4) : target.ply.index + 7]:
            fens.append(p.fen_before)
            fens.append(p.fen_after)
        moment = self.moment_at(g.gid, target.ply.index)
        if moment:
            for line in moment.best_lines:
                fens.extend(_line_fens(moment.fen_before, line.uci))
            if moment.refutation:
                fens.extend(_line_fens(moment.fen_after, moment.refutation.uci))
        return list(dict.fromkeys(fens))


def _line_fens(start_fen: str, ucis: list[str]) -> list[str]:
    b = chess.Board(start_fen)
    out = [b.fen()]
    for u in ucis:
        mv = chess.Move.from_uci(u)
        if not b.is_legal(mv):
            break
        b.push(mv)
        out.append(b.fen())
    return out
