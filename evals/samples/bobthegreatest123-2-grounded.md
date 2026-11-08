## The big picture
You're a 754-average player with a puzzle rating of 1721, so you can see tactics. The games show you often don't use that skill when it counts. You went 5W 3L, but your middlegame accuracy was only 66.1 (opening 80.9, endgame 78.5). You lost on time in G2, G3 and G4, and you let winning positions slip in G1, G2, G5 and G6.

## Patterns to fix

**1. Playing fast and not checking what your move leaves hanging**
- In G5 you were clearly winning, then played [[G5 13.Nf3]] and left the bishop on f4 undefended. Bxf4+ won it. Later, [[G5 20.f5]] took 0.8s and skipped the winning capture cxd5.
- In G4, [[G4 13...Bxf6]] took 0.6s and left the queen on c7 hanging to Bxc7. [[G4 12...f5]] took 1.0s.
- In G3, [[G3 11.Qxe5+]] took 1.3s and left your queen undefended.
- Why: the data tags these moves "played_quickly" and "hangs_piece". You had 34–36s left in several of them, so the clock wasn't the cause. You were rushing.
- Habit: before every move, ask "what does this move leave undefended, and what can they capture or check?" Then look at all captures and checks for both sides. Even in 1+0 bullet, take 3–4 seconds on any move that moves a piece next to an enemy piece.

**2. Missing the punishing move after your opponent errs**
- In G1 your opponent blundered with 14...Kg8. You then played [[G1 15.Qe3]] and [[G1 16.g4]] instead of f4 or Ne6, and a +3.6 position turned into a near loss.
- [[G5 32.Kd2]] missed Rxd3, which was available because the opponent's 31...Bd3 was a blunder.
- Why: the tags say "missed_punish" (6x) and "best_was_capture" (4x). After an opponent's error, you play a natural-looking move instead of checking captures and checks first.
- Habit: when the opponent's last move looks odd, ask "what did that move allow?" Check every capture and check before anything else.

**3. Running out of time while winning**
- You lost on time in G2, G3 and G4. In G2 you had mate in 1 and played [[G2 17...Qxg4+]] with 3s left, instead of Nf3#.
- Habit: in bullet, always check for mate and checks first when you're low on time. Don't play your first idea.

## Moments to replay
- [[G2 17...Qxg4+]]: Nf3# was mate on the spot. Always scan for checks before capturing.
- [[G5 13.Nf3]]: Ne4 kept your big advantage. Nf3 left the f4 bishop hanging.
- [[G1 16.g4]]: Ne6 kept you clearly better. g4 weakened your king and let Nf4 in.
- [[G7 12...Be7]]: bxc6 was the engine's move. Be7 let Nxe7 come with a big swing against you.
- [[G6 37...Kg4]]: Ke3 or Ke4 kept you clearly winning. Kg4 allowed Rg6+ and lost material.

## What is working
- You fight back. G1, G5 and G7 were all comebacks from very bad positions, and you won all three.
- Your rapid games were your best. G8 scored 93.1 accuracy and G6 and G7 scored 87.5 and 87.3. Your opening in G8 (Queen's Gambit) scored 88.7, and your G6 opening scored 89.1.

## Your plan for the next 7 days
- Play 3–4 rapid games (10+0) this week. Your results and accuracy are better there than in bullet.
- Do 10 puzzles a day on "hanging piece" and "capture or check" themes. Before each answer, name what your move leaves undefended.
- Use this checklist on every move: checks, captures, threats, then what my move leaves hanging.
- In bullet, stop moving instantly once you're ahead. Take a short pause to look for a mate or a free piece.
- Practice the Caro-Kann and Scandinavian lines you played in G4 and G2 (opening accuracy 73.0 and 74.0). Study the first 10 moves.
- Practice simple endgames such as rook-and-pawn and knight-and-pawn, so you convert wins like G6 more easily.