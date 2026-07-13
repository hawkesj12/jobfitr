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
  if (!form) return;

  const OPENER = "What job are you chasing?";
  const reduceMo = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const messages = [];
  let config = {};
  let busy = false;
  let prefetched = false;
  let currentQuestion = OPENER; // the question the next answer responds to
  let typeToken = 0;

  function renderSay(text, withCursor) {
    sayEl.textContent = text;
    if (withCursor) {
      const c = document.createElement("span");
      c.className = "cur";
      sayEl.appendChild(c);
    }
  }

  // Type `text` into the assistant line ABOVE the box, with a blinking cursor. A newer
  // message supersedes an in-flight one (typeToken).
  function typeInto(text) {
    const token = ++typeToken;
    if (reduceMo || !text) {
      renderSay(text || "", false);
      return;
    }
    let i = 0;
    (function step() {
      if (token !== typeToken) return;
      renderSay(text.slice(0, i), true);
      if (i < text.length) {
        i++;
        setTimeout(step, 18 + Math.random() * 22);
      } else {
        renderSay(text, false);
      }
    })();
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

  function toConfirm() {
    if (window.jobfitr && window.jobfitr.confirm) window.jobfitr.confirm(config);
    else if (window.jobfitr) window.jobfitr.run(config);
  }
  function fallbackToForm() {
    if (window.jobfitr) window.jobfitr.showForm();
  }

  async function send(text) {
    if (busy || !text.trim()) return;
    busy = true;
    input.value = "";
    pushEcho(currentQuestion, text);
    messages.push({ role: "user", content: text });
    renderSay("", true);

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages, config }),
      });
      if (!resp.ok) {
        // 503 (no key / daily ceiling) or 429 (caps): the assistant is unavailable.
        // Use what we have, or hand to the form — with a warm note, never a failure.
        if (hasTitles()) {
          typeInto("The assistant is resting — pulling your matches from what you told me.");
          setTimeout(toConfirm, 900);
        } else {
          typeInto("The assistant is resting just now — switching you to the quick form.");
          setTimeout(fallbackToForm, 1100);
        }
        return;
      }
      const data = await resp.json();
      if (data.error) {
        if (hasTitles()) toConfirm();
        else fallbackToForm();
        return;
      }
      config = data.config || config;
      persist();
      const reply = data.reply || "Got it.";
      messages.push({ role: "assistant", content: reply });
      currentQuestion = reply;
      typeInto(reply);
      maybePrefetch();
      if (data.ready) setTimeout(toConfirm, reply.length * 22 + 500);
    } catch {
      if (hasTitles()) toConfirm();
      else fallbackToForm();
    } finally {
      busy = false;
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    send(input.value);
  });

  // Open the conversation: type the first question ABOVE the box.
  setTimeout(() => typeInto(OPENER), 450);
})();
