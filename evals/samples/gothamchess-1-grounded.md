## The big picture
You lost 4 of 6 blitz games (2W 4L) against opponents averaging 2912, and engine accuracy was still a solid 86.4. The games were decided by a few quick, automatic moves in positions that were better or balanced. Of your 13 serious errors, 8 were played in 2 seconds or less, and 0 came in time pressure. You had time on the clock, and you used it on the wrong moves.

## Patterns to fix

**1. Skipping the capture or punishing move after an opponent's error**
- In G1, your opponent's 21.Qxc3 was a mistake, and Bxe4 was the punishment. You played [[G1 21...Nf4]] in 1.8s. One move later you again missed a capture with [[G1 22...Qc7]] (1.1s), when Nxe4 was best.
- [[G5 22.Nxh3]] missed Ng6, which was winning material. [[G6 14.Bc1]] missed Bxc3, which won about 3 points of material.
- Why: the tags are `missed_punish` (4x) and `best_was_capture` (4x). After an opponent's slip, you play a natural developing or consolidating move without checking forcing options first.
- Habit: after every opponent move that changes the position, list all captures and checks for both sides before you play. Do this even when your move is already "obvious".

**2. Slipping while clearly winning (the conversion phase)**
- In G2, [[G2 43...a4]] and [[G2 44...Qb4]] both passed on d3, which was worth about +4.5. You were still better and won the game.
- In G3, [[G3 52.c7]] gave away a +5.0 position, and [[G3 53.Kb4]] missed Kc3. [[G3 43.Kd1]] was also played in 1.1s.
- Why: the tag `was_winning` appears 5x. These moves came with 18–50s left, and 3 of the 5 took under 2s. You moved at the speed of a premove when the position needed a king-safety and queen-check audit.
- Habit: when ahead by a lot, ask "what is their best check or perpetual?" before moving. In queen endgames, check the opponent's checks first. Give these moves a minimum of 3–4 seconds.

**3. Quick moves in the early middlegame and opening**
- [[G1 17...Nc5]] (1.3s, with 2:36 left) was the move that turned a +0.9 position into a worse one. The engine preferred a5.
- [[G6 8.b3]] (1.3s, with 2:48 left) dropped your evaluation, and e4 was best.
- Why: `played_quickly` appeared in 8 of the 13 serious errors. Your opening accuracy is high (91.1), so you are probably playing from memory or habit, and leaving prep sooner than you think.

## Moments to replay
- [[G1 22...Qc7]]: Nxe4 was the engine's choice, and Qc7 let your opponent play c5. Capturing the e4 pawn was worth far more than the quiet queen move.
- [[G3 52.c7]]: Ka4 kept a +5.0 advantage. Pushing the pawn let Qd1+ force a perpetual-type sequence.
- [[G5 22.Nxh3]]: Ng6 attacked the rook and won material. Taking the pawn on h3 let your opponent back into the game.
- [[G4 24...Qb5]]: Rxe1+ was the engine's choice, and the queen move allowed Qxb5 axb5 Rxe8+ Rxe8 Ng4. Trade rooks first to solve the problem.
- [[G5 45.Nf4]]: Kd3 was the only move that held the endgame. Nf4 allowed Ra1 Rxb2 Bxb2 Nd3, which loses material.

## What is working
- Your opening phase is your best phase at 91.1 accuracy, against 86.4 in the middlegame and 82.8 in the endgame. In G1 you scored 96.9 in the opening, and you won G2 as Black against a 2899.
- You never thrown away a win by the 80% standard, and you have 0 time-pressure errors. Your clock handling is not the problem.

## Your plan for the next 7 days
- Before each move, run a "captures, checks, threats" scan for both sides. Count it as a clear habit, not a feeling.
- Set a rule: after the opponent's move, spend at least 3 seconds if the move was a capture or a check. If you are ahead, spend 3 seconds before every move.
- Do 15 puzzles a day on "missed capture" and "zwischenzug" themes, then take 10 minutes to review the ones you got wrong.
- Drill queen-plus-pawn endgames, especially perpetual-check defence with your king close to the pawn. Use G3 as your model.
- Review the 6.b3 line in G6 and the English structure in G1, and check where your moves differ from the engine's.
- Play 10 blitz games and record every move you make in under 2 seconds. Check whether each one was a forced reply or a habit.