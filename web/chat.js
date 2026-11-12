// ── chat.js ────────────────────────────────────────────────────────────────
// The conversation, and it IS the product.
//
// The model runs its own search loop on the server (POST /api/chat/stream): it interviews,
// decides what to search for, searches several times, reads the postings, and calls
// `recommend`. The server then checks every pick against what the person asked for and
// against the posting's own text before any of it reaches this file.
//
// WHAT THIS FILE SENDS: a session id and the ONE new message. The transcript is held on
// the server. It used to be held here and POSTed whole on every turn, the assistant's
// turns included, so anyone with the network tab open could rewrite what the assistant
// had "said". There is nothing here to forge now.
//
// The search activity is shown rather than hidden, as it happens. It is the part that
// shows the person they were listened to, and a spinner would throw it away.
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
  const startersEl = document.getElementById("starters");
  const pulse = document.getElementById("pulse");
  const sendBtn = form ? form.querySelector(".chat-send") : null;
  if (!form || !input) return;

  const OPENER = "What kind of work are you looking for?";
  let session = null;
  let busy = false;

  // The old UI advertised a fixed four-question interview with step dots. The model asks
  // as many questions as it needs, so a progress indicator would be lying about a length
  // nobody knows in advance.
  if (stepsEl) stepsEl.remove();
  if (chipsEl) chipsEl.remove();
  if (hintEl) hintEl.remove();

  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  // Only http(s) links are ever written into an href. Every url here came from the
  // server's own store, but the page should not have to trust that.
  const href = (u) => (/^https?:\/\//i.test(u || "") ? esc(u) : "#");

  // Links and bare urls out. Every job is a card with its own link; a hundred-character
  // tracking url inside a sentence is unreadable no matter why it got there.
  function plainReply(text) {
    return (text || "")
      .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, "$1")
      .replace(/(^|\s)https?:\/\/\S+/g, "$1")
      .trim();
  }

  // The reply, rendered as what it is. This used to flatten markdown into one block, so a
  // heading ran into the paragraph after it and list numbers sat inline (the "wall of
  // text" from the 2026-11-11 runs). Everything is ESCAPED FIRST and only then given
  // structure, so nothing the model writes can become markup.
  function renderReply(text) {
    const inline = (s) => esc(s).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    const out = [];
    plainReply(text).split(/\n{2,}/).forEach((para) => {
      para.split("\n").forEach((line) => {
        const l = line.trim();
        if (!l) return;
        const head = l.match(/^#{1,6}\s+(.*)$/);
        const item = l.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
        if (head) out.push(`<span class="say-p say-h">${inline(head[1])}</span>`);
        else if (item) out.push(`<span class="say-li">${inline(item[1])}</span>`);
        else out.push(`<span class="say-p">${inline(l)}</span>`);
      });
    });
    return out.join("");
  }

  function say(text) {
    if (!sayEl) return;
    sayEl.classList.remove("working");
    // A question is one line and reads well large. A closing note is a paragraph, and at
    // the same size it filled a phone screen.
    sayEl.classList.toggle("long", (text || "").length > 180);
    sayEl.innerHTML = renderReply(text);
  }

  // The working line. It is on screen in the same frame as the click, before any request
  // is sent: the first reply used to be 18-35 seconds of nothing at all.
  let workTimer = null;
  let workSince = 0;
  let workLabel = "";
  function working(label) {
    if (!sayEl) return;
    workLabel = label;
    if (!workSince) workSince = Date.now();
    const paint = () => {
      const s = Math.round((Date.now() - workSince) / 1000);
      sayEl.classList.add("working");
      sayEl.innerHTML = `${esc(workLabel)}<span class="dots" aria-hidden="true"></span>${s >= 5 ? ` <span class="work-s">${s}s</span>` : ""}`;
    };
    paint();
    if (!workTimer) workTimer = setInterval(paint, 1000);
  }
  function stopWorking() {
    if (workTimer) clearInterval(workTimer);
    workTimer = null;
    workSince = 0;
    if (sayEl) sayEl.classList.remove("working");
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
    if (question) block("echo-q", renderReply(question));
    if (answer) block("echo-a", esc(answer));
  }

  // What it did, in its own words, AS IT HAPPENS.
  let actList = null;

  function actLine(ev) {
    if (ev.tool === "search_jobs") {
      // The titles are shown, not just `why`. Watching it pivot to "1st Shift Supervisor"
      // after someone says they want off nights is what shows the reasoning is real.
      const terms = (ev.titles || []).map((t) => `<code>${esc(t)}</code>`).join(" ");
      // A remote-only search is not "in Louisville" even when the person lives there.
      const where = ev.remote_only ? " · remote" : ev.location ? ` in ${esc(ev.location)}` : "";
      const n = Number(ev.returned) || 0;
      const fresh = typeof ev.new === "number" && ev.new !== n ? `, ${ev.new} new` : "";
      return `<li><b>searched</b> ${esc(ev.why || "")}${terms ? `<br><span class="act-terms">${terms}${where}</span>` : ""} <span class="act-n">${n} found${fresh}</span></li>`;
    }
    if (ev.tool === "read_jobs") return `<li><b>read</b> ${Number(ev.returned) || 0} postings in full</li>`;
    if (ev.tool === "recommend") {
      if (ev.sent_back)
        return `<li><b>checked</b> its reasons against the postings and sent ${ev.sent_back} back to be fixed</li>`;
      const brief = (ev.brief || []).length
        ? `<br><span class="act-terms">checked each one against: ${ev.brief.map(esc).join(" · ")}</span>`
        : "";
      const removed = ev.removed ? ` <span class="act-n">${ev.removed} removed by the checks</span>` : "";
      return `<li><b>chose</b> ${Number(ev.returned) || 0}${removed}${brief}</li>`;
    }
    return "";
  }

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

  function startLabel(ev) {
    if (ev.tool === "search_jobs") {
      const t = (ev.titles || [])[0];
      return t ? `Searching for ${t}` : "Searching";
    }
    if (ev.tool === "read_jobs") return ev.count ? `Reading ${ev.count} postings in full` : "Reading the postings";
    if (ev.tool === "recommend") return "Checking each pick against what you asked for";
    return "Working";
  }

  function posted(j) {
    if (typeof j.age_days === "number") {
      if (j.age_days <= 0) return "posted today";
      return j.age_days === 1 ? "posted yesterday" : `posted ${j.age_days} days ago`;
    }
    return j.posted ? `posted ${esc(j.posted)}` : "";
  }

  // "Apply on the employer's site" was printed on every card, including the ones that go
  // through Adzuna first. The front page promises each card says which it is.
  function goLabel(j) {
    if (!j.link || j.link === "employer") return "Apply on the employer's site →";
    const name = j.link.charAt(0).toUpperCase() + j.link.slice(1);
    return `Open on ${esc(name)}, which passes you to the employer →`;
  }

  function cardHtml(j) {
    const warnings = (j.warnings || []).slice();
    if (j.warning) warnings.unshift(j.warning);
    return `
      <article class="pick glass">
        <a class="pick-title" href="${href(j.url)}" target="_blank" rel="noopener noreferrer">${esc(j.title)}</a>
        <p class="pick-meta">
          <span class="pick-co">${esc(j.company)}</span>
          ${j.location ? `<span>${esc(j.location)}</span>` : ""}
          ${j.salary ? `<span class="pick-pay">${esc(j.salary)}</span>` : ""}
          ${j.remote ? `<span class="pick-tag">${esc(j.remote)}</span>` : ""}
          ${posted(j) ? `<span class="pick-age">${posted(j)}</span>` : ""}
        </p>
        ${j.why ? `<p class="pick-why">${esc(j.why)}</p>` : ""}
        ${j.quote ? `<blockquote class="pick-quote"><span class="pick-quote-lab">From the posting</span>“${esc(j.quote)}”</blockquote>` : ""}
        ${warnings.length ? `<ul class="pick-warn" aria-label="Before you apply">${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
        <a class="pick-go" href="${href(j.url)}" target="_blank" rel="noopener noreferrer">${goLabel(j)}</a>
      </article>`;
  }

  function ruledHtml(list) {
    if (!list || !list.length) return "";
    const rows = list
      .map(
        (r) => `
        <li>
          <a href="${href(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.title)}</a>
          <span class="ruled-co">${esc(r.company)}</span>
          ${r.by === "check" ? `<span class="ruled-by">removed by a check</span>` : ""}
          <span class="ruled-why">${esc(r.why_not)}</span>
          ${r.quote ? `<span class="ruled-quote">“${esc(r.quote)}”</span>` : ""}
        </li>`
      )
      .join("");
    return `<section class="ruled"><h3>Looked at and ruled out</h3><ul>${rows}</ul></section>`;
  }

  // One answer block per turn. `recommend` can run twice in a turn (the model searches
  // again after a check removes a pick), and the second result REPLACES the first.
  let answerEl = null;
  function pushAnswer(d) {
    if (!echo) return;
    const picks = d.picks || [];
    const html =
      (picks.length
        ? `<div class="echo-picks">${picks.map(cardHtml).join("")}</div>`
        : d.nothing_fits
          ? `<p class="none-fit">Nothing passed. No job is shown as a pick; the reasons are below.</p>`
          : "") + ruledHtml(d.ruled_out);
    if (!html) return;
    if (answerEl) answerEl.innerHTML = html;
    else answerEl = block("echo-answer", html);
  }

  function pushReceipt(r) {
    if (!echo || !r) return;
    const parts = [
      `${r.searches} ${r.searches === 1 ? "search" : "searches"}`,
      `${r.read} ${r.read === 1 ? "posting" : "postings"} read in full`,
      `${r.seconds}s`,
    ];
    if (r.cost) parts.push(`$${Number(r.cost).toFixed(2)} of model time`);
    if (r.model) parts.push(esc(r.model));
    block("receipt", `<span class="receipt-lab">What this answer took</span> ${parts.join(" · ")}`);
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
    const question = sayEl && !sayEl.classList.contains("working") ? sayEl.innerText : "";
    enterConversation();
    pushExchange(question, text);
    setBusy(true);
    working("Thinking");
    actList = null; // a new turn starts a new activity list
    answerEl = null;
    let streamed = "";
    const onEvent = (ev) => {
      if (ev.type === "session") session = ev.id;
      else if (ev.type === "status") working("Thinking");
      else if (ev.type === "text") {
        // The model's words, as it writes them.
        streamed += ev.delta || "";
        if (workTimer) { clearInterval(workTimer); workTimer = null; }
        sayEl.classList.remove("working");
        sayEl.textContent = plainReply(streamed);
      } else if (ev.type === "reset") {
        streamed = "";
        working("Working");
      } else if (ev.type === "tool_start") working(startLabel(ev));
      else if (ev.type === "tool") {
        pushStep(ev);
        working("Thinking");
      } else if (ev.type === "picks") pushAnswer(ev);
    };
    try {
      const body = JSON.stringify({ session, message: text });
      const r = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
      });
      if (r.status === 503) {
        const why = await r.json().then((x) => x.detail).catch(() => "");
        stopWorking();
        say(why === "daily_ceiling" || why === "daily_spend"
          ? "jobfitr has reached its limit for today. It resets at midnight Eastern."
          : "The assistant is unavailable right now. Try again in a minute.");
        return;
      }
      if (r.status === 429 || r.status === 409) {
        const why = await r.json().then((x) => x.detail).catch(() => "");
        stopWorking();
        say(why === "turn_cap"
          ? "This conversation has reached its length limit. Reload the page to start a new one."
          : why === "address_daily_cap"
            // Their own limit, not the site's: say so, so nobody thinks it is down.
            ? "You've used your conversations for today. It's a per-visitor limit that keeps jobfitr free; it resets at midnight Eastern."
            : "Too many messages at once. Wait a few seconds and send that again.");
        return;
      }
      const d = await readStream(r, onEvent, body);
      stopWorking();
      if (!d) {
        say("Something went wrong reaching the model. Send that again.");
        return;
      }
      if (d.session) session = d.session;
      if (d.__fallback) (d.trace || []).forEach((t) => pushStep({ ...t, titles: (t.args && t.args.titles) || [], location: (t.args && t.args.location) || "" }));
      if ((d.picks && d.picks.length) || (d.ruled_out && d.ruled_out.length) || d.nothing_fits) pushAnswer(d);
      pushReceipt(d.receipt);
      if (d.error === "service_unavailable")
        // The model provider refused the account (out of credit, or the key). Nothing a
        // retry from here can fix, and "something went wrong" made it look like a bug.
        say("The assistant is unavailable right now. Please try again later.");
      else if (d.error === "tool_budget_exhausted")
        say("I ran out of search budget before I could finish. Anything I had settled on is above; tell me what to narrow and I will try again.");
      else if (d.error && answerEl)
        // The picks arrived and the closing note did not. Say that, not "something went
        // wrong", which reads as though the answer above cannot be trusted.
        say("That is the full answer; the closing note did not come through. Ask a follow-up if you want it narrowed.");
      else say(d.reply || (d.error ? "Something went wrong reaching the model. Send that again." : ""));
    } catch {
      stopWorking();
      say("Couldn't reach the server. Check your connection and try again.");
    } finally {
      stopWorking();
      setBusy(false);
      input.focus();
    }
  }

  // Read Server-Sent Events off a POST. EventSource cannot POST, so the frames are
  // parsed by hand — SSE is line-oriented and a frame ends at a blank line, which is the
  // whole protocol. A chunk can split mid-frame, so the tail is carried over; forgetting
  // that yields JSON parse errors only under load, when frames start arriving coalesced.
  async function readStream(r, onEvent, body) {
    if (!r.body || !r.ok) {
      // No stream available — fall back to the plain endpoint so the turn still completes.
      const d = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
      }).then((x) => (x.ok ? x.json() : null)).catch(() => null);
      return d ? { ...d, __fallback: true } : null;
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
        if (ev.type === "done") result = ev.result;
        else if (ev.type === "error") result = { reply: "", error: ev.error };
        else onEvent(ev); // unknown types fall through onEvent untouched, on purpose
      }
    }
    return result;
  }

  // Three searches that run on one click. Each carries a whole brief and ends by telling
  // it to search, so a visitor sees results without typing; a bare input tells a
  // first-time visitor nothing about what this can do. Local and hourly through remote
  // and senior, because the range is the claim.
  const STARTERS = [
    ["Warehouse supervisor · Louisville · days",
      "I'm a warehouse supervisor in Louisville, KY. I've run a shift of about thirty people for six years, all on nights. I want a day-shift supervisor job in or near Louisville, no nights, and no less than $65,000 a year. That's everything, search now."],
    ["ERP implementation consultant · remote",
      "I implement ERP systems for manufacturers and distributors: requirements, configuration, data migration, go-live and training. Eight years of it. I want a remote ERP implementation consultant role in the US paying at least $110,000, and not through a staffing agency. That's everything, search now."],
    ["Forward-deployed AI engineer · remote · $200K+",
      "I'm a senior backend engineer, ten years in, the last three building LLM features into a logistics company's customer-facing software, and I spend a lot of my week on calls with the customers who use them. I want forward-deployed or AI implementation engineering work, where I sit with a customer's team and get the thing working in their systems. Remote only, anywhere in the US. $200,000 floor. No big consulting firms and no agencies. That's everything, search now."],
  ];
  if (startersEl) {
    STARTERS.forEach(([label, text]) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "starter";
      b.innerHTML = `${esc(label)} <span class="arw" aria-hidden="true">→</span>`;
      b.addEventListener("click", () => send(text));
      startersEl.appendChild(b);
    });
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

  say(OPENER);
})();
