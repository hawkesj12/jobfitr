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

  const messages = [];
  let config = {};
  let busy = false;
  let prefetched = false;
  let currentQuestion = OPENER; // the question the next answer responds to
  let answered = 0;
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

  // ── contextual chips: ONE TAP = ONE ANSWER. ──────────────────────────────────────
  // These used to only FILL the box and leave the user to find Enter — a dead end
  // dressed as a button, and the single most confusing moment in the interview. A tap
  // now sends. Anything the user has already typed is carried along, so a chip still
  // composes with free text; typing a comma-separated list by hand works as before.
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
  }
  function selectChip(label) {
    const typed = input.value.trim().replace(/,\s*$/, "");
    send(typed ? typed + ", " + label : label);
  }
  function setChips(list) {
    chipPool = Array.isArray(list) ? list.filter((c) => typeof c === "string" && c.trim()) : [];
    renderChips();
  }

  // Search runs straight from the conversation once ready — no form, ever.
  function toResults() {
    if (window.jobfitr) window.jobfitr.run(config);
  }

  async function send(text) {
    if (busy || !text.trim()) return;
    busy = true;
    enterConversation();
    input.value = "";
    setChips([]); // clear last question's chips until the next turn returns fresh ones
    pushEcho(currentQuestion, text);
    messages.push({ role: "user", content: text });
    answered = Math.min(answered + 1, TOTAL_STEPS - 1);
    renderSteps();
    typeInto("");

    // Assistant unavailable (503/429/upstream/network). If we already have a role +
    // place, just search; otherwise ask them to try again — never the form.
    const unavailable = () => {
      if (hasTitles() && hasLocation()) {
        typeInto("Pulling your matches from what you told me…");
        setTimeout(toResults, 900);
      } else {
        typeInto("One sec — I lost my train of thought. Say that again?");
      }
    };

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages, config }),
      });
      const data = resp.ok ? await resp.json() : null;
      if (!data || data.error) {
        unavailable();
        return;
      }
      config = data.config || config;
      persist();
      const reply = data.reply || "Got it.";
      messages.push({ role: "assistant", content: reply });
      currentQuestion = reply;
      typeInto(reply);
      setChips(data.chips);
      maybePrefetch();
      // The reply is on screen instantly now, so the old length-proportional wait
      // (which paced the typewriter) would just be dead time before the board.
      if (data.ready) setTimeout(toResults, 650);
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

  // A starter button is just a first answer typed for you.
  window.jobfitr = window.jobfitr || {};
  window.jobfitr.seed = (text) => send(text);

  // Open the conversation with the first question already on screen.
  renderSteps();
  typeInto(OPENER);
})();
