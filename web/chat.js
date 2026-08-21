// ── chat.js ────────────────────────────────────────────────────────────────
// The conversation, and it IS the product.
//
// This used to fill a config: the model returned {reply, config, ready}, the client
// watched for titles + location, then handed off to /api/score and switched to a board of
// 200 scored rows. That design is gone. The model now runs its own search loop server-side
// (POST /api/chat) — it interviews, decides what to search for, searches several times,
// reads the postings, and calls `recommend` with the jobs it chose.
//
// So there is no config to fill, no readiness to detect, no view to switch to. The answer
// arrives IN the conversation, with its cards attached, because that is what it is: a
// person asked someone to find them work and they came back with five jobs and reasons.
//
// The search activity is shown rather than hidden. The run that produced this design ran
// nine searches and said what each one was testing; a spinner would throw that away, and
// it is the part that shows the person they were actually listened to.
(function () {
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const sayEl = document.getElementById("chat-say");
  const echo = document.getElementById("echo");
  const heroEl = document.getElementById("hero");
  const doorExtra = document.getElementById("doorextra");
  const chipsEl = document.getElementById("chips");
  const hintEl = document.getElementById("chips-hint");
  const stepsEl = document.getElementById("steps");
  const pulse = document.getElementById("pulse");
  const sendBtn = form ? form.querySelector(".chat-send") : null;
  if (!form || !input) return;

  const OPENER = "What kind of work are you looking for?";
  const messages = [];
  let busy = false;

  // The old UI advertised a fixed four-question interview with step dots. The model now
  // asks as many questions as it needs — "four is a floor, not a target" — so a progress
  // indicator would be lying about a length nobody knows in advance.
  if (stepsEl) stepsEl.remove();
  if (chipsEl) chipsEl.remove();
  if (hintEl) hintEl.remove();

  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  function typeInto(text) {
    if (sayEl) sayEl.textContent = text || "";
  }

  function enterConversation() {
    document.body.classList.add("talking");
    if (heroEl) heroEl.classList.add("shrunk");
    if (doorExtra) doorExtra.setAttribute("hidden", "");
  }

  function block(cls, html) {
    const d = document.createElement("div");
    d.className = cls;
    d.innerHTML = html;
    echo.appendChild(d);
    d.scrollIntoView({ behavior: "smooth", block: "end" });
    return d;
  }

  function pushExchange(question, answer) {
    if (!echo) return;
    if (question) block("echo-q", esc(question));
    if (answer) block("echo-a", esc(answer));
  }

  // What it searched, in its own words, AS IT HAPPENS. `why` is a required tool argument
  // for exactly this. These used to render all at once after the turn finished, which
  // meant up to three minutes of spinner and then a summary of things already done — the
  // work is the interesting part of this product and it was the part nobody could see.
  let actList = null;

  function actLine(ev) {
    if (ev.tool === "search_jobs") {
      // The titles are shown, not just `why`. Watching it pivot to "1st Shift Supervisor"
      // after someone says they want off nights is what shows the reasoning is real.
      const terms = (ev.titles || []).map((t) => `<code>${esc(t)}</code>`).join(" ");
      const where = ev.location ? ` in ${esc(ev.location)}` : "";
      return `<li><b>searched</b> ${esc(ev.why || "")}${terms ? `<br><span class="act-terms">${terms}${where}</span>` : ""} <span class="act-n">${ev.returned} found</span></li>`;
    }
    if (ev.tool === "read_jobs") return `<li><b>read</b> ${ev.returned} postings in full</li>`;
    if (ev.tool === "recommend") return `<li><b>chose</b> ${ev.returned}</li>`;
    return "";
  }

  // One live event. Appends into a single list so the steps stack in order rather than
  // each becoming its own block.
  function pushStep(ev) {
    if (!echo) return;
    const html = actLine(ev);
    if (!html) return;
    if (!actList) {
      const b = block("echo-act", "<ul></ul>");
      actList = b.querySelector("ul");
    }
    actList.insertAdjacentHTML("beforeend", html);
    actList.lastElementChild.scrollIntoView({ behavior: "smooth", block: "end" });
  }

  // The non-streaming fallback still has to work: if the stream is unavailable the turn
  // completes normally and the whole trace arrives at once, exactly as before.
  function pushActivity(trace) {
    if (!echo || !trace || !trace.length) return;
    trace.forEach((t) => pushStep({ ...t, titles: (t.args && t.args.titles) || [], location: (t.args && t.args.location) || "" }));
  }

  function money(job) {
    if (job.salary) return esc(job.salary);
    if (job.salary_min) return "$" + Math.round(job.salary_min).toLocaleString();
    return "";
  }

  function pushPicks(picks) {
    if (!echo || !picks || !picks.length) return;
    const cards = picks
      .map(
        (j) => `
      <article class="pick glass">
        <a class="pick-title" href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a>
        <p class="pick-meta">
          <span class="pick-co">${esc(j.company)}</span>
          ${j.location ? `<span>${esc(j.location)}</span>` : ""}
          ${money(j) ? `<span class="pick-pay">${money(j)}</span>` : ""}
          ${j.remote ? `<span class="pick-tag">${esc(j.remote)}</span>` : ""}
        </p>
        ${j.why ? `<p class="pick-why">${esc(j.why)}</p>` : ""}
        ${j.caveat ? `<p class="pick-caveat">${esc(j.caveat)}</p>` : ""}
        <a class="pick-go" href="${esc(j.url)}" target="_blank" rel="noopener">Apply on the employer's site →</a>
      </article>`
      )
      .join("");
    block("echo-picks", cards);
  }

  function setBusy(on) {
    busy = on;
    input.disabled = on;
    if (sendBtn) sendBtn.disabled = on;
    if (pulse) pulse.classList.toggle("thinking", on);
    document.body.classList.toggle("waiting", on);
  }

  async function send(text) {
    if (busy) return;
    const question = sayEl ? sayEl.textContent : "";
    enterConversation();
    pushExchange(question, text);
    typeInto("");
    setBusy(true);
    messages.push({ role: "user", content: text });
    actList = null; // a new turn starts a new activity list
    try {
      const r = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages }),
      });
      if (r.status === 503 || r.status === 429) {
        typeInto("The assistant is unavailable right now — try again in a minute.");
        return;
      }
      const d = await readStream(r, pushStep);
      if (!d) {
        typeInto("Something went wrong reaching the model.");
        return;
      }
      if (d.__fallbackTrace) pushActivity(d.trace);
      if (d.picks && d.picks.length) pushPicks(d.picks);
      if (d.nothing_fits && (!d.picks || !d.picks.length))
        block("echo-act", "<ul><li>Nothing in the pool genuinely fits — see below.</li></ul>");
      const reply = d.reply || (d.error ? "Something went wrong reaching the model." : "");
      messages.push({ role: "assistant", content: reply });
      typeInto(reply);
    } catch {
      typeInto("Couldn't reach the server — check your connection and try again.");
    } finally {
      setBusy(false);
      input.focus();
    }
  }

  // Read Server-Sent Events off a POST. EventSource cannot POST, so the frames are
  // parsed by hand — SSE is line-oriented and a frame ends at a blank line, which is the
  // whole protocol. A chunk can split mid-frame, so the tail is carried over; forgetting
  // that yields JSON parse errors only under load, when frames start arriving coalesced.
  async function readStream(r, onStep) {
    if (!r.body || !r.ok) {
      // No stream available — fall back to the plain endpoint so the turn still completes.
      const d = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages }),
      }).then((x) => x.json()).catch(() => null);
      return d ? { ...d, __fallbackTrace: true } : null;
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let result = null;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) !== -1) {
        const frame = buf.slice(0, i);
        buf = buf.slice(i + 2);
        if (!frame.startsWith("data:")) continue;
        let ev;
        try {
          ev = JSON.parse(frame.slice(5).trim());
        } catch {
          continue; // a malformed frame must not kill the turn
        }
        if (ev.type === "tool") onStep(ev);
        else if (ev.type === "done") result = ev.result;
        else if (ev.type === "error") result = { reply: "", error: ev.error };
        // Unknown types are ignored on purpose — the server may add more.
      }
    }
    return result;
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const t = input.value.trim();
    if (!t) return;
    input.value = "";
    if (sendBtn) sendBtn.classList.remove("ready");
    send(t);
  });
  input.addEventListener("input", () => {
    if (sendBtn) sendBtn.classList.toggle("ready", input.value.trim() !== "");
  });

  typeInto(OPENER);
})();
