"use strict";

// The conversational front door. Streams /api/chat (SSE over a POST), renders the
// assistant's tokens live, captures the set_config delta, and hands the finished
// config to window.jobfitr.run(). Fails gracefully to the 5-question form whenever
// the AI is unavailable (no key, daily ceiling, or an error).

(function () {
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const sayEl = document.getElementById("chat-say");
  const echo = document.getElementById("echo");
  if (!form) return;

  // ── the front door types its own question on load ───────────────────────────
  const QUESTION = "What job are you chasing?";
  const reduceMo = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function typeQuestion() {
    if (reduceMo) { input.placeholder = QUESTION; return; }
    let i = 0;
    (function step() {
      if (input.value) return; // the user started typing — stop, get out of the way
      input.placeholder = QUESTION.slice(0, i) + (i < QUESTION.length ? "▌" : "");
      if (i < QUESTION.length) { i++; setTimeout(step, 55 + Math.random() * 45); }
      else { input.placeholder = QUESTION; }
    })();
  }
  setTimeout(typeQuestion, 450);

  const messages = [];
  let config = {};
  let busy = false;
  let currentQuestion = QUESTION; // the question the next answer is responding to

  function renderSay(text, withCursor) {
    sayEl.textContent = text;
    if (withCursor) {
      const c = document.createElement("span");
      c.className = "cur";
      sayEl.appendChild(c);
    }
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
    rows.forEach((r, i) => (r.style.opacity = i === rows.length - 1 ? "1" : i === rows.length - 2 ? "0.5" : "0.28"));
  }

  function toResults(cfg) {
    const go = () => window.jobfitr && window.jobfitr.run(cfg);
    if (document.startViewTransition && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      document.startViewTransition(go);
    } else {
      go();
    }
  }

  function fallbackToForm() {
    if (window.jobfitr) window.jobfitr.showForm();
  }

  function handleEvent(block) {
    const lines = block.split("\n");
    let event = "message";
    const data = [];
    for (const line of lines) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trim());
    }
    const raw = data.join("\n");
    if (!raw) return null;
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return null;
    }
    return { event, parsed };
  }

  async function send(text) {
    if (busy || !text.trim()) return;
    busy = true;
    input.value = "";
    input.placeholder = ""; // drop the stale first-question placeholder once talking
    pushEcho(currentQuestion, text);
    messages.push({ role: "user", content: text });
    renderSay("", true);

    let assistant = "";
    let ready = false;
    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages, config }),
      });
      if (!resp.ok || !resp.body) {
        // 503 (no key / daily ceiling) or 429 (caps): the assistant is unavailable.
        // Run what we already have, or hand off to the quick form — with a warm note
        // so the switch never feels like a failure.
        if (config.titles && config.titles.length) {
          renderSay("The assistant is resting — pulling your matches from what you told me.", false);
          setTimeout(() => toResults(config), 700);
        } else {
          renderSay("The assistant is resting just now — switching you to the quick form.", false);
          setTimeout(fallbackToForm, 900);
        }
        return;
      }
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true }).replace(/\r/g, "");
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const ev = handleEvent(buf.slice(0, idx));
          buf = buf.slice(idx + 2);
          if (!ev) continue;
          if (ev.event === "token" && ev.parsed.text) {
            assistant += ev.parsed.text;
            renderSay(assistant, true);
          } else if (ev.event === "config") {
            config = ev.parsed.config || config;
            ready = !!ev.parsed.ready;
          } else if (ev.event === "error") {
            if (config.titles && config.titles.length) toResults(config);
            else fallbackToForm();
            return;
          }
        }
      }
      renderSay(assistant || "Got it.", false);
      messages.push({ role: "assistant", content: assistant });
      // the assistant's reply is the question the NEXT answer will respond to
      if (assistant) currentQuestion = assistant;
      if (ready) setTimeout(() => toResults(config), 450);
    } catch {
      if (config.titles && config.titles.length) toResults(config);
      else fallbackToForm();
    } finally {
      busy = false;
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    send(input.value);
  });
})();
