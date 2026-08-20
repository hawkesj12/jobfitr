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

  // What it searched, in its own words. `why` is a required tool argument for exactly this.
  function pushActivity(trace) {
    if (!echo || !trace || !trace.length) return;
    const lines = trace
      .map((t) => {
        if (t.tool === "search_jobs")
          return `<li><b>searched</b> ${esc(t.why || "")} <span class="act-n">${t.returned} found</span></li>`;
        if (t.tool === "read_jobs")
          return `<li><b>read</b> ${t.returned} postings in full</li>`;
        if (t.tool === "recommend") return `<li><b>chose</b> ${t.returned}</li>`;
        return "";
      })
      .filter(Boolean)
      .join("");
    if (lines) block("echo-act", `<ul>${lines}</ul>`);
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
    try {
      const r = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages }),
      });
      if (r.status === 503 || r.status === 429) {
        typeInto("The assistant is unavailable right now — try again in a minute.");
        return;
      }
      const d = await r.json();
      pushActivity(d.trace);
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
