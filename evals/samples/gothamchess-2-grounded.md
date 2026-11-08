## The big picture
You went 2W 4L in six 3+0 blitz games, mostly against opponents rated 2741–2983. Your engine accuracy was 86.4 overall, but your endgame was the weakest phase at 82.8. Of your 13 serious errors, 8 were played in 2 seconds or less, and none came in time pressure. Your clock was fine. The errors came from moving too fast at key moments.

## Patterns to fix

**1. Skipping the capture or check at critical moments (fast, automatic moves)**
- Evidence: [[G1 21...Nf4]] and [[G1 22...Qc7]] both ignored a capture on e4. [[G6 14.Bc1]] missed Bxc3. [[G4 24...Qb5]] missed the forcing Rxe1+. The tags `best_was_capture` (4x) and `played_quickly` (8x) show this.
- Why: In G1 you played both moves in under 2 seconds with about 2:30 left. You were choosing the "natural" developing or retreating move without checking forcing options.
- Habit: When you have more than a minute, run captures, checks and threats before any quiet move. Do it on every move where the position has tension.

**2. Not punishing opponent errors and slipping from winning positions**
- Evidence: After your opponent's mistake in G2, [[G2 43...a4]] let the advantage drop, and [[G2 44...Qb4]] gave up more. In G5, [[G5 22.Nxh3]] took a pawn instead of Ng6, which the engine said wins material. In G3, [[G3 53.Kb4]] missed Kc3. `missed_punish` appeared 4x and `was_winning` 5x.
- Why: After a free gift, you relax and play the first reasonable move. Three of these were played in under 2 seconds.
- Habit: When the opponent's last move looks wrong, ask what it allowed, and spend at least 10 seconds on it.

**3. Endgame technique with a low clock**
- Evidence: [[G3 52.c7]] pushed the pawn when the engine preferred Ka4. [[G5 45.Nf4]] lost material to a forcing reply, and the engine preferred Kd3. [[G3 43.Kd1]] gave up much of a clear edge.
- Why: With 20–50 seconds left, you went for direct pushes instead of checking the opponent's checks and king safety.
- Habit: Before pushing a passed pawn, list every check the opponent has. Move your king toward safety first (the engine liked Kc3 and Ka4).

## Moments to replay
- [[G1 21...Nf4]]: Bxe4 was the engine's choice and kept your edge. Nf4 let White consolidate.
- [[G3 53.Kb4]]: Kc3 kept you winning. Kb4 allowed Qd2+ and perpetual-check ideas.
- [[G5 22.Nxh3]]: Ng6 attacked the rook and won material. Nxh3 grabbed a pawn but let the advantage shrink.
- [[G6 14.Bc1]]: Bxc3 was the move. Retreating missed a chance to win material.
- [[G4 24...Qb5]]: Rxe1+ was the only move that kept the balance. Offering a queen trade here lost ground.

## What is working
- Your openings are sound. Opening accuracy was 91.1, and you reached 96.9 in G1 and 94.3 in G5.
- You don't collapse in time trouble (0 serious errors under pressure). You also didn't throw away any won positions or have to claw back any lost ones. You won G2 and G3 despite the slips noted above.

## Your plan for the next 7 days
- Play 3+0 sessions with one rule: on any move where you have more than 60 seconds, pause for 3 seconds and list captures and checks first.
- Do 15 minutes a day of puzzles tagged "capture" and "zwischenzug". Pick sets where the best move is a forcing one.
- Review your games for moves just after an opponent error. Write down what the error allowed before looking at your move.
- Drill queen-and-pawn endings with a passed pawn, such as the [[G3 52.c7]] position. Practise checking the opponent's checks before pushing.
- Practise rook endings with 30 seconds on the clock. Focus on king safety and activity ([[G5 45.Nf4]]).
- Check your English line from G1 and your Trompowsky line from G5 up to move 20. Both openings went well, so look for where you lose the thread afterwards.