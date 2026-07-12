"use strict";

// jobfitr front end — vanilla JS, no build step. Talks to the same-origin API
// (/api/score, /api/meta). All personal state (config, applied, dismissed) lives
// in localStorage; nothing is sent anywhere but the scoring request.

const API = "";
const KEY = { config: "jobfitr.config", applied: "jobfitr.applied", dismissed: "jobfitr.dismissed" };
const RING_C = 2 * Math.PI * 19; // ring circumference (r=19)
const TIER = (s) => (s >= 25 ? "strong" : s >= 15 ? "good" : "fair");
const LIST_FIELDS = ["titles", "boosts", "exclude", "rank_down"];

const $ = (sel, root = document) => root.querySelector(sel);
const el = {
  form: $("#search-form"),
  freshness: $("#freshness"),
  notice: $("#notice"),
  resultsSection: $("#results-section"),
  results: $("#results"),
  summary: $("#result-summary"),
  loading: $("#loading"),
  empty: $("#empty"),
  error: $("#error"),
  submit: $("#submit-btn"),
  share: $("#share-btn"),
  rail: $("#rail"),
  railToggle: $("#rail-toggle"),
  railBody: $("#rail-body"),
  railList: $("#rail-list"),
  railCount: $("#rail-count"),
  cardTpl: $("#card-template"),
};

// ── storage helpers ──────────────────────────────────────────────────────────
const store = {
  get(k, fallback) {
    try {
      const v = localStorage.getItem(k);
      return v ? JSON.parse(v) : fallback;
    } catch {
      return fallback;
    }
  },
  set(k, v) {
    try {
      localStorage.setItem(k, JSON.stringify(v));
    } catch {
      /* storage disabled / full — the app still works for this session */
    }
  },
};

// ── config <-> form <-> URL hash ─────────────────────────────────────────────
function splitList(s) {
  return (s || "")
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
}

function readForm() {
  const data = new FormData(el.form);
  const cfg = {};
  for (const f of LIST_FIELDS) cfg[f] = splitList(data.get(f));
  cfg.location = (data.get("location") || "").trim();
  const age = parseInt(data.get("max_age_days"), 10);
  if (Number.isFinite(age) && age > 0) cfg.max_age_days = age;
  cfg.min_score = data.get("min_score") || "balanced";
  return cfg;
}

function hydrateForm(cfg) {
  if (!cfg) return;
  for (const f of LIST_FIELDS) {
    const input = el.form.elements[f];
    if (input) input.value = Array.isArray(cfg[f]) ? cfg[f].join(", ") : cfg[f] || "";
  }
  if (el.form.elements.location) el.form.elements.location.value = cfg.location || "";
  if (cfg.max_age_days && el.form.elements.max_age_days) el.form.elements.max_age_days.value = cfg.max_age_days;
  const pick = document.querySelector(`input[name="min_score"][value="${cfg.min_score || "balanced"}"]`);
  if (pick) pick.checked = true;
}

function encodeHash(cfg) {
  try {
    return "#q=" + btoa(unescape(encodeURIComponent(JSON.stringify(cfg))));
  } catch {
    return "";
  }
}

function decodeHash() {
  const m = location.hash.match(/[#&]q=([^&]+)/);
  if (!m) return null;
  try {
    return JSON.parse(decodeURIComponent(escape(atob(m[1]))));
  } catch {
    return null;
  }
}

// ── freshness line ───────────────────────────────────────────────────────────
function relTime(iso) {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "";
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 2) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

async function loadFreshness() {
  try {
    const r = await fetch(API + "/api/meta");
    if (!r.ok) return;
    const m = await r.json();
    if (m && m.count) {
      const when = m.harvested_at ? ` · refreshed ${relTime(m.harvested_at)}` : "";
      el.freshness.textContent = `${m.count.toLocaleString()} jobs in the pool${when}`;
    }
  } catch {
    /* freshness is cosmetic — silent if the endpoint is unreachable */
  }
}

// ── summary "moment" ─────────────────────────────────────────────────────────
// The summary is the source of truth for the visible match count. writeSummary
// always lands on the final value (a token cancels any in-flight count-up, and a
// timeout backstops rAF if the tab is throttled), so it can never go stale — e.g.
// after Apply/Dismiss removes a card, we re-write it with the remaining count.
let _summaryToken = 0;
function summaryText(n) {
  return `${n} ${n === 1 ? "match" : "matches"}`;
}
function writeSummary(n, animate) {
  const token = ++_summaryToken;
  const final = summaryText(n);
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!animate || reduce || n <= 0) {
    el.summary.textContent = final;
    return;
  }
  el.summary.textContent = summaryText(0);
  const start = performance.now();
  const dur = Math.min(700, 200 + n * 12);
  function tick(now) {
    if (token !== _summaryToken) return; // a newer write superseded this one
    const p = Math.min(1, (now - start) / dur);
    const eased = 1 - Math.pow(1 - p, 3);
    el.summary.textContent = p < 1 ? summaryText(Math.round(eased * n)) : final;
    if (p < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  setTimeout(() => {
    if (token === _summaryToken) el.summary.textContent = final; // backstop
  }, dur + 100);
}

function visibleCount() {
  return el.results.querySelectorAll(".card").length;
}

// ── rendering ────────────────────────────────────────────────────────────────
function show(node, on) {
  node.hidden = !on;
}

function fillCard(job) {
  const node = el.cardTpl.content.firstElementChild.cloneNode(true);
  const tier = TIER(job.fit_score);
  node.dataset.tier = tier;
  node.dataset.url = job.url;

  $(".ring-score", node).textContent = job.fit_score;
  $(".tier", node).textContent = tier;
  const frac = Math.max(0.05, Math.min(1, job.fit_score / 40));
  const fill = $(".ring-fill", node);
  fill.style.strokeDashoffset = RING_C; // start empty, then sweep on next frame
  requestAnimationFrame(() => {
    fill.style.strokeDashoffset = RING_C * (1 - frac);
  });

  const link = $(".card-title a", node);
  link.textContent = job.title || "Untitled role";
  link.href = job.url;
  $(".org", node).textContent = job.company || "—";
  $(".loc", node).textContent = job.location || "—";
  $(".snippet", node).textContent = job.snippet || "";
  $(".posted", node).textContent = job.posted ? `Posted ${job.posted}` : "";
  $(".salary", node).textContent = job.salary || "";
  $(".source", node).textContent = job.source ? `via ${job.source}` : "";

  const chips = $(".chips", node);
  (job.why || "")
    .split(",")
    .map((w) => w.trim())
    .filter(Boolean)
    .slice(0, 5)
    .forEach((w) => {
      const li = document.createElement("li");
      li.textContent = w;
      chips.appendChild(li);
    });

  const apply = $(".btn-apply", node);
  apply.href = job.url;
  apply.addEventListener("click", () => applyJob(node, job)); // lets the new tab open, then flies
  $(".btn-dismiss", node).addEventListener("click", () => dismissJob(node, job));
  return node;
}

function renderResults(data) {
  const applied = store.get(KEY.applied, {});
  const dismissed = new Set(store.get(KEY.dismissed, []));
  const jobs = (data.jobs || []).filter((j) => j.url && !dismissed.has(j.url) && !applied[j.url]);

  el.results.textContent = "";
  show(el.loading, false);
  show(el.error, false);

  if (!jobs.length) {
    show(el.resultsSection, false);
    show(el.empty, true);
    maybeThinNotice(data);
    return;
  }
  show(el.empty, false);
  show(el.resultsSection, true);
  writeSummary(jobs.length, true);
  const frag = document.createDocumentFragment();
  jobs.forEach((j) => frag.appendChild(fillCard(j)));
  el.results.appendChild(frag);
  maybeThinNotice(data);
}

function maybeThinNotice(data) {
  // The free, no-key sources skew remote-tech; warn a non-tech searcher rather
  // than let them read a thin list as "no jobs exist."
  if ((data.count || 0) < 3) {
    el.notice.innerHTML =
      "Thin results? The free job sources skew remote-tech right now. A wider, " +
      'non-tech search needs a free <a href="https://developer.adzuna.com/" target="_blank" rel="noopener">Adzuna key</a> ' +
      "(coming soon).";
    show(el.notice, true);
  } else {
    show(el.notice, false);
  }
}

// ── applied rail ─────────────────────────────────────────────────────────────
function renderRail() {
  const applied = store.get(KEY.applied, {});
  const items = Object.values(applied).sort((a, b) => (b.appliedAt || 0) - (a.appliedAt || 0));
  el.railCount.textContent = items.length;
  show(el.rail, items.length > 0);
  el.railList.textContent = "";
  for (const it of items) {
    const li = document.createElement("li");
    li.className = "rail-item";
    const a = document.createElement("a");
    a.href = it.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.innerHTML = `${escapeHtml(it.title || "Role")}<span class="ro">${escapeHtml(it.company || "")}</span>`;
    const rm = document.createElement("button");
    rm.className = "rail-remove";
    rm.type = "button";
    rm.setAttribute("aria-label", `Remove ${it.title || "role"} from applied`);
    rm.textContent = "×";
    rm.addEventListener("click", () => {
      const cur = store.get(KEY.applied, {});
      delete cur[it.url];
      store.set(KEY.applied, cur);
      renderRail();
    });
    li.append(a, rm);
    el.railList.appendChild(li);
  }
}

function applyJob(cardNode, job) {
  const applied = store.get(KEY.applied, {});
  if (!applied[job.url]) {
    applied[job.url] = { title: job.title, company: job.company, url: job.url, appliedAt: Date.now() };
    store.set(KEY.applied, applied);
  }
  flyToRail(cardNode, () => {
    cardNode.remove();
    renderRail();
    pulseRail();
    afterCardRemoved();
  });
}

function dismissJob(cardNode, job) {
  const dismissed = new Set(store.get(KEY.dismissed, []));
  dismissed.add(job.url);
  store.set(KEY.dismissed, [...dismissed]);
  cardNode.classList.add("dismissing");
  const done = () => {
    cardNode.remove();
    afterCardRemoved();
  };
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) done();
  else cardNode.addEventListener("transitionend", done, { once: true });
}

// Keep the summary honest after a card leaves (applied or dismissed); if the list
// is now empty, surface the empty state instead of a stale "0 matches".
function afterCardRemoved() {
  const n = visibleCount();
  if (n === 0) {
    show(el.resultsSection, false);
    show(el.empty, true);
  } else {
    writeSummary(n, false);
  }
}

function flyToRail(cardNode, done) {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) {
    done();
    return;
  }
  const from = cardNode.getBoundingClientRect();
  const to = el.railToggle.getBoundingClientRect();
  cardNode.style.setProperty("--fly-x", `${to.left - from.left}px`);
  cardNode.style.setProperty("--fly-y", `${to.top - from.top}px`);
  cardNode.classList.add("flying");
  cardNode.addEventListener("animationend", done, { once: true });
}

function pulseRail() {
  el.railToggle.animate(
    [{ transform: "scale(1)" }, { transform: "scale(1.18)" }, { transform: "scale(1)" }],
    { duration: 320, easing: "ease-out" }
  );
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

// ── submit ───────────────────────────────────────────────────────────────────
async function runSearch(cfg) {
  show(el.empty, false);
  show(el.error, false);
  show(el.notice, false);
  show(el.resultsSection, false);
  show(el.loading, true);
  el.submit.disabled = true;
  try {
    const r = await fetch(API + "/api/score", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    renderResults(data);
    el.share.hidden = false;
  } catch {
    show(el.loading, false);
    show(el.resultsSection, false);
    show(el.error, true);
  } finally {
    el.submit.disabled = false;
  }
}

el.form.addEventListener("submit", (e) => {
  e.preventDefault();
  const cfg = readForm();
  store.set(KEY.config, cfg);
  history.replaceState(null, "", encodeHash(cfg));
  runSearch(cfg);
});

el.railToggle.addEventListener("click", () => {
  const open = el.rail.classList.toggle("open");
  el.railToggle.setAttribute("aria-expanded", String(open));
});

el.share.addEventListener("click", async () => {
  const url = location.origin + location.pathname + encodeHash(readForm());
  try {
    await navigator.clipboard.writeText(url);
    el.share.textContent = "Link copied ✓";
    setTimeout(() => (el.share.textContent = "Copy shareable link"), 1800);
  } catch {
    el.share.textContent = url; // clipboard blocked — show it to copy manually
  }
});

// ── boot ─────────────────────────────────────────────────────────────────────
(function init() {
  const fromHash = decodeHash();
  const cfg = fromHash || store.get(KEY.config, null);
  hydrateForm(cfg);
  renderRail();
  loadFreshness();
  if (cfg) runSearch(cfg); // a shared link or a returning user runs immediately
})();
