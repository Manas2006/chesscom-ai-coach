"use strict";

const $ = (s, el = document) => el.querySelector(s);
const state = { sid: null, data: null, gid: null, ply: -1, mode: "after", health: null, tab: "coach" };
const CITE_RE = /\[\[\s*(G\d+\s+\d+\s*(?:\.\.\.|\.)\s*[^\]\s]+)\s*\]\]/g;

/* Game Review vocabulary. Glyphs are standard chess annotation symbols. */
const CLS = {
  best: { sym: "★", text: "is best" },
  excellent: { sym: "✓", text: "is excellent" },
  good: { sym: "✓", text: "is good" },
  inaccuracy: { sym: "?!", text: "is an inaccuracy" },
  mistake: { sym: "?", text: "is a mistake" },
  blunder: { sym: "??", text: "is a blunder" },
};
const NOTABLE = new Set(["inaccuracy", "mistake", "blunder"]);
const FIG = { K: "♚", Q: "♛", R: "♜", B: "♝", N: "♞" };
const REASONS = {
  checkmated: "by checkmate", resigned: "by resignation", timeout: "on time", abandoned: "by abandonment",
  agreed: "by agreement", repetition: "by repetition", stalemate: "by stalemate", insufficient: "by insufficient material",
  "50move": "by the 50-move rule", timevsinsufficient: "on time vs insufficient material",
};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => (x == null ? "n/a" : `${Math.round(x)}%`);
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const reason = (r) => REASONS[r] || `(${r})`;
const ico = (cls, small) => (CLS[cls] ? `<span class="ico ${small ? "sm" : ""} bg-${cls}" title="${cap(cls)}">${CLS[cls].sym}</span>` : "");

function fmtClock(s) {
  if (s == null) return "";
  if (s < 20) return `0:${String(Math.floor(s)).padStart(2, "0")}.${Math.floor((s * 10) % 10)}`;
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "content-type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}

/* ------------------------------------------------------------------ status */
async function loadHealth() {
  try {
    const h = await api("/api/health");
    state.health = h;
    const eng = h.engine.ok
      ? `<span class="chip ok"><span class="dot"></span>${esc(h.engine.name)} · depth ${h.engine.depth}</span>`
      : `<span class="chip bad" title="${esc(h.engine.error)}"><span class="dot"></span>Stockfish missing</span>`;
    const llm = h.llm.provider
      ? `<span class="chip ok"><span class="dot"></span>${esc(h.llm.model)}</span>`
      : `<span class="chip warn" title="Set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env"><span class="dot"></span>No LLM key: engine-only mode</span>`;
    $("#status").innerHTML = eng + llm + (h.offline ? `<span class="chip warn"><span class="dot"></span>offline</span>` : "");
    if (!h.engine.ok) showFormError(h.engine.error);
  } catch (e) {
    $("#status").innerHTML = `<span class="chip bad"><span class="dot"></span>server unreachable</span>`;
  }
}

/* ------------------------------------------------------------------ analysis job */
function showFormError(msg) {
  const el = $("#form-error");
  el.textContent = msg;
  el.classList.toggle("hidden", !msg);
}

function show(id) {
  for (const s of ["landing", "progress", "dashboard"]) $("#" + s).classList.toggle("hidden", s !== id);
}

async function start(body) {
  showFormError("");
  show("progress");
  setProgress(0.02, "Starting");
  try {
    const { job_id } = await api("/api/analyze", { method: "POST", body: JSON.stringify(body) });
    for (;;) {
      await new Promise((r) => setTimeout(r, 600));
      const j = await api(`/api/jobs/${job_id}`);
      setProgress(j.progress, j.stage);
      if (j.status === "done") return loadSession(j.session_id);
      if (j.status === "error") throw new Error(j.error);
    }
  } catch (e) {
    show("landing");
    showFormError(e.message);
  }
}

function setProgress(p, stage) {
  $("#progress-bar").style.width = `${Math.round(p * 100)}%`;
  $("#progress-pct").textContent = `${Math.round(p * 100)}%`;
  $("#progress-stage").textContent = stage;
}

async function loadSession(sid) {
  state.sid = sid;
  state.data = await api(`/api/sessions/${sid}`);
  history.replaceState(null, "", `?s=${sid}`);
  show("dashboard");
  renderDashboard();
  const worst = [...state.data.moments].sort((a, b) => b.win_drop - a.win_drop)[0];
  if (worst) gotoRef(worst.ref, false);
  else selectGame(state.data.games[0].gid, -1, "after");
  if (state.data.report) renderReport(state.data.report);
  else loadReport(false);
}

/* ------------------------------------------------------------------ dashboard */
function avatarHtml(cls) {
  const p = state.data.profile;
  return p.avatar
    ? `<img class="${cls || ""}" src="${esc(p.avatar)}" alt="" referrerpolicy="no-referrer" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'avatar-ph',textContent:'♞'}))" />`
    : `<div class="avatar-ph">♞</div>`;
}

function renderDashboard() {
  const d = state.data, p = d.profile, smp = p.sample;
  const ratings = Object.entries(p.ratings)
    .map(([k, v]) => `<span class="chip">${esc(cap(k))} <b style="color:#fff">${esc(v.current)}</b></span>`)
    .join("");
  $("#player-card").innerHTML = `
    ${avatarHtml()}
    <div>
      <div class="name">${p.title ? `<span class="title-badge">${esc(p.title)}</span>` : ""}${esc(p.name || p.username)}</div>
      <div class="muted small"><a href="${esc(p.profile_url)}" target="_blank" rel="noopener">@${esc(p.username)}</a> · ${smp.games} games · ${esc(smp.date_range[0])}${smp.date_range[1] !== smp.date_range[0] ? " to " + esc(smp.date_range[1]) : ""}</div>
      <div class="ratings">${ratings}</div>
    </div>`;
  const r = smp.record, acc = p.accuracy, err = p.errors.per_game;
  $("#tiles").innerHTML = [
    tile("Record", `${r.win}W ${r.loss}L ${r.draw}D`, `score ${smp.score ?? "n/a"}%`),
    tile("Accuracy", acc.overall ?? "n/a", acc.chesscom_avg != null ? `Chess.com Game Review avg ${acc.chesscom_avg}` : "engine accuracy (this app)"),
    tile("Mistakes / game", err.mistake, `${err.inaccuracy} inaccuracies / game`),
    tile("Blunders / game", err.blunder, `${p.time.played_quickly} of ${p.time.serious_errors} costly moves made in 2s or less`),
  ].join("");

  $("#game-tabs").innerHTML = d.games
    .map((g) => `<button data-gid="${g.gid}" title="vs ${esc(g.opponent)} · ${esc(g.opening)}"><span class="res ${g.outcome}"></span>${g.gid}</button>`)
    .join("");
  $("#game-tabs").onclick = (e) => {
    const b = e.target.closest("button");
    if (b) selectGame(b.dataset.gid, -1, "after");
  };

  renderMoments();
  renderStats();
  renderSuggestions();
  $("#footnote").textContent =
    `${d.source === "fixture" ? "Offline demo data (real Chess.com games saved in fixtures/). " : ""}` +
    `Engine: ${d.engine}, depth ${state.health?.engine.depth ?? 12} on every position plus a multi-line re-check of each key moment. ` +
    `Win chance uses Lichess's published model; move labels use Chess.com's Game Review expected-points bands. ` +
    `"Accuracy" is this app's engine metric; Chess.com's own Game Review number is shown where the API provides it.`;
}

const tile = (k, v, s) => `<div class="tile"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="s">${esc(s)}</div></div>`;

function renderMoments() {
  const ms = [...state.data.moments].sort((a, b) => b.win_drop - a.win_drop);
  if (!ms.length) {
    $("#moments").innerHTML = `<p class="muted">No mistakes found. Impressive.</p>`;
    return;
  }
  $("#moments").innerHTML = ms
    .map((m) => {
      const img = `/api/sessions/${state.sid}/board.svg?game=${m.gid}&ply=${m.ply}&mode=decision&size=300`;
      const tags = m.tags.map((t) => `<span class="tag">${esc(t.replace(/_/g, " "))}</span>`).join("");
      return `<div class="moment" data-ref="${esc(m.ref)}">
        <img loading="lazy" src="${img}" alt="Position before ${esc(m.ref)}" />
        <div class="body">
          <div class="ref">${ico(m.classification, true)} ${esc(m.ref)}</div>
          <div class="swing"><b class="c-${m.classification}">${cap(m.classification)}</b> · ${esc(m.eval_before)} → ${esc(m.eval_after)} <span class="muted">(−${Math.round(m.win_drop)} pts)</span></div>
          <div>Best was <b style="color:#fff">${esc(m.best_san || "?")}</b>${m.refutation && m.refutation.san.length ? ` · punished by <b style="color:#fff">${esc(m.refutation.san[0])}</b>` : ""}</div>
          ${m.tag_notes.length ? `<div class="muted" style="margin-top:4px">${esc(cap(m.tag_notes[0]))}</div>` : ""}
          <div class="tags">${tags}</div>
        </div></div>`;
    })
    .join("");
  $("#moments").onclick = (e) => {
    const c = e.target.closest(".moment");
    if (!c) return;
    setTab("moves");
    gotoRef(c.dataset.ref, true);
  };
}

function renderStats() {
  const p = state.data.profile;
  $("#phases").innerHTML = ["opening", "middlegame", "endgame"]
    .map((ph) => {
      const a = p.accuracy.by_phase[ph], e = p.errors.by_phase[ph];
      return `<div class="phase-row"><span>${cap(ph)}</span><div class="phase-bar"><div style="width:${a ?? 0}%"></div></div><b>${a ?? "n/a"}</b></div>
        <div class="muted small" style="margin:0 0 6px 98px">${e.moves} moves · ${e.mistakes} mistakes · ${e.blunders} blunders</div>`;
    })
    .join("");
  const pats = p.patterns.filter((x) => x.tag !== "best_was_check");
  $("#patterns").innerHTML = pats.length
    ? pats.map((x) => `<div class="kv"><span>${esc(cap(x.label))}</span><b style="color:#fff">${x.count}×</b></div>`).join("")
    : `<p class="muted small">No recurring pattern detected.</p>`;
  $("#openings").innerHTML = `<table class="op"><tr><th></th><th>Opening</th><th>W-L-D</th></tr>${p.openings
    .map((o) => `<tr><td>${o.color === "white" ? "♔" : "♚"}</td><td>${esc(o.opening)}</td><td>${o.win}-${o.loss}-${o.draw}</td></tr>`)
    .join("")}</table>`;
}

/* ------------------------------------------------------------------ tabs */
function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  document.querySelectorAll(".tab-body").forEach((b) => b.classList.toggle("hidden", b.dataset.body !== tab));
  if (tab === "moves") scrollCurrentMove();
}
document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => setTab(t.dataset.tab)));

/* ------------------------------------------------------------------ board + review */
const game = (gid) => state.data.games.find((g) => g.gid === gid);

function gotoRef(ref, scroll) {
  const m = ref.match(/^(G\d+)\s+(\d+)\s*(\.\.\.|\.)/);
  if (!m) return;
  const g = game(m[1]);
  if (!g) return;
  const color = m[3] === "..." ? "black" : "white";
  const idx = g.plies.findIndex((p) => p.color === color && parseInt(p.label, 10) === +m[2]);
  if (idx < 0) return;
  selectGame(g.gid, idx, "decision");
  if (scroll) $("#board-col").scrollIntoView({ behavior: "smooth", block: "start" });
}

function selectGame(gid, ply, mode) {
  const g = game(gid);
  const changed = state.gid !== gid;
  state.gid = gid;
  state.ply = Math.max(-1, Math.min(ply, g.plies.length - 1));
  state.mode = mode || "after";
  if (changed) {
    document.querySelectorAll("#game-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.gid === gid));
    renderMoveList();
    renderAccuracy();
  }
  renderPosition();
}

function baseSeconds(g) {
  const m = String(g.time_control || "").match(/^(\d+)(?:\+(\d+))?$/);
  return m ? +m[1] : null;
}

function clockAt(g, color, ply) {
  for (let i = ply; i >= 0; i--) if (g.plies[i].color === color && g.plies[i].clock != null) return g.plies[i].clock;
  return baseSeconds(g);
}

// Index of the last ply already played in the position shown on the board.
const shownPly = () => (state.mode === "decision" ? state.ply - 1 : state.ply);

function playerBar(g, color, isYou) {
  const name = color === "white" ? g.white : g.black;
  const rating = color === "white" ? g.white_rating : g.black_rating;
  const sp = shownPly();
  const next = sp < 0 ? "white" : g.plies[sp].color === "white" ? "black" : "white";
  const clk = g.time_class !== "daily" ? clockAt(g, color, sp) : null;
  const av = isYou ? avatarHtml() : `<span>${esc((name || "?")[0].toUpperCase())}</span>`;
  return `<div class="av">${av}</div>
    <div class="who">${esc(name)}<span class="rt">(${rating ?? "?"})</span>${isYou ? `<span class="you">YOU</span>` : ""}</div>
    ${clk != null ? `<div class="clock ${color} ${next === color ? "active" : ""}">${fmtClock(clk)}</div>` : ""}`;
}

function renderPosition() {
  const g = game(state.gid);
  const you = g.player_color, opp = you === "white" ? "black" : "white";
  $("#board").src = `/api/sessions/${state.sid}/board.svg?game=${g.gid}&ply=${state.ply}&mode=${state.mode}&size=560`;
  $("#pbar-top").innerHTML = playerBar(g, opp, false);
  $("#pbar-bottom").innerHTML = playerBar(g, you, true);
  renderCoords(g);
  renderBadge(g);
  renderEvalBar(g);
  renderGraph(g);
  renderExplainer(g);
  document.querySelectorAll("#moves .mv").forEach((el) => el.classList.toggle("cur", +el.dataset.ply === state.ply));
  scrollCurrentMove();
}

function squareXY(g, sq) {
  // sq like "e4" -> displayed column/row with the coached player at the bottom
  const file = sq.charCodeAt(0) - 97, rank = +sq[1];
  return g.player_color === "white" ? { col: file, row: 8 - rank } : { col: 7 - file, row: rank - 1 };
}

function renderCoords(g) {
  const out = [];
  for (let i = 0; i < 8; i++) {
    // files along the bottom edge, ranks along the left edge, drawn in the opposite square colour
    const file = g.player_color === "white" ? i : 7 - i;
    const fileDark = (file + (g.player_color === "white" ? 1 : 8)) % 2 === 1;
    out.push(`<span style="right:calc(${(7 - i) * 12.5}% + 0.7%);bottom:0.9%;color:${fileDark ? "#EBECD0" : "#739552"}">${String.fromCharCode(97 + file)}</span>`);
    const rank = g.player_color === "white" ? 8 - i : i + 1;
    const rankDark = ((g.player_color === "white" ? 0 : 7) + rank) % 2 === 1;
    out.push(`<span style="left:0.7%;top:calc(${i * 12.5}% + 0.7%);color:${rankDark ? "#EBECD0" : "#739552"}">${rank}</span>`);
  }
  $("#coords").innerHTML = out.join("");
}

function renderBadge(g) {
  const el = $("#sq-badge"), p = g.plies[state.ply];
  if (!p || state.mode !== "after" || !CLS[p.cls]) {
    el.classList.add("hidden");
    return;
  }
  const { col, row } = squareXY(g, p.uci.slice(2, 4));
  el.className = `sq-badge bg-${p.cls}`;
  el.style.left = `${(col + 1) * 12.5}%`;
  el.style.top = `${row * 12.5}%`;
  el.textContent = CLS[p.cls].sym;
}

function renderEvalBar(g) {
  const i = shownPly() + 1;
  const win = g.win_curve[i];
  const label = g.eval_curve[i] || "";
  const bar = $("#evalbar");
  bar.classList.toggle("flipped", g.player_color === "black");
  $("#evalbar-fill").style.height = `${win}%`;
  const ahead = win >= 50;
  const sc = $("#evalbar-score");
  sc.textContent = label.replace(/^[+-](?=\d|M)/, "");
  sc.style.top = ahead ? "auto" : "0";
  sc.style.bottom = ahead ? "0" : "auto";
  // Text sits on the bottom (your colour) when you are ahead, on the opponent's colour otherwise.
  const onWhite = (g.player_color === "white") === ahead;
  sc.style.color = onWhite ? "#312E2B" : "#fff";
}

function renderGraph(g) {
  const youWhite = g.player_color === "white";
  const pts = g.win_curve.map((w) => (youWhite ? w : 100 - w)); // White's win chance, as Game Review draws it
  const n = pts.length - 1 || 1, W = 400, H = 76;
  const x = (i) => (i / n) * W, y = (v) => H - (v / 100) * H;
  const line = pts.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const dots = g.plies
    .map((p, i) => (NOTABLE.has(p.cls) && p.cls !== "inaccuracy" ? `<circle cx="${x(i + 1)}" cy="${y(pts[i + 1])}" r="3.6" fill="var(--${p.cls})" stroke="#262421" stroke-width="1.2"/>` : ""))
    .join("");
  const cx = x(shownPly() + 1);
  $("#eval-graph").innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-label="Evaluation graph">
      <rect width="${W}" height="${H}" fill="#403D39"/>
      <path d="${line}L${W},${H}L0,${H}Z" fill="#fff"/>
      <line x1="0" x2="${W}" y1="${H / 2}" y2="${H / 2}" stroke="#8B8987" stroke-width="1" stroke-dasharray="2 3" vector-effect="non-scaling-stroke"/>
      <line x1="${cx}" x2="${cx}" y1="0" y2="${H}" stroke="#81B64C" stroke-width="2" vector-effect="non-scaling-stroke"/>${dots}</svg>
      <div class="cap muted small"><span>White's winning chances over the game</span><span>You: ${pct(g.win_curve[shownPly() + 1])}</span></div>`;
  $("#eval-graph svg").onclick = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    selectGame(g.gid, Math.round(((e.clientX - r.left) / r.width) * n) - 1, "after");
  };
}

function renderAccuracy() {
  const g = game(state.gid);
  const box = (color) => {
    const you = color === g.player_color;
    const acc = you ? g.accuracy : g.opponent_accuracy;
    const name = color === "white" ? g.white : g.black;
    const cc = you && g.chesscom_accuracy != null ? ` · Chess.com ${g.chesscom_accuracy}` : "";
    return `<div class="acc ${color}"><div class="n">${esc(name)}</div><div class="v">${acc ?? "n/a"}</div><div class="l">accuracy${cc}</div></div>`;
  };
  const res = { win: "won", loss: "lost", draw: "drew" }[g.outcome];
  $("#acc-row").innerHTML = box("white") + box("black") +
    `<div class="muted small" style="grid-column:1/-1">${esc(g.time_class)} · you ${res} ${esc(reason(g.end_reason))} · ${esc(g.opening)} · <a href="${esc(g.url)}" target="_blank" rel="noopener">open on Chess.com</a></div>`;
}

function renderExplainer(g) {
  const p = g.plies[state.ply];
  const el = $("#explainer");
  if (!p) {
    el.innerHTML = `<div><div class="t">Starting position</div><div class="d">Step through with ◀ ▶ or the arrow keys, or click a key moment.</div></div>`;
    return;
  }
  const m = state.data.moments.find((x) => x.gid === g.gid && x.ply === state.ply);
  const c = CLS[p.cls] ? p.cls : "good";
  const lines = [];
  if (state.mode === "decision") lines.push(`Board shows the position before ${esc(p.label)}: <span class="c-blunder">red</span> is what was played, <span class="c-best">green</span> the engine's choice.`);
  if (NOTABLE.has(p.cls) && (m?.best_san || p.best)) lines.push(`The best move was <b style="color:#fff">${esc(m?.best_san || p.best)}</b>${m ? ` (${esc(m.eval_before)} → ${esc(m.eval_after)})` : ""}.`);
  if (m?.tag_notes.length) lines.push(esc(m.tag_notes.map(cap).join(". ")) + ".");
  if (m?.refutation?.san.length) lines.push(`Punishing line: ${esc(m.refutation.san.join(" "))}.`);
  if (p.time_spent != null) lines.push(`<span class="muted">${p.time_spent}s spent · ${fmtClock(p.clock)} left</span>`);
  el.innerHTML = `${ico(c)}<div><div class="t"><span class="c-${c}">${esc(p.san)} ${CLS[c].text}</span></div><div class="d">${lines.join(" ")}</div></div>`;
}

function sanHtml(san, color) {
  const f = FIG[san[0]];
  return f ? `<span class="fig">${f}</span>${esc(san.slice(1))}` : esc(san);
}

function renderMoveList() {
  const g = game(state.gid);
  const rows = new Map();
  g.plies.forEach((p, i) => {
    const num = parseInt(p.label, 10);
    if (!rows.has(num)) rows.set(num, { white: "<div></div>", black: "<div></div>" });
    const icon = NOTABLE.has(p.cls) ? ico(p.cls, true) : "";
    rows.get(num)[p.color] = `<div class="mv" data-ply="${i}">${sanHtml(p.san, p.color)}${icon}</div>`;
  });
  $("#moves").innerHTML = [...rows.entries()].map(([n, r]) => `<div class="mrow"><div class="n">${n}.</div>${r.white}${r.black}</div>`).join("");
  $("#moves").onclick = (e) => {
    const m = e.target.closest(".mv");
    if (m) selectGame(g.gid, +m.dataset.ply, "after");
  };
}

function scrollCurrentMove() {
  const cur = $("#moves .mv.cur"), box = $("#moves");
  if (!cur || $(".tab-body[data-body=moves]").classList.contains("hidden")) return;
  const top = cur.offsetTop - box.offsetTop;
  if (top < box.scrollTop + 40 || top > box.scrollTop + box.clientHeight - 60) box.scrollTop = top - box.clientHeight / 2;
}

function nav(where) {
  if (!state.gid) return;
  const g = game(state.gid), last = g.plies.length - 1;
  const ply = { start: -1, prev: state.ply - 1, next: state.ply + 1, end: last }[where];
  selectGame(g.gid, ply, "after");
}
document.querySelectorAll("[data-nav]").forEach((b) => (b.onclick = () => nav(b.dataset.nav)));
document.addEventListener("keydown", (e) => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName) || !state.data) return;
  const k = { ArrowLeft: "prev", ArrowRight: "next", Home: "start", End: "end" }[e.key];
  if (k) {
    e.preventDefault();
    nav(k);
  }
});

/* ------------------------------------------------------------------ markdown + grounding */
function inline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
    .replace(/(^|\s)_([^_]+)_(?=[\s.,;:!?]|$)/g, "$1<em>$2</em>");
}

function renderMarkdown(md, verification) {
  const bad = new Set((verification?.citations?.failures || []).map((f) => f.replace(/^\[\[|\]\].*$/g, "").replace(/\s+/g, " ").trim()));
  const cites = [];
  const text = esc(md || "").replace(CITE_RE, (_, ref) => {
    cites.push(ref.replace(/\s+/g, " ").trim());
    return `\u0000${cites.length - 1}\u0000`;
  });
  const out = [];
  let list = null;
  const close = () => {
    if (list) out.push(`</${list}>`);
    list = null;
  };
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    let m;
    if (!line.trim()) { close(); continue; }
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { close(); const lvl = Math.min(m[1].length + 1, 4); out.push(`<h${lvl}>${inline(m[2])}</h${lvl}>`); continue; }
    if ((m = line.match(/^\s*[-*]\s+(.*)$/))) { if (list !== "ul") { close(); out.push("<ul>"); list = "ul"; } out.push(`<li>${inline(m[1])}</li>`); continue; }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { if (list !== "ol") { close(); out.push("<ol>"); list = "ol"; } out.push(`<li>${inline(m[1])}</li>`); continue; }
    close();
    out.push(`<p>${inline(line)}</p>`);
  }
  close();
  return out.join("").replace(/\u0000(\d+)\u0000/g, (_, i) => {
    const r = cites[+i], isBad = bad.has(r);
    const label = r.replace(/^(G\d+)\s+/, "$1 · ");
    return `<button class="cite${isBad ? " bad" : ""}" data-ref="${esc(r)}" title="${isBad ? "This citation does not match the game record" : "Show on board"}">${esc(label)}</button>`;
  });
}

function markUnverified(root, verification) {
  if (!verification) return;
  const tokens = [...(verification.moves?.failures || []), ...(verification.recommendations?.failures || []), ...(verification.evals?.failures || [])];
  if (!tokens.length) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement.closest(".cite, mark") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  const re = new RegExp(tokens.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g");
  for (const node of nodes) {
    if (!re.test(node.nodeValue)) continue;
    re.lastIndex = 0;
    const span = document.createElement("span");
    span.innerHTML = esc(node.nodeValue).replace(re, (t) => `<mark class="unverified" title="Not supported by the engine data">${t}</mark>`);
    node.replaceWith(...span.childNodes);
  }
}

function groundingBadge(v, small) {
  if (!v) return "";
  const s = Math.round(v.score * 100);
  const cls = v.score >= 0.95 ? "good" : v.score >= 0.8 ? "mid" : "bad";
  return `<span class="badge ${cls}" title="Citations, moves, recommendations and evaluations checked against Stockfish">${v.score >= 0.95 ? "✓" : "⚠"} ${s}% grounded${small ? "" : ` · ${v.checked} checks`}</span>`;
}

function groundingTable(rep) {
  const v = rep.verification;
  const row = (k, c, what) => `<tr><td>${k}</td><td>${c.ok}/${c.total}</td><td class="muted">${what}${c.failures.length ? `<br><span class="c-blunder">Flagged: ${c.failures.map(esc).join(", ")}</span>` : ""}</td></tr>`;
  let html = `<table><tr><th>Check</th><th>Passed</th><th>What is verified</th></tr>
    ${row("Citations", v.citations, "Each [[G# move]] points to a move actually played in that game")}
    ${row("Moves", v.moves, "Each move written in the text is legal in a position the sentence is about")}
    ${row("Recommendations", v.recommendations, "Moves called better or best are among Stockfish's top choices or were checked with a tool")}
    ${row("Evaluations", v.evals, "Numbers like +1.7 appear in the engine data")}</table>`;
  if (rep.repaired && rep.verification_before_repair) {
    const before = Math.round(rep.verification_before_repair.score * 100), after = Math.round(v.score * 100);
    html += `<p class="small">The first draft scored ${before}%. The coach was shown the flagged items and revised its answer (now ${after}%)${v.flagged.length ? "; anything still unsupported is highlighted in the text" : ""}.</p>`;
  }
  if (rep.trace?.length) html += `<div class="trace"><p class="small muted">Engine tools the coach called:</p>${traceHtml(rep.trace)}</div>`;
  return html;
}

function traceHtml(trace) {
  return trace
    .map((t) => `<details><summary>🔧 ${esc(t.tool)}(${esc(Object.values(t.args).join(", "))}) · ${t.ms} ms</summary><pre>${esc(t.result)}</pre></details>`)
    .join("");
}

/* ------------------------------------------------------------------ report */
async function loadReport(refresh) {
  const n = state.data.moments.length;
  $("#report").innerHTML = '<div class="skeleton" style="width:60%"></div><div class="skeleton"></div><div class="skeleton" style="width:85%"></div><div class="skeleton" style="width:40%;margin-top:22px"></div><div class="skeleton"></div><div class="skeleton" style="width:75%"></div>';
  $("#report-meta").textContent = state.health?.llm.provider ? `Your coach is reviewing ${n} key moments… (usually 15 to 40 seconds)` : "";
  $("#grounding-badge").innerHTML = "";
  $("#grounding-details").classList.add("hidden");
  $("#regen").disabled = true;
  try {
    renderReport(await api(`/api/sessions/${state.sid}/report${refresh ? "?refresh=true" : ""}`, { method: "POST" }));
  } catch (e) {
    $("#report").innerHTML = `<div class="error">${esc(e.message)}</div>`;
  } finally {
    $("#regen").disabled = false;
  }
}

function renderReport(rep) {
  const el = $("#report");
  el.innerHTML = renderMarkdown(rep.markdown, rep.verification);
  markUnverified(el, rep.verification);
  $("#grounding-badge").innerHTML = groundingBadge(rep.verification);
  $("#grounding-body").innerHTML = groundingTable(rep);
  $("#grounding-details").classList.remove("hidden");
  const u = rep.usage || {};
  $("#report-meta").innerHTML =
    rep.mode === "llm"
      ? `${esc(rep.model)} · ${rep.latency_s}s · ${(u.input_tokens || 0) + (u.output_tokens || 0)} tokens · ${rep.trace.length} engine tool call${rep.trace.length === 1 ? "" : "s"}${rep.repaired ? " · revised after verification" : ""}`
      : rep.error ? `<span class="c-mistake">${esc(rep.error)}</span>` : "";
}
$("#regen").onclick = () => loadReport(true);
$("#grounding-badge").onclick = () => ($("#grounding-details").open = !$("#grounding-details").open);
document.addEventListener("click", (e) => {
  const c = e.target.closest(".cite");
  if (c) gotoRef(c.dataset.ref, window.innerWidth < 1100);
});

/* ------------------------------------------------------------------ chat */
function renderSuggestions() {
  const ms = [...state.data.moments].sort((a, b) => b.win_drop - a.win_drop);
  const s = ["What is the one habit that would gain me the most rating points?"];
  if (ms[0]) s.push(`Why was ${ms[0].ref} so costly?`);
  const alt = ms.find((m) => m.best_lines.length > 1 && m.best_lines[1].san.length);
  if (alt) s.push(`In ${alt.ref}, would ${alt.best_lines[1].san[0]} have been good too?`);
  if (state.data.profile.openings.length) s.push("Which of my openings should I work on first?");
  $("#chat-suggestions").innerHTML = s.map((q) => `<button type="button">${esc(q)}</button>`).join("");
  $("#chat-suggestions").onclick = (e) => {
    const b = e.target.closest("button");
    if (b) sendChat(b.textContent);
  };
}

function addMsg(cls, html) {
  const d = document.createElement("div");
  d.className = `msg ${cls}`;
  d.innerHTML = html;
  $("#chat-log").appendChild(d);
  const body = $(".tab-body[data-body=chat]");
  body.scrollTop = body.scrollHeight;
  return d;
}

async function sendChat(text) {
  text = text.trim();
  if (!text) return;
  setTab("chat");
  $("#chat-input").value = "";
  addMsg("user", esc(text));
  const pending = addMsg("bot", `<div class="skeleton" style="width:200px"></div><span class="muted small">Thinking${state.health?.llm.provider ? " (may run Stockfish)" : ""}…</span>`);
  try {
    const r = await api(`/api/sessions/${state.sid}/chat`, { method: "POST", body: JSON.stringify({ message: text }) });
    pending.innerHTML = `<div class="md">${renderMarkdown(r.markdown, r.verification)}</div>` +
      `<div class="tools">${r.verification ? groundingBadge(r.verification, true) : ""}${r.trace?.length ? `<div class="trace">${traceHtml(r.trace)}</div>` : ""}</div>`;
    markUnverified(pending, r.verification);
  } catch (e) {
    pending.innerHTML = `<span class="c-blunder">${esc(e.message)}</span>`;
  }
  const body = $(".tab-body[data-body=chat]");
  body.scrollTop = body.scrollHeight;
}
$("#chat-form").onsubmit = (e) => {
  e.preventDefault();
  sendChat($("#chat-input").value);
};

/* ------------------------------------------------------------------ boot */
$("#analyze-form").onsubmit = (e) => {
  e.preventDefault();
  start({ username: $("#username").value.trim(), games: +$("#ngames").value, time_class: $("#tclass").value, demo: false });
};
$("#demo-btn").onclick = () => start({ username: "gothamchess", games: 6, time_class: "all", demo: true });

(async function boot() {
  await loadHealth();
  const q = new URLSearchParams(location.search);
  if (q.get("s")) {
    try {
      await loadSession(q.get("s"));
      return;
    } catch {
      history.replaceState(null, "", "/");
    }
  }
  if (q.get("demo")) start({ username: "gothamchess", games: 6, time_class: "all", demo: true });
  else if (q.get("u")) start({ username: q.get("u"), games: +(q.get("n") || 10), time_class: q.get("tc") || "all", demo: false });
})();
