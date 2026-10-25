"use strict";

const $ = (id) => document.getElementById(id);

const GLYPHS = { k: "\u265A", q: "\u265B", r: "\u265C", b: "\u265D", n: "\u265E", p: "\u265F" };
const PIECE_NAMES = { k: "King", q: "Queen", r: "Rook", b: "Bishop", n: "Knight", p: "Pawn" };
const VALUES = { p: 1, n: 3, b: 3, r: 5, q: 9, k: 0 };

let state = null;
let prevPlacement = null;
let selected = null;
let targetSquares = [];
let suggestion = null;
let flags = { opponents: false, partner: false, autopilot: false };
let voiceOn = false;
let autoKaiOn = false;
let avaOn = false;
let replaying = false;
let toastTimer = null;

// ---------------------------------------------------------------- speech out

let voiceCache = [];
let voiceWarned = false;

function refreshVoices() {
  const all = "speechSynthesis" in window ? speechSynthesis.getVoices() : [];
  voiceCache = all.filter((v) => v.lang && v.lang.toLowerCase().startsWith("en"));
}
if ("speechSynthesis" in window) {
  refreshVoices();
  speechSynthesis.onvoiceschanged = refreshVoices;
}

function pickVoice(who) {
  if (!voiceCache.length) return null;
  const female = voiceCache.find((v) => /zira|huihui|female|susan|hazel|aria|jenny/i.test(v.name));
  const male = voiceCache.find((v) => !/zira|huihui|female|susan|hazel|aria|jenny/i.test(v.name)) || voiceCache[0];
  switch (who) {
    case "Mira":
    case "Ava":
      return female || voiceCache[voiceCache.length - 1];
    default:
      return male;
  }
}

const VOICE_PITCHES = { Kai: 1.1, Viktor: 0.5, Mira: 1.4, Ava: 0.95, Arbiter: 1.0 };
const VOICE_RATES = { Kai: 1.15, Viktor: 0.92, Mira: 1.08, Ava: 1.18, Arbiter: 1.1 };

function speak(text, who) {
  if (!voiceOn || !("speechSynthesis" in window) || !text) return;
  const clean = text
    .replace(/(MOVE|SUGGEST|CHAT|TALK)\s*[:\-].*/gis, "")
    .replace(/[*_`#>]/g, "")
    .trim();
  if (!clean) return;
  try {
    const u = new SpeechSynthesisUtterance(clean.slice(0, 220));
    u.pitch = VOICE_PITCHES[who] ?? 1.0;
    u.rate = VOICE_RATES[who] || 1.12;
    u.volume = 1;
    u.lang = "en-US";
    const v = pickVoice(who);
    if (v) u.voice = v;
    u.onerror = () => {
      if (!voiceWarned) {
        voiceWarned = true;
        toast("Speech failed in this browser - try Edge or Chrome.");
      }
    };
    speechSynthesis.speak(u);
  } catch (err) {
    console.warn("speech unavailable", err);
  }
}

// ---------------------------------------------------------------- sound fx

let audioCtx = null;
let soundOn = true;

function ensureAudio() {
  if (!audioCtx && "AudioContext" in window) {
    try { audioCtx = new AudioContext(); } catch (e) { soundOn = false; }
  }
  if (audioCtx && audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
}
document.addEventListener("pointerdown", ensureAudio, { once: true });

function tone(freqStart, freqEnd, duration, type, gainVal, delay = 0) {
  if (!audioCtx || !soundOn) return;
  const t0 = audioCtx.currentTime + delay;
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(freqStart, t0);
  osc.frequency.exponentialRampToValueAtTime(Math.max(40, freqEnd), t0 + duration);
  gain.gain.setValueAtTime(gainVal, t0);
  gain.gain.exponentialRampToValueAtTime(0.0001, t0 + duration);
  osc.connect(gain).connect(audioCtx.destination);
  osc.start(t0);
  osc.stop(t0 + duration + 0.02);
}

function playSound(kind) {
  if (!soundOn) return;
  ensureAudio();
  if (!audioCtx) return;
  switch (kind) {
    case "move":
      tone(520, 300, 0.07, "triangle", 0.12);
      break;
    case "capture":
      tone(300, 90, 0.14, "square", 0.1);
      tone(180, 70, 0.16, "triangle", 0.12, 0.02);
      break;
    case "check":
      tone(880, 880, 0.08, "sine", 0.12);
      tone(880, 880, 0.08, "sine", 0.12, 0.12);
      break;
    case "end":
      tone(520, 520, 0.18, "sine", 0.14);
      tone(392, 392, 0.2, "sine", 0.14, 0.2);
      tone(262, 262, 0.34, "sine", 0.16, 0.42);
      break;
    case "notify":
      tone(700, 950, 0.09, "sine", 0.1);
      break;
  }
}

// ---------------------------------------------------------------- helpers

function pieceMap(fen) {
  const map = {};
  const rows = fen.split(" ")[0].split("/");
  const files = "abcdefgh";
  rows.forEach((row, i) => {
    const rank = 8 - i;
    let file = 0;
    for (const ch of row) {
      if (/\d/.test(ch)) {
        file += parseInt(ch, 10);
      } else {
        map[files[file] + rank] = { glyph: GLYPHS[ch.toLowerCase()], color: ch === ch.toUpperCase() ? "white" : "black", type: ch.toLowerCase() };
        file += 1;
      }
    }
  });
  return map;
}

function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 4000);
}

async function post(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const data = await resp.json();
      if (data.detail) detail = data.detail;
    } catch (e) { /* keep default */ }
    toast(detail);
    return null;
  }
  return resp.json();
}

function scrollLog(el) {
  el.scrollTop = el.scrollHeight;
}

function splitDirective(text) {
  const lines = (text || "").split("\n");
  let move = null;
  const visible = [];
  for (const line of lines) {
    const m = line.match(/^\s*(?:MOVE|SUGGEST)\s*[:\-]\s*(.+)$/i);
    if (m) {
      move = m[1].trim().replace(/[`"'.,;!?]/g, "");
    } else if (!/^\s*(?:CHAT|TALK)\s*[:\-]/i.test(line)) {
      visible.push(line);
    }
  }
  return { visible: visible.join("\n").trim(), move };
}

// ---------------------------------------------------------------- board render

const FILES = "abcdefgh";

function squareXY(sq) {
  return { x: FILES.indexOf(sq[0]), y: parseInt(sq[1], 10) - 1 }; // x: 0-7, y: 0-7
}

function renderBoard(animate = false) {
  const board = $("board");
  board.innerHTML = "";
  if (!state) return;
  const map = pieceMap(state.fen);
  for (let rank = 8; rank >= 1; rank--) {
    for (const file of FILES) {
      const sq = file + rank;
      const div = document.createElement("div");
      const isLight = (FILES.indexOf(file) + rank) % 2 === 0;
      div.className = "square " + (isLight ? "light" : "dark");
      div.dataset.square = sq;
      if (map[sq]) {
        const span = document.createElement("span");
        span.className = "piece " + map[sq].color;
        span.textContent = map[sq].glyph;
        div.appendChild(span);
      }
      // lichess-style coordinates
      if (file === "a") {
        const c = document.createElement("span");
        c.className = "coord rank";
        c.textContent = rank;
        div.appendChild(c);
      }
      if (rank === 1) {
        const c = document.createElement("span");
        c.className = "coord file";
        c.textContent = file;
        div.appendChild(c);
      }
      if (sq === selected) div.classList.add("sel");
      if (targetSquares.includes(sq)) div.classList.add("target");
      if (sq === state.last_from || sq === state.last_to) div.classList.add("lastmove");
      if (state.check && map[sq] && map[sq].glyph === GLYPHS.k &&
          ((state.turn === "white") === (map[sq].color === "white"))) {
        div.classList.add("check");
      }
      div.addEventListener("click", () => onSquare(sq));
      board.appendChild(div);
    }
  }
  if (animate && state.last_from && state.last_to) {
    const piece = board.querySelector(`[data-square="${state.last_to}"] .piece`);
    if (piece) {
      const cell = board.clientWidth / 8;
      const from = squareXY(state.last_from);
      const to = squareXY(state.last_to);
      piece.style.setProperty("--dx", `${(from.x - to.x) * cell}px`);
      piece.style.setProperty("--dy", `${-(from.y - to.y) * cell}px`);
      piece.classList.add("moving");
    }
  }
}

function renderCaptured() {
  const top = $("capturedTop");
  const bottom = $("capturedBottom");
  top.innerHTML = "";
  bottom.innerHTML = "";
  if (!state) return;
  const counts = {};
  for (const ch of state.fen.split(" ")[0]) {
    if (/[a-zA-Z]/.test(ch)) counts[ch] = (counts[ch] || 0) + 1;
  }
  const initial = { P: 8, N: 2, B: 2, R: 2, Q: 1, p: 8, n: 2, b: 2, r: 2, q: 1 };
  const whiteLost = []; // black pieces white captured
  const blackLost = [];
  let whiteAdv = 0;
  for (const [ch, n] of Object.entries(initial)) {
    const missing = n - (counts[ch] || 0);
    for (let i = 0; i < missing; i++) {
      if (ch === ch.toUpperCase()) blackLost.push(ch);
      else whiteLost.push(ch);
    }
  }
  for (const ch of blackLost) whiteAdv += VALUES[ch.toLowerCase()];
  for (const ch of whiteLost) whiteAdv -= VALUES[ch.toLowerCase()];
  const order = ["q", "r", "b", "n", "p"];
  const byVal = (a, b) => order.indexOf(a.toLowerCase()) - order.indexOf(b.toLowerCase());
  // top: pieces you captured (black pieces)
  whiteLost.length && whiteLost.sort(byVal);
  for (const ch of whiteLost) top.append(glyphSpan(ch.toLowerCase(), "black"));
  if (whiteAdv > 0) top.append(advSpan(whiteAdv));
  // bottom: pieces they captured (white pieces)
  blackLost.length && blackLost.sort(byVal);
  for (const ch of blackLost) bottom.append(glyphSpan(ch.toLowerCase(), "white"));
  if (whiteAdv < 0) bottom.append(advSpan(-whiteAdv));
}

function glyphSpan(type, color) {
  const s = document.createElement("span");
  s.className = "piece-glyph " + color;
  s.textContent = GLYPHS[type];
  return s;
}

function advSpan(n) {
  const s = document.createElement("span");
  s.className = "adv";
  s.textContent = `+${n}`;
  return s;
}

// ---------------------------------------------------------------- status / moves

function renderStatus() {
  const el = $("status");
  el.classList.remove("you-turn", "busy", "over");
  if (!state) { el.textContent = "Connecting..."; return; }
  if (state.game_over) {
    let msg = "Game over";
    if (state.status === "checkmate") msg = `Checkmate - ${state.winner === "white" ? "your team wins!" : "Viktor & Mira win."}`;
    else if (state.status === "stalemate") msg = "Stalemate - draw.";
    else msg = "Draw.";
    el.textContent = msg;
    el.classList.add("over");
  } else if (state.turn === "white") {
    if (flags.autopilot) { el.textContent = "Ava is playing your move..."; el.classList.add("busy"); }
    else if (flags.partner) { el.textContent = "Kai is choosing your move..."; el.classList.add("busy"); }
    else { el.textContent = "Your move (White)" + (state.check ? " - CHECK!" : ""); el.classList.add("you-turn"); }
  } else {
    el.textContent = flags.opponents ? "Viktor & Mira are conferring..." : "Black to move";
    el.classList.add("busy");
  }
  $("suggestBtn").disabled = state.turn !== "white" || flags.partner || flags.opponents || state.game_over;
  $("kaiPlayBtn").disabled = state.turn !== "white" || flags.partner || flags.opponents || state.game_over;
  $("moveInput").disabled = state.turn !== "white" || flags.opponents || state.game_over;
}

function renderMoves() {
  const ol = $("moveList");
  ol.innerHTML = "";
  const h = state ? state.history : [];
  for (let i = 0; i < h.length; i += 2) {
    const li = document.createElement("li");
    li.dataset.no = i / 2 + 1;
    const b = document.createElement("b");
    b.textContent = h[i];
    li.appendChild(b);
    if (h[i + 1]) {
      li.appendChild(document.createTextNode(" " + h[i + 1]));
    }
    ol.appendChild(li);
  }
  ol.scrollTop = ol.scrollHeight;
}

// ---------------------------------------------------------------- chat

const AVATAR_CLASS = {
  Kai: "kai", Ava: "ava", Viktor: "viktor", Mira: "mira", Arbiter: "arbiter",
};

function appendBubble(container, who, text, whoClass, mine = false) {
  const { visible, move } = splitDirective(text);
  const wrap = document.createElement("div");
  wrap.className = "msg" + (mine ? " mine" : "");
  if (!mine && AVATAR_CLASS[who]) {
    const av = document.createElement("span");
    av.className = "avatar mini " + AVATAR_CLASS[who];
    av.textContent = who === "Arbiter" ? "\u2696" : who[0];
    wrap.appendChild(av);
  }
  const div = document.createElement("div");
  div.className = "bubble " + whoClass;
  const w = document.createElement("span");
  w.className = "who";
  w.textContent = who;
  div.appendChild(w);
  div.appendChild(document.createTextNode(visible || (move ? "plays" : "\u2026")));
  if (move) {
    const chip = document.createElement("span");
    chip.className = "movechip";
    chip.textContent = "\u25B8 " + move;
    div.appendChild(chip);
  }
  wrap.appendChild(div);
  container.appendChild(wrap);
  scrollLog(container);
  if (!replaying && who !== "You") {
    speak(visible || (move ? `plays ${move}` : ""), who);
  }
}

function renderChatLog(entries) {
  const chat = $("chat");
  chat.innerHTML = "";
  replaying = true;
  for (const e of entries) {
    const who = e.name === "Ava" ? "Ava" : e.role === "you" ? "You" : "Kai";
    appendBubble(chat, who, e.text, who === "You" ? "you" : who, who === "You" || who === "Ava");
  }
  replaying = false;
}

function renderOppLog(entries) {
  const feed = $("oppFeed");
  feed.innerHTML = "";
  replaying = true;
  for (const e of entries) appendBubble(feed, e.name, e.text, e.name);
  replaying = false;
}

function setSuggestion(s) {
  suggestion = s;
  const el = $("suggestion");
  el.innerHTML = "";
  if (!s || !state || state.game_over || state.turn !== "white") {
    el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  const av = document.createElement("span");
  av.className = "avatar mini kai";
  av.textContent = "K";
  const label = document.createElement("span");
  label.innerHTML = "Kai suggests <span class='san'></span>";
  label.querySelector(".san").textContent = s.san;
  const btn = document.createElement("button");
  btn.textContent = "Play it";
  btn.className = "take";
  btn.addEventListener("click", () => doUserMove(s.san));
  el.appendChild(av);
  el.appendChild(label);
  el.appendChild(btn);
}

function setThinkingFlag(who, active) {
  if (who === "opponents") flags.opponents = active;
  if (who === "partner") flags.partner = active;
  if (who === "autopilot") flags.autopilot = active;
  $("kaiTyping").classList.toggle("hidden", !flags.partner);
  $("oppTyping").classList.toggle("hidden", !flags.opponents);
  renderStatus();
}

function maybeAutoPlay() {
  if (!autoKaiOn || avaOn || !state || state.game_over || state.turn !== "white") return;
  if (flags.partner || flags.opponents || flags.autopilot) return;
  setTimeout(() => {
    if (state && state.turn === "white" && !state.game_over && !flags.partner && !flags.opponents && !flags.autopilot) {
      post("/api/partner_move");
    }
  }, 900);
}

// ---------------------------------------------------------------- actions

function onSquare(sq) {
  if (!state || state.game_over || state.turn !== "white" || flags.opponents) return;
  const mv = selected ? state.legal_moves.find((m) => m.from === selected && m.to === sq) : null;
  if (mv) {
    doUserMove(mv.san);
    return;
  }
  const piece = pieceMap(state.fen)[sq];
  if (piece && piece.color === "white") {
    selected = sq;
    targetSquares = state.legal_moves.filter((m) => m.from === sq).map((m) => m.to);
  } else {
    selected = null;
    targetSquares = [];
  }
  renderBoard();
}

async function doUserMove(san) {
  selected = null;
  targetSquares = [];
  const result = await post("/api/move", { move: san });
  if (result) renderBoard();
}

// ---------------------------------------------------------------- speech to text

const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognizing = false;
let recognition = null;

const PIECE_WORDS = { knight: "N", bishop: "B", rook: "R", queen: "Q", king: "K", pawn: "" };

function transcriptToMove(t) {
  let s = (t || "").toLowerCase().trim().replace(/[.,!?]/g, "");
  if (!s) return null;
  s = s.replace(/^(make |play |i play |my move is |move )+/, "");
  if (/castle (long|queen ?side|queenside)/.test(s)) return "O-O-O";
  if (/castle (short|king ?side|kingside)/.test(s) || /^castle/.test(s)) return "O-O";
  for (const [w, l] of Object.entries(PIECE_WORDS)) {
    s = s.replace(new RegExp(`\\b${w}\\b`, "g"), l);
  }
  s = s
    .replace(/\btakes?\b|\bcaptures?\b|\bexchange\b|\bex\b/g, "x")
    .replace(/\bto\b|\bgoes\b|\bfrom\b|\bsquare\b/g, "")
    .replace(/\s+/g, "");
  if (/^O-O(-O)?$/.test(s)) return s;
  if (/^[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8]$/.test(s)) return s;
  return null;
}

function setMicListening(on) {
  const btn = $("micBtn");
  recognizing = on;
  btn.classList.toggle("listening", on);
  btn.title = on ? "Listening... speak a move or chat" : "Speak - moves get played, chat goes to Kai";
}

function startRecognition() {
  if (!SpeechRec) {
    toast("Speech recognition needs Chrome or Edge (not supported here).");
    return;
  }
  if (recognizing) return;
  recognition = new SpeechRec();
  recognition.lang = "en-US";
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;

  let finalText = "";
  recognition.onstart = () => setMicListening(true);
  recognition.onerror = (e) => {
    if (e.error === "not-allowed" || e.error === "service-not-allowed") {
      toast("Microphone blocked - allow mic access in the browser.");
    } else if (e.error !== "aborted" && e.error !== "no-speech") {
      toast(`Mic error: ${e.error}`);
    }
  };
  recognition.onend = () => {
    setMicListening(false);
    if (finalText.trim()) routeTranscript(finalText.trim());
  };
  recognition.onresult = (e) => {
    for (let i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) finalText += e.results[i][0].transcript + " ";
    }
  };
  recognition.start();
}

function routeTranscript(text) {
  const move = transcriptToMove(text);
  if (move && state && !state.game_over && state.turn === "white" && !flags.opponents && !flags.autopilot) {
    toast(`\uD83C\uDFA4 Playing ${move}`);
    doUserMove(move);
    return;
  }
  toast(`\uD83C\uDFA4 You: ${text}`);
  post("/api/chat", { text });
}

// ---------------------------------------------------------------- game over modal

function showGameOver(ev) {
  const status = ev.status || (state && state.status);
  const winner = ev.winner || (state && state.winner);
  const icon = $("goIcon");
  const title = $("goTitle");
  const text = $("goText");
  if (status === "checkmate") {
    if (winner === "white") {
      icon.textContent = "\u2654";
      title.textContent = "Checkmate!";
      text.textContent = "Your team takes the game. Brilliant teamwork.";
    } else {
      icon.textContent = "\u265A";
      title.textContent = "Checkmate";
      text.textContent = "Viktor & Mira take this one. Rematch?";
    }
  } else if (status === "stalemate") {
    icon.textContent = "\u00BD";
    title.textContent = "Stalemate";
    text.textContent = "A draw - nobody could land the punch.";
  } else {
    icon.textContent = "\u00BD";
    title.textContent = "Draw";
    text.textContent = "The pieces have spoken: equality.";
  }
  $("gameOver").classList.remove("hidden");
  playSound("end");
}

// ---------------------------------------------------------------- websocket

function handle(ev) {
  switch (ev.type) {
    case "snapshot":
      state = ev.state;
      renderChatLog(ev.partner_log);
      renderOppLog(ev.opp_log);
      setSuggestion(ev.suggestion);
      flags = { opponents: ev.opponent_thinking, partner: ev.partner_thinking, autopilot: false };
      avaOn = !!ev.autopilot;
      $("avaToggle").checked = avaOn;      renderBoard();
      renderCaptured();
      renderStatus();
      renderMoves();
      if (state.game_over) showGameOver(state);
      break;
    case "state": {
      const prevPieces = state ? state.fen.split(" ")[0] : null;
      state = ev.state;
      selected = null;
      targetSquares = [];
      if (state.turn !== "white" || state.game_over) setSuggestion(null);
      const wasCapture = prevPieces
        ? prevPieces.replace(/\d/g, "").length !== state.fen.split(" ")[0].replace(/\d/g, "").length
        : false;
      renderBoard(true);
      renderCaptured();
      renderStatus();
      renderMoves();
      playSound(state.game_over ? "end" : state.check ? "check" : wasCapture ? "capture" : "move");
      maybeAutoPlay();
      break;
    }
    case "partner": {
      const who = ev.name === "Ava" ? "Ava" : ev.role === "you" ? "You" : "Kai";
      appendBubble($("chat"), who, ev.text, who === "You" ? "you" : who, who === "You" || who === "Ava");
      break;
    }
    case "opp":
      appendBubble($("oppFeed"), ev.name, ev.text, ev.name);
      break;
    case "thinking":
      setThinkingFlag(ev.who, ev.active);
      break;
    case "suggestion":
      setSuggestion({ san: ev.san, text: ev.text });
      if (!replaying) {
        speak(`Kai suggests ${ev.san}.`, "Kai");
        playSound("notify");
      }
      break;
    case "clear_suggestion":
      setSuggestion(null);
      break;
    case "error":
      toast(ev.text);
      break;
    case "game_over":
      renderStatus();
      showGameOver(ev);
      break;
    case "autopilot":
      avaOn = ev.enabled;
      $("avaToggle").checked = avaOn;
      renderStatus();
      break;
    case "feeds_cleared":
      $("chat").innerHTML = "";
      $("oppFeed").innerHTML = "";
      $("gameOver").classList.add("hidden");
      if ("speechSynthesis" in window) speechSynthesis.cancel();
      break;
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (e) => {
    try {
      handle(JSON.parse(e.data));
    } catch (err) {
      console.error("bad message", err);
    }
  };
  ws.onclose = () => setTimeout(connect, 2000);
  ws.onerror = () => ws.close();
}

// ---------------------------------------------------------------- wire up

$("moveForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("moveInput");
  const text = input.value.trim();
  if (text) {
    doUserMove(text);
    input.value = "";
  }
});

$("chatForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("chatInput");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  await post("/api/chat", { text });
});

$("suggestBtn").addEventListener("click", () => post("/api/suggest"));
$("kaiPlayBtn").addEventListener("click", () => post("/api/partner_move"));
$("newGameBtn").addEventListener("click", () => post("/api/new_game"));
$("goRematch").addEventListener("click", () => post("/api/new_game"));

$("voiceBtn").addEventListener("click", () => {
  voiceOn = !voiceOn;
  const btn = $("voiceBtn");
  btn.classList.toggle("active", voiceOn);
  btn.setAttribute("aria-pressed", String(voiceOn));
  btn.innerHTML = (voiceOn ? "&#128266;" : "&#128263;") + " Voice";
  if (!voiceOn) {
    if ("speechSynthesis" in window) speechSynthesis.cancel();
    return;
  }
  if (!("speechSynthesis" in window)) {
    toast("This browser has no speech synthesis - try Edge or Chrome.");
    voiceOn = false;
    btn.classList.remove("active");
    btn.innerHTML = "&#128263; Voice";
    return;
  }
  refreshVoices();
  const n = voiceCache.length;
  speak(
    n > 0
      ? `Voice narration on. ${n} English voice${n === 1 ? "" : "s"} found. Good luck!`
      : "Voice narration on. Good luck!",
    "Arbiter"
  );
});

$("autoKaiToggle").addEventListener("change", (e) => {
  autoKaiOn = e.target.checked;
  if (autoKaiOn) maybeAutoPlay();
});

$("avaToggle").addEventListener("change", async (e) => {
  const wanted = e.target.checked;
  const result = await post("/api/autopilot", { enabled: wanted });
  if (!result) e.target.checked = !wanted;
});

$("micBtn").addEventListener("click", () => {
  if (recognizing) {
    if (recognition) recognition.stop();
    return;
  }
  startRecognition();
});

connect();