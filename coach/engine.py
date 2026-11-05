"""Stockfish analysis: the single source of truth for every chess fact the coach states.

Two passes per game:
  1. fast pass: every position at a fixed depth -> eval curve, best move, per-move classification
  2. deep pass: only the player's worst moves -> top-3 engine lines + the opponent's punishing reply
Both passes are depth- or node-limited with a hash reset per game, so results are reproducible.

Evaluations are converted to win probability with Lichess's published logistic fit, and moves are
classified by expected points lost using Chess.com's Game Review bands (5 / 10 / 20 points).
"""

from __future__ import annotations

import math
import queue
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Iterator

import chess
import chess.engine

from .pgn import ParsedGame, Ply

MATE_CP = 10_000
PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
PIECE_NAMES = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop", chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}
THRESHOLDS = (("blunder", 20.0), ("mistake", 10.0), ("inaccuracy", 5.0))
ANALYSIS_VERSION = 5  # bump to invalidate cached analyses when the logic changes


# ---------------------------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------------------------
def win_pct_from_cp(cp: float) -> float:
    """Lichess win% model: https://lichess.org/page/accuracy"""
    cp = max(-1000.0, min(1000.0, cp))
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(-0.00368208 * cp)) - 1.0)


def win_pct(cp: int | None, mate: int | None) -> float:
    if mate is not None:
        return 100.0 if mate > 0 else 0.0
    return win_pct_from_cp(cp or 0)


def move_accuracy(win_drop: float) -> float:
    """Lichess per-move accuracy from win% lost."""
    return max(0.0, min(100.0, 103.1668 * math.exp(-0.04354 * max(0.0, win_drop)) - 3.1669))


def aggregate_accuracy(values: list[float]) -> float | None:
    """Average of arithmetic and harmonic means (harmonic punishes single disasters, like Lichess)."""
    if not values:
        return None
    arith = sum(values) / len(values)
    harm = len(values) / sum(1.0 / max(v, 1.0) for v in values)
    return round((arith + harm) / 2.0, 1)


def classify(win_drop: float, is_best: bool) -> str:
    if is_best:
        return "best"
    for name, threshold in THRESHOLDS:
        if win_drop >= threshold:
            return name
    return "good"


def fmt_eval(cp: int | None, mate: int | None) -> str:
    """Human eval string from a given side's point of view: +1.3, -0.4, #3 (mate for us), #-2."""
    if mate is not None:
        return f"#{mate}" if mate > 0 else f"#-{abs(mate)}"
    return f"{(cp or 0) / 100:+.1f}"


def material(board: chess.Board, color: chess.Color) -> int:
    return sum(PIECE_VALUES[p.piece_type] for p in board.piece_map().values() if p.color == color)


def non_pawn_material(board: chess.Board) -> int:
    return sum(PIECE_VALUES[p.piece_type] for p in board.piece_map().values() if p.piece_type not in (chess.PAWN, chess.KING))


def game_phase(board: chess.Board) -> str:
    if non_pawn_material(board) <= 26:
        return "endgame"
    if board.fullmove_number <= 12:
        return "opening"
    return "middlegame"


# ---------------------------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------------------------
@dataclass
class PosEval:
    cp: int | None  # white point of view
    mate: int | None  # white point of view
    best_uci: str | None
    best_san: str | None
    terminal: str | None = None  # checkmate | draw

    def pov(self, color: str) -> tuple[int | None, int | None]:
        sign = 1 if color == "white" else -1
        return (None if self.cp is None else sign * self.cp, None if self.mate is None else sign * self.mate)

    def win(self, color: str) -> float:
        if self.terminal == "draw":
            return 50.0
        cp, mate = self.pov(color)
        return win_pct(cp, mate)


@dataclass
class EngineLine:
    san: list[str]
    uci: list[str]
    cp: int | None  # from the point of view of `perspective`
    mate: int | None
    perspective: str  # whose point of view the score is from

    @property
    def win(self) -> float:
        return win_pct(self.cp, self.mate)

    @property
    def eval_str(self) -> str:
        return fmt_eval(self.cp, self.mate)


@dataclass
class MoveAnalysis:
    ply: int
    color: str
    label: str
    san: str
    win_before: float  # mover's win%
    win_after: float
    win_drop: float
    eval_before: str  # mover's POV
    eval_after: str
    best_san: str | None
    classification: str
    accuracy: float
    phase: str


@dataclass
class CriticalMoment:
    ref: str  # citation id, e.g. "G1 24.Nxd4" (also the exact string the LLM must cite)
    gid: str
    ply: int
    move_number: int
    color: str
    played_san: str
    played_uci: str
    fen_before: str
    fen_after: str
    classification: str
    win_before: float
    win_after: float
    win_drop: float
    eval_before: str
    eval_after: str
    phase: str
    clock: float | None
    time_spent: float | None
    best_lines: list[EngineLine]
    refutation: EngineLine | None
    continuation: list[str]  # what actually happened next in the game (SAN)
    tags: list[str] = field(default_factory=list)
    tag_notes: list[str] = field(default_factory=list)

    @property
    def best_san(self) -> str | None:
        return self.best_lines[0].san[0] if self.best_lines and self.best_lines[0].san else None


@dataclass
class GameAnalysis:
    gid: str
    depth: int
    evals: list[PosEval]
    moves: list[MoveAnalysis]
    moments: list[CriticalMoment]
    accuracy: float | None
    opponent_accuracy: float | None
    counts: dict[str, int]
    phase_stats: dict[str, dict]
    peak_win: float  # player's best win% after move 10
    low_win: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GameAnalysis":
        def line(x):
            return EngineLine(**x) if x else None

        moments = []
        for m in d["moments"]:
            m = dict(m)
            m["best_lines"] = [EngineLine(**x) for x in m["best_lines"]]
            m["refutation"] = line(m["refutation"])
            moments.append(CriticalMoment(**m))
        return cls(
            gid=d["gid"],
            depth=d["depth"],
            evals=[PosEval(**e) for e in d["evals"]],
            moves=[MoveAnalysis(**m) for m in d["moves"]],
            moments=moments,
            accuracy=d["accuracy"],
            opponent_accuracy=d["opponent_accuracy"],
            counts=d["counts"],
            phase_stats=d["phase_stats"],
            peak_win=d["peak_win"],
            low_win=d["low_win"],
        )


# ---------------------------------------------------------------------------------------------
# Engine pool
# ---------------------------------------------------------------------------------------------
class EnginePool:
    """A fixed set of Stockfish processes shared by worker threads (python-chess engines are not thread-safe)."""

    def __init__(self, path: str | None, workers: int = 2, hash_mb: int = 64):
        if not path:
            raise RuntimeError(
                "Stockfish not found. Install it (macOS: `brew install stockfish`, Debian/Ubuntu: "
                "`apt install stockfish`) or set STOCKFISH_PATH, or run with `docker compose up`."
            )
        self.path, self.workers, self.hash_mb = path, workers, hash_mb
        self._q: queue.Queue[chess.engine.SimpleEngine] = queue.Queue()
        self._all: list[chess.engine.SimpleEngine] = []
        self._lock = threading.Lock()

    def _spawn(self) -> chess.engine.SimpleEngine:
        eng = chess.engine.SimpleEngine.popen_uci(self.path)
        eng.configure({"Threads": 1, "Hash": self.hash_mb})
        return eng

    @contextmanager
    def engine(self) -> Iterator[chess.engine.SimpleEngine]:
        with self._lock:
            if self._q.empty() and len(self._all) < self.workers:
                eng = self._spawn()
                self._all.append(eng)
                self._q.put(eng)
        eng = self._q.get()
        try:
            yield eng
        except chess.engine.EngineTerminatedError:
            with self._lock:
                self._all.remove(eng)
                eng = self._spawn()
                self._all.append(eng)
            raise
        finally:
            self._q.put(eng)

    def version(self) -> str:
        with self.engine() as eng:
            return eng.id.get("name", "Stockfish")

    def close(self) -> None:
        for eng in self._all:
            try:
                eng.quit()
            except Exception:
                pass
        self._all.clear()


# ---------------------------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------------------------
def _terminal_eval(board: chess.Board) -> PosEval | None:
    if board.is_checkmate():
        # side to move is mated
        return PosEval(cp=-MATE_CP if board.turn == chess.WHITE else MATE_CP, mate=None, best_uci=None, best_san=None, terminal="checkmate")
    if board.is_stalemate() or board.is_insufficient_material() or board.can_claim_fifty_moves():
        return PosEval(cp=0, mate=None, best_uci=None, best_san=None, terminal="draw")
    return None


def _pos_eval(info: dict, board: chess.Board) -> PosEval:
    score = info["score"].white()
    pv = info.get("pv") or []
    best = pv[0] if pv else None
    return PosEval(
        cp=score.score(),
        mate=score.mate(),
        best_uci=best.uci() if best else None,
        best_san=board.san(best) if best else None,
    )


def evaluate_positions(eng: chess.engine.SimpleEngine, game: ParsedGame, depth: int) -> list[PosEval]:
    """Fixed-depth pass. Passing `game=` makes python-chess send `ucinewgame` (hash reset) when the game changes."""
    board = chess.Board(game.start_fen)
    out: list[PosEval] = []
    positions = [board.copy()]
    for p in game.plies:
        board.push_uci(p.uci)
        positions.append(board.copy())
    for b in positions:
        term = _terminal_eval(b)
        if term:
            out.append(term)
            continue
        info = eng.analyse(b, chess.engine.Limit(depth=depth), game=game.uuid)
        out.append(_pos_eval(info, b))
    return out


def line_from_info(info: dict, board: chess.Board, perspective: str, max_plies: int = 6) -> EngineLine:
    pv = (info.get("pv") or [])[:max_plies]
    san, uci, b = [], [], board.copy()
    for mv in pv:
        if not b.is_legal(mv):
            break
        san.append(b.san(mv))
        uci.append(mv.uci())
        b.push(mv)
    score = info["score"].pov(chess.WHITE if perspective == "white" else chess.BLACK)
    return EngineLine(san=san, uci=uci, cp=score.score(), mate=score.mate(), perspective=perspective)


def analyse_lines(eng, board: chess.Board, perspective: str, nodes: int, multipv: int = 1, game: object = None) -> list[EngineLine]:
    if board.is_game_over():
        return []
    infos = eng.analyse(board, chess.engine.Limit(nodes=nodes), multipv=multipv, game=game)
    if isinstance(infos, dict):
        infos = [infos]
    return [line_from_info(i, board, perspective) for i in infos if i.get("pv")]


def _move_analyses(game: ParsedGame, evals: list[PosEval]) -> list[MoveAnalysis]:
    moves = []
    for p in game.plies:
        before, after = evals[p.index], evals[p.index + 1]
        wb, wa = before.win(p.color), after.win(p.color)
        drop = round(max(0.0, wb - wa), 1)
        is_best = before.best_uci == p.uci
        cls = classify(drop, is_best)
        cpb, mb = before.pov(p.color)
        cpa, ma = after.pov(p.color)
        board = chess.Board(p.fen_before)
        moves.append(
            MoveAnalysis(
                ply=p.index,
                color=p.color,
                label=p.label,
                san=p.san,
                win_before=round(wb, 1),
                win_after=round(wa, 1),
                win_drop=drop,
                eval_before=fmt_eval(cpb, mb) if not before.terminal else "",
                eval_after=fmt_eval(cpa, ma) if after.terminal != "checkmate" else ("#" if wa == 100 else "#-0"),
                best_san=before.best_san,
                classification=cls,
                accuracy=round(move_accuracy(0.0 if is_best else drop), 1),
                phase=game_phase(board),
            )
        )
    return moves


def _phase_stats(moves: list[MoveAnalysis]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for phase in ("opening", "middlegame", "endgame"):
        ms = [m for m in moves if m.phase == phase]
        stats[phase] = {
            "moves": len(ms),
            "accuracy": aggregate_accuracy([m.accuracy for m in ms]),
            "inaccuracy": sum(m.classification == "inaccuracy" for m in ms),
            "mistake": sum(m.classification == "mistake" for m in ms),
            "blunder": sum(m.classification == "blunder" for m in ms),
        }
    return stats


def pick_candidate_moments(moves: list[MoveAnalysis], color: str, limit: int = 3) -> list[MoveAnalysis]:
    errs = [m for m in moves if m.color == color and m.classification in ("mistake", "blunder")]
    if not errs:
        errs = [m for m in moves if m.color == color and m.classification == "inaccuracy"]
    return sorted(errs, key=lambda m: m.win_drop, reverse=True)[:limit]


def build_moment(
    eng, game: ParsedGame, ply: Ply, ma: MoveAnalysis, deep_nodes: int, refutation_nodes: int
) -> tuple[CriticalMoment | None, float]:
    """Deep-analyse one candidate. Returns (moment or None if the deep search clears the move, deep win% drop)."""
    from .motifs import tag_moment  # local import to avoid a cycle

    before = chess.Board(ply.fen_before)
    after = chess.Board(ply.fen_after)
    opp = "black" if ply.color == "white" else "white"
    best_lines = analyse_lines(eng, before, ply.color, deep_nodes, multipv=3, game=game.uuid)
    refutation_lines = analyse_lines(eng, after, ply.color, refutation_nodes, multipv=1, game=game.uuid)
    refutation = refutation_lines[0] if refutation_lines else None

    win_before, win_after, win_drop = ma.win_before, ma.win_after, ma.win_drop
    eval_after = ma.eval_after
    if best_lines:
        # Guard against fast-pass noise: re-measure the loss with the deeper search. If the played
        # move is the deep best move, or costs < 5 points against it, it was not a real error.
        best_win = best_lines[0].win
        played = next((l for l in best_lines if l.uci and l.uci[0] == ply.uci), None)
        played_win = played.win if played else (refutation.win if refutation else ma.win_after)
        win_before, win_after = round(best_win, 1), round(played_win, 1)
        win_drop = round(max(0.0, best_win - played_win), 1)
        if (best_lines[0].uci and best_lines[0].uci[0] == ply.uci) or win_drop < 5.0:
            return None, win_drop
        if played:
            eval_after = played.eval_str
        elif refutation:
            eval_after = refutation.eval_str

    continuation = []
    for p in game.plies[ply.index + 1 : ply.index + 5]:
        continuation.append(p.label)

    moment = CriticalMoment(
        ref=f"{game.gid} {ply.label}",
        gid=game.gid,
        ply=ply.index,
        move_number=ply.move_number,
        color=ply.color,
        played_san=ply.san,
        played_uci=ply.uci,
        fen_before=ply.fen_before,
        fen_after=ply.fen_after,
        classification=classify(win_drop, False),
        win_before=win_before,
        win_after=win_after,
        win_drop=win_drop,
        eval_before=best_lines[0].eval_str if best_lines else ma.eval_before,
        eval_after=eval_after,
        phase=ma.phase,
        clock=ply.clock,
        time_spent=ply.time_spent,
        best_lines=best_lines,
        refutation=refutation,
        continuation=continuation,
    )
    tag_moment(moment, game, before, after, opp)
    return moment, win_drop


def analyse_game(pool: EnginePool, game: ParsedGame, depth: int, deep_nodes: int, refutation_nodes: int) -> GameAnalysis:
    with pool.engine() as eng:
        evals = evaluate_positions(eng, game, depth)
        moves = _move_analyses(game, evals)
        moments: list[CriticalMoment] = []
        for ma in pick_candidate_moments(moves, game.player_color, limit=5):
            moment, deep_drop = build_moment(eng, game, game.plies[ma.ply], ma, deep_nodes, refutation_nodes)
            # The deep pass is the better measurement: write it back so per-move stats, the eval
            # chart labels and the critical moments never disagree.
            ma.win_drop = deep_drop
            ma.accuracy = round(move_accuracy(deep_drop), 1)
            ma.classification = moment.classification if moment else classify(deep_drop, deep_drop == 0.0)
            if moment:
                moments.append(moment)
            if len(moments) >= 3:
                break
    mine = [m for m in moves if m.color == game.player_color]
    theirs = [m for m in moves if m.color != game.player_color]
    counts = {c: sum(m.classification == c for m in mine) for c in ("best", "good", "inaccuracy", "mistake", "blunder")}
    after_opening = [m.win_after for m in mine if int(m.label.split(".")[0]) > 10] or [m.win_after for m in mine] or [50.0]
    return GameAnalysis(
        gid=game.gid,
        depth=depth,
        evals=evals,
        moves=moves,
        moments=sorted(moments, key=lambda m: m.ply),
        accuracy=aggregate_accuracy([m.accuracy for m in mine]),
        opponent_accuracy=aggregate_accuracy([m.accuracy for m in theirs]),
        counts=counts,
        phase_stats=_phase_stats(mine),
        peak_win=round(max(after_opening), 1),
        low_win=round(min(after_opening), 1),
    )
