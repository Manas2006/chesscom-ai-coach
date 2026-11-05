"""Post-hoc grounding checker for LLM output.

It re-reads the coach's answer and checks four kinds of claims mechanically:

  citations        [[G1 22...Qc7]] must point at a real move that was actually played in that game
  move mentions    every SAN move written in prose must be legal in a position the answer is talking
                   about (the cited moment, its engine lines, the real game continuation, or a position
                   a tool analysed during this answer)
  recommendations  a move presented as "better / best / should have played" must be one of the
                   engine's top choices at that moment, or have been checked with evaluate_move
  evaluations      numbers like +1.7 or #3 must appear in the engine data the model was given

The result is shown to the player as a grounding badge, unsupported tokens are highlighted, and
the agent gets one automatic chance to repair them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import chess

from .session import CoachSession, clean_san

CITE_RE = re.compile(r"\[\[\s*(G\d+\s+\d+\s*(?:\.\.\.|\.)\s*[^\]\s]+)\s*\]\]")
# Piece moves, captures, castling and promotions are unambiguous. Bare pawn pushes ("e4") are only
# treated as moves when written with a move number ("12.e4"), since "e4" is usually a square.
SAN_RE = re.compile(
    r"(?<![\w\[/.-])"
    r"(?:(\d+)\s?(\.\.\.|\.)\s?|\.\.\.)?"
    r"(O-O-O|O-O|[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h]x[a-h][1-8](?:=[QRBN])?|[a-h][1-8]=[QRBN]|[a-h][1-8])"
    r"([+#])?"
    r"(?![\w-])"
)
EVAL_RE = re.compile(r"(?<![\w.\d])([+-]\d{1,2}\.\d|#-?\d{1,2})(?![\d])")
RECOMMEND_RE = re.compile(
    r"\b(better|best|instead|should have|should've|stronger|preferable|correct|engine (?:wanted|prefers|preferred|liked|suggests)|"
    r"was winning with|wins with|was the move|the right move|recommended|improvement)\b",
    re.I,
)


@dataclass
class Evidence:
    """Positions and evaluations produced by tool calls during this answer."""

    fens: set[str] = field(default_factory=set)
    endorsed: set[tuple[str, str]] = field(default_factory=set)  # (fen, san) judged OK by evaluate_move
    texts: list[str] = field(default_factory=list)


@dataclass
class Check:
    total: int = 0
    ok: int = 0
    failures: list[str] = field(default_factory=list)

    def add(self, ok: bool, label: str) -> None:
        self.total += 1
        if ok:
            self.ok += 1
        elif label not in self.failures:
            self.failures.append(label)

    def as_dict(self) -> dict:
        return {"total": self.total, "ok": self.ok, "failures": self.failures}


def _paragraphs(text: str) -> list[str]:
    # Bullet items and paragraphs are the natural "claim units" for anchoring moves to citations.
    parts = re.split(r"\n\s*\n|\n(?=\s*(?:[-*]|\d+\.)\s)|\n(?=#)", text)
    return [p for p in parts if p.strip()]


def _sentences(par: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\[(])", par) if s.strip()]


def _legal_in(san: str, fens: list[str] | set[str]) -> str | None:
    target = clean_san(san)
    for fen in fens:
        b = chess.Board(fen)
        try:
            mv = b.parse_san(target)
        except ValueError:
            continue
        if clean_san(b.san(mv)) == target or target in ("O-O", "O-O-O"):
            return fen
    return None


def _in_engine_lines(session: CoachSession, anchors: list, san: str, evidence: Evidence) -> bool:
    target = clean_san(san)
    for t in anchors:
        m = session.moment_at(t.game.gid, t.ply.index)
        if not m:
            continue
        sans = [x for l in m.best_lines for x in l.san] + (m.refutation.san if m.refutation else [])
        sans += [c.split(".")[-1] for c in m.continuation]
        if target in {clean_san(x) for x in sans}:
            return True
    return any(re.search(rf"(?<![\w]){re.escape(target)}(?![\w])", txt) for txt in evidence.texts)


def verify(text: str, session: CoachSession, context_text: str, evidence: Evidence | None = None) -> dict:
    evidence = evidence or Evidence()
    citations, moves, recs, evals = Check(), Check(), Check(), Check()
    all_moment_fens: set[str] = set()
    for g in session.games:
        for m in session.analyses[g.gid].moments:
            target = session.resolve_ref(m.ref)
            if target:
                all_moment_fens.update(session.evidence_fens(target))

    for par in _paragraphs(text):
        anchors = []
        for cm in CITE_RE.finditer(par):
            ref = re.sub(r"\s+", " ", cm.group(1)).strip()
            target = session.resolve_ref(ref)
            if target is None:
                citations.add(False, f"[[{ref}]] (no such move in that game)")
            elif not target.san_matches:
                citations.add(False, f"[[{ref}]] (the move played was {target.ply.label})")
                anchors.append(target)
            else:
                citations.add(True, ref)
                anchors.append(target)
        anchor_fens: list[str] = []
        for t in anchors:
            anchor_fens.extend(session.evidence_fens(t))
        stripped = CITE_RE.sub(" ", par)

        for sent in _sentences(stripped):
            recommending = bool(RECOMMEND_RE.search(sent))
            for sm in SAN_RE.finditer(sent):
                num, dots, san, suffix = sm.group(1), sm.group(2), sm.group(3), sm.group(4) or ""
                if re.fullmatch(r"[a-h][1-8]", san) and not num:
                    continue  # a square, not a move
                token = sm.group(0).strip()
                pool = anchor_fens or list(all_moment_fens)
                fen = _legal_in(san, pool) or _legal_in(san, evidence.fens)
                if fen is None and not anchor_fens:
                    # Unanchored mention: accept it if it is legal anywhere in the analysed games.
                    fen = _legal_in(san, [p.fen_before for g in session.games for p in g.plies])
                moves.add(fen is not None, token)
                if recommending and fen is not None and anchors:
                    endorsed = False
                    for t in anchors:
                        moment = session.moment_at(t.game.gid, t.ply.index)
                        if moment and any(l.san and clean_san(l.san[0]) == clean_san(san) for l in moment.best_lines):
                            endorsed = True
                        if (t.ply.fen_before, clean_san(san)) in evidence.endorsed:
                            endorsed = True
                        if clean_san(t.ply.san) == clean_san(san):
                            endorsed = True  # discussing the played move itself, not recommending it
                    if any(_legal_in(san, [t.ply.fen_before]) for t in anchors):
                        # An alternative at the decision point: must be an engine top choice or tool-checked.
                        recs.add(endorsed, token)
                    elif not _in_engine_lines(session, anchors, san, evidence):
                        # Legal somewhere nearby but recommended without any engine line containing it.
                        recs.add(False, token)

    grounded_numbers = context_text + "\n" + "\n".join(evidence.texts)
    for em in EVAL_RE.finditer(CITE_RE.sub(" ", text)):
        val = em.group(1)
        evals.add(val in grounded_numbers, val)

    total = citations.total + moves.total + recs.total + evals.total
    ok = citations.ok + moves.ok + recs.ok + evals.ok
    return {
        "score": round(ok / total, 3) if total else 1.0,
        "checked": total,
        "citations": citations.as_dict(),
        "moves": moves.as_dict(),
        "recommendations": recs.as_dict(),
        "evals": evals.as_dict(),
        "flagged": citations.failures + moves.failures + recs.failures + evals.failures,
    }


def problems_for_repair(result: dict) -> str:
    lines = []
    for f in result["citations"]["failures"]:
        lines.append(f"- citation {f}")
    for f in result["moves"]["failures"]:
        lines.append(f"- move '{f}' is not legal in any position this answer refers to")
    for f in result["recommendations"]["failures"]:
        lines.append(f"- '{f}' is recommended but is not one of the engine's top choices there (check it with evaluate_move or drop it)")
    for f in result["evals"]["failures"]:
        lines.append(f"- evaluation '{f}' does not appear in the engine data")
    return "\n".join(lines)
