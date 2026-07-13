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
  const starters = document.getElementById("starters");
  if (!form) return;

  const messages = [];
  let config = {};
  let busy = false;

  function renderSay(text, withCursor) {
    sayEl.textContent = text;
    if (withCursor) {
      const c = document.createElement("span");
      c.className = "cur";
      sayEl.appendChild(c);
    }
  }

  function pushEcho(userText) {
    const row = document.createElement("div");
    row.className = "row";
    const q = document.createElement("span");
    q.className = "q";
    q.textContent = "> ";
    row.append(q, document.createTextNode(userText));
    echo.appendChild(row);
    // keep the last three; fade the older ones so only the recent stay legible
    const rows = [...echo.children];
    while (rows.length > 3) echo.removeChild(rows.shift());
    rows.forEach((r, i) => (r.style.opacity = i === rows.length - 1 ? "1" : i === rows.length - 2 ? "0.45" : "0.2"));
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
    pushEcho(text);
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
        // 503 (no key / ceiling) or 429 (caps) → run what we have, else the form
        if (config.titles && config.titles.length) toResults(config);
        else fallbackToForm();
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
  starters.addEventListener("click", (e) => {
    const btn = e.target.closest(".starter");
    if (btn) send(btn.textContent);
  });
})();
