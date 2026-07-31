"use strict";

// The conversational front door. One JSON turn per message: POST /api/chat →
// {reply, config, ready}. The model converses (reply, always above the box) while the
// config fills; the moment titles + location are known the client warms the results
// cache (/api/prefetch) so the search is instant. Falls back to the search form if the
// AI is unavailable (no key, daily ceiling, or an upstream error).

(function () {
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const sayEl = document.getElementById("chat-say");
  const echo = document.getElementById("echo");
  const chipsEl = document.getElementById("chips");
  if (!form) return;

  const OPENER = "What job are you chasing?";
  const CHIP_SHOW = 4; // how many chips to show at once (pool is up to 8; refills on pick)
  const TOTAL_STEPS = 4; // the interview is four questions — show where you are
  const heroEl = document.getElementById("hero");
  const doorExtra = document.getElementById("doorextra");
  const stepsEl = document.getElementById("steps");
  const hintEl = document.getElementById("chips-hint");
  const CHIP_HINT = "tap to add — press ↵ when you're done";
  let turnHint = ""; // the server's explanation for THIS question, when it sends one
  const sendBtn = form.querySelector(".chat-send");

  const messages = [];
  let config = {};
  let busy = false;
  let prefetched = false;
  let currentQuestion = OPENER; // the question the next answer responds to
  let answered = 0;
  // Whether we are refining is a property of what is ON SCREEN, not of how we got
  // there: a shared #q= link opens the board without chat.js ever running the
  // interview, and a local flag left those sessions talking to the intake prompt.
  const onBoard = () => document.body.classList.contains("board");
  let chipPool = []; // suggestions for the current question; CHIP_SHOW render, rest reserve

  // Render the assistant line. The question appears AT ONCE — the old version typed it
  // out character by character, which delays interaction to simulate work that has
  // already finished. Faking latency where none exists is theater, not presence; the
  // blinking cursor carries the aliveness without costing the user a second.
  function typeInto(text) {
    sayEl.textContent = text || "";
    if (text) {
      const c = document.createElement("span");
      c.className = "cur";
      sayEl.appendChild(c);
    }
  }

  function renderSteps() {
    if (!stepsEl) return;
    stepsEl.textContent = "";
    for (let i = 0; i < TOTAL_STEPS; i++) {
      const d = document.createElement("i");
      if (i <= answered) d.classList.add("on");
      stepsEl.appendChild(d);
    }
  }

  // Once the conversation is underway the marketing block has done its job and would
  // only compete with the answers.
  function enterConversation() {
    if (heroEl) heroEl.hidden = true;
    if (doorExtra) doorExtra.hidden = true;
  }

  function pushEcho(question, answer) {
    const row = document.createElement("div");
    row.className = "row";
    const q = document.createElement("div");
    q.className = "q-asked";
    q.textContent = question;
    const a = document.createElement("div");
    a.className = "a-given";
    a.textContent = answer;
    row.append(q, a);
    echo.appendChild(row);
    // keep the last three exchanges; fade the older ones so only the recent stay legible
    const rows = [...echo.children];
    while (rows.length > 3) echo.removeChild(rows.shift());
    rows.forEach((r, i) => (r.style.opacity = i === rows.length - 1 ? "1" : i === rows.length - 2 ? "0.72" : "0.45"));
  }

  function persist() {
    try {
      localStorage.setItem("jobfitr.config", JSON.stringify(config));
    } catch {
      /* storage disabled — the session still works */
    }
  }

  function hasTitles() {
    return Array.isArray(config.titles) ? config.titles.length > 0 : !!config.titles;
  }
  function hasLocation() {
    const loc = config.location;
    return (typeof loc === "string" && loc.trim() !== "") || !!config.remote_only;
  }

  // The moment titles + location are both known, warm the results cache in the
  // background so the 3-4s live fetch overlaps the rest of the chat. Fire once.
  function maybePrefetch() {
    if (prefetched || !hasTitles() || !hasLocation()) return;
    prefetched = true;
    fetch("/api/prefetch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ titles: config.titles, location: config.location || "" }),
    }).catch(() => {
      prefetched = false; // let a later turn retry
    });
  }

  // ── contextual chips: tap to BUILD an answer, ↵ to send ──────────────────────────
  // Chips are multi-select on purpose — "Remote, Hybrid" or three skills at once is a
  // normal answer, so a tap ADDS to the box rather than committing the turn. The tapped
  // chip is removed and the next reserve slides in, so it visibly falls away and can't
  // be double-added. What makes this readable rather than a dead end is the send button:
  // it lights up the moment there is something to send (see syncReady), so the way
  // forward is on screen instead of being an Enter key you have to know about.
  function renderChips() {
    if (!chipsEl) return;
    chipsEl.textContent = "";
    for (const label of chipPool.slice(0, CHIP_SHOW)) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.textContent = label;
      b.addEventListener("click", () => selectChip(label));
      chipsEl.appendChild(b);
    }
    renderHint();
  }
  // The line under the chips. The server sends a `hint` on the two questions whose
  // mechanic a user cannot guess — what makes a good boost, and that "avoid" REMOVES
  // listings rather than nudging them. That is worth more than the generic chip
  // instruction, so it wins when present and the generic line fills in otherwise.
  function renderHint() {
    if (!hintEl) return;
    const text = turnHint || (chipPool.length ? CHIP_HINT : "");
    hintEl.textContent = text;
    hintEl.classList.toggle("chips-hint-strong", !!turnHint);
    hintEl.hidden = !text;
  }
  function selectChip(label) {
    const cur = input.value.trim().replace(/,\s*$/, "");
    input.value = cur ? cur + ", " + label : label;
    input.focus();
    chipPool = chipPool.filter((c) => c !== label); // remove it; the next reserve slides in
    renderChips();
    syncReady();
  }

  // The send button is the answer to "what do I do now?" — dim while the box is empty,
  // lit the moment there is an answer to send.
  function syncReady() {
    if (!sendBtn) return;
    sendBtn.classList.toggle("ready", input.value.trim() !== "");
  }
  function setChips(list) {
    chipPool = Array.isArray(list) ? list.filter((c) => typeof c === "string" && c.trim()) : [];
    renderChips();
  }

  // Search runs straight from the conversation once ready — no form, ever.
  function toResults() {
    if (window.jobfitr) window.jobfitr.run(config); // app.js re-labels the box for the board
  }

  // Nothing about an exchange is recorded until the turn actually LANDS. Recording it
  // up front looked harmless and was not: a failed turn left the answer sitting in the
  // transcript as though it had been understood, while the config never received it —
  // and the user turn stayed in `messages`, so retyping the answer sent it to the model
  // twice, with a second identical transcript row to match. A turn that did not happen
  // must leave the conversation exactly where it was.
  async function send(text) {
    if (busy || !text.trim()) return;
    busy = true;
    // Once the board is up, ITS config is the truth — the user may have arrived by a
    // shared link (this module never ran the interview) or dropped a criteria pill
    // since the last turn. Refining against a stale local copy silently discarded
    // whatever it was missing.
    if (onBoard() && window.jobfitr && window.jobfitr.currentConfig) {
      config = { ...window.jobfitr.currentConfig() };
    }
    enterConversation();
    // on the board the panel may be closed — never let a reply land out of sight
    if (onBoard() && window.jobfitrShowRefine) window.jobfitrShowRefine();
    input.value = "";
    syncReady();
    typeInto("");

    const asked = currentQuestion; // the question THIS answer is responding to
    const priorChips = chipPool.slice(); // restored if the turn never lands
    const turn = [...messages, { role: "user", content: text }];
    turnHint = ""; // the previous question's explanation must not outlive its question
    setChips([]); // clear last question's chips until the next turn returns fresh ones

    // Assistant unavailable (503/429/upstream/network). If we already have a role +
    // place, just search; otherwise ask them to try again — never the form.
    const unavailable = () => {
      if (hasTitles() && hasLocation()) {
        typeInto("Pulling your matches from what you told me…");
        setTimeout(toResults, 900);
        return;
      }
      // hand the answer back so a retry is one keypress, not a retype
      input.value = text;
      syncReady();
      setChips(priorChips);
      typeInto("One sec — I lost my train of thought. Say that again?");
    };

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // `refining` tells the server the interview is over and this is an edit to a
        // search the user is already looking at — without it the assistant re-runs the
        // intake script and the requested change is silently dropped.
        body: JSON.stringify({ messages: turn, config, refining: onBoard() }),
      });
      const data = resp.ok ? await resp.json() : null;
      if (!data || data.error) {
        unavailable();
        return;
      }

      // the turn landed — only now does the exchange become part of the conversation
      messages.push({ role: "user", content: text });
      pushEcho(asked, text);
      answered = Math.min(answered + 1, TOTAL_STEPS - 1);
      renderSteps();

      const prev = JSON.stringify(config);
      config = data.config || config;
      // On the board, any config change IS the result of the refinement, so re-score on
      // the change itself rather than waiting for the model to volunteer `ready` — a
      // refinement that silently left the board untouched was the whole complaint.
      const changed = onBoard() && JSON.stringify(config) !== prev;
      persist();
      const reply = data.reply || "Got it.";
      messages.push({ role: "assistant", content: reply });
      currentQuestion = reply;
      typeInto(reply);
      turnHint = typeof data.hint === "string" ? data.hint : "";
      setChips(data.chips);
      maybePrefetch();
      // The reply is on screen instantly now, so the old length-proportional wait
      // (which paced the typewriter) would just be dead time before the board.
      if (data.ready || changed) setTimeout(toResults, 650);
    } catch {
      unavailable();
    } finally {
      busy = false;
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    send(input.value);
  });
  input.addEventListener("input", syncReady);

  // A starter button is just a first answer typed for you.
  window.jobfitr = window.jobfitr || {};
  window.jobfitr.seed = (text) => send(text);

  // Open the conversation with the first question already on screen.
  renderSteps();
  typeInto(OPENER);
})();
