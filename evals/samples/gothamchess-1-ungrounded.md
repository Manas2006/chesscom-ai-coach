## The big picture
You went 2-4 against opponents rated 2741-2983. Every loss involved dropping a pawn at a transition (a trade or a pawn advance), then being ground down: [[G1 21.Qxc3]], [[G4 34.Nxd5]], [[G5 27...Rxb2]] and [[G6 12...Bxe5]]. Both wins came from sharp, initiative-based play. Note that the data has only move lists, with no clocks or engine marks, so the "why" below is my own reading of the moves.

## Patterns to fix

**1. Loose pawns after trades**
- Evidence: in G1, 18...Bc3 19.Bxc3 dxc3 20.b4 hits the Nc5, and 21.Qxc3 wins the c3 pawn. In G4, 24...Qb5 25.Qxb5 axb5 leaves the d5 pawn isolated, and it falls to [[G4 34.Nxd5]]. In G5, the queen trade at [[G5 26.Qxb4]] leaves b2 weak, and [[G5 27.Rc7]] Rxb2 loses it.
- Why: each trade changed which pawns were defended, and the opponent captured the pawn immediately. You seem to evaluate the trade itself, not the position after the recapture.
- Habit: before any trade, list your undefended pawns and ask "what hangs after the recapture?"

**2. Central pawn tension handled badly in offbeat setups**
- Evidence: in G6, 11.e4 d4 attacks the Nc3, and 12.e5 Bxe5 13.Bxc6 dxc3 leaves you a pawn down against a strong c3 pawn. In G1, the ...d4 advance needed support, but 18...Bc3 dissolved it and lost a pawn.
- Why: the pawn pushes (11.e4, 15...d4) were committed without checking what the reply would hit. In G6 the 1.Nh3 setup left the pieces less coordinated.
- Habit: before a central push, name the piece that defends the pawn and the enemy break or fork that attacks it.

**3. King walk into a mating net (appears once)**
- Evidence: in G6, 35.Kxf5 e4+ 36.Kf6 exf3 37.Kf7 Re1 38.Bf4 Rf5#.
- Why: the king captured a pawn without a full check of the opponent's forcing replies.
- Habit: in an endgame, list every check and rook move before moving the king.

## Moments to replay
- [[G6 12.e5]]: this loses a pawn to ...Bxe5. Look at a calm knight retreat (such as Ne2) to keep the structure and avoid the ...dxc3 pawn.
- [[G1 18...Bc3]]: this gives up the d4 pawn's support. Look for a way to keep the bishop or the knight protected so b4 doesn't come with tempo.
- [[G4 24...Qb5]]: this allows the queen trade into an isolated d5 pawn. Look for a move that keeps queens on or fixes the structure first.
- [[G5 27.Rc7]]: the rook activity wins nothing concrete and loses b2. Check whether a defensive move or a trade first keeps material level.
- [[G6 35.Kxf5]]: check whether a safer king move avoids the ...e4+ net.

## What is working
- G2 as Black (Modern Defense) beat a 2899 player. The ...g5/...g4 kingside space plan worked, and the late passed pawn (42...d4, 47...d3, 49...d2) decided the game.
- G3 as White (Levitsky Attack) beat a 2907 player. You converted a long game with queen and a passed c-pawn and finished with [[G3 56.Qd8#]].

## Your plan for the next 7 days
- Do 10 "hanging or loose piece" puzzles a day, 5-10 minutes, naming each undefended pawn before you answer.
- Add a clock rule: before every capture or queen trade, pause for one deliberate look at undefended pawns.
- Replay G1 and G6 move by move with an engine, since I had none. Find where the evaluation first dropped.
- Review the G6 opening: decide whether to keep 1.Nh3 or return to a more familiar setup with the same pawn structure.
- Drill rook endgames and king-activity positions for 15 minutes, twice this week.
- Keep what works: replay G2 and G3, and note why the initiative held.