"use strict";

// jobfitr front end — vanilla, no build step. The chat (chat.js) or the fallback
// form both produce a config, hand it to run(), which scores the cached snapshot
// via /api/score and renders the gauge-card carousel. All personal state (config,
// applied, dismissed) lives in localStorage; the only request is the scoring POST.

const KEY = { config: "jobfitr.config", applied: "jobfitr.applied", dismissed: "jobfitr.dismissed" };
const LIST_FIELDS = ["titles", "boosts", "exclude", "rank_down"];
const AGENCY_RE = /staffing|agency|recruit|talent solutions/i;

// The facet drawer groups, in display order. `own` = the value lives in its own card
// field (job.category / job.employment_type); the rest are in job.tags (the derived
// remote / seniority / salary_band tags). `label` humanizes a raw facet value.
const FACET_GROUPS = [
  { key: "category", title: "Field", own: true, label: (v) => v.replace(/ Jobs$/, "") },
  { key: "remote", title: "Work style", own: false, label: (v) => (v === "onsite" ? "On-site" : "Remote") },
  { key: "employment_type", title: "Type", own: true, label: (v) => v.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()) },
  { key: "seniority", title: "Level", own: false, label: (v) => v.charAt(0).toUpperCase() + v.slice(1) },
  { key: "salary_band", title: "Salary", own: false, label: labelBand },
];
const REMOTE_TAGS = new Set(["remote", "onsite"]);
const SENIORITY_TAGS = new Set(["junior", "mid", "senior", "lead"]);

function labelBand(v) {
  return (
    { "under-50k": "< $50k", "50-80k": "$50–80k", "80-120k": "$80–120k", "120-180k": "$120–180k", "180k-plus": "$180k+" }[v] || v
  );
}
// Which facet group a derived job.tags value belongs to.
function tagGroup(tag) {
  if (REMOTE_TAGS.has(tag)) return "remote";
  if (SENIORITY_TAGS.has(tag)) return "seniority";
  return "salary_band";
}

const $ = (s, r = document) => r.querySelector(s);
const el = {
  chatView: $("#chat-view"),
  resultsView: $("#results-view"),
  formView: $("#form-view"),
  notice: $("#notice"),
  carousel: $("#carousel"),
  summary: $("#result-summary"),
  loading: $("#loading"),
  empty: $("#empty"),
  error: $("#error"),
  form: $("#search-form"),
  toForm: $("#to-form"),
  toChat: $("#to-chat"),
  refine: $("#refine"),
  sort: $("#sort"),
  filtersToggle: $("#filters-toggle"),
  filters: $("#filters"),
  fFit: $("#f-fit"),
  fFitVal: $("#f-fit-val"),
  fSalary: $("#f-salary"),
  fSalaryVal: $("#f-salary-val"),
  fFacets: $("#f-facets"),
  fAgency: $("#f-agency"),
  fSeen: $("#f-seen"),
  fCount: $("#f-count"),
  rail: $("#rail"),
  railToggle: $("#rail-toggle"),
  railList: $("#rail-list"),
  railCount: $("#rail-count"),
  railShare: $("#rail-share"),
  cardTpl: $("#card-template"),
};

const reduceMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// ── storage ───────────────────────────────────────────────────────────────────
const store = {
  get(k, fb) {
    try {
      const v = localStorage.getItem(k);
      return v ? JSON.parse(v) : fb;
    } catch {
      return fb;
    }
  },
  set(k, v) {
    try {
      localStorage.setItem(k, JSON.stringify(v));
    } catch {
      /* storage disabled — the app still works this session */
    }
  },
};

// ── in-memory result state ──────────────────────────────────────────────────
const state = {
  all: [], // every scored job from the last /api/score
  cfg: {},
  // facets: per-group Set of selected values (OR within a group, AND across groups)
  filters: { fit: 0, minSalary: 0, facets: {}, agency: false, seen: true },
  sort: "fit",
  view: [], // the current filtered/sorted list the board pages through
  focusIndex: 0, // which job is the big primary card
};
function selectedFacets(key) {
  return state.filters.facets[key] || (state.filters.facets[key] = new Set());
}

// ── config <-> form <-> URL hash ───────────────────────────────────────────
function splitList(s) {
  return (s || "").split(",").map((x) => x.trim()).filter(Boolean);
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

// ── the count-up summary "moment" ───────────────────────────────────────────
let _summaryToken = 0;
function summaryText(n) {
  return `${n} ${n === 1 ? "match" : "matches"}`;
}
function writeSummary(n, animate) {
  const token = ++_summaryToken;
  const final = summaryText(n);
  if (!animate || reduceMotion() || n <= 0) {
    el.summary.textContent = final;
    return;
  }
  el.summary.textContent = summaryText(0);
  const start = performance.now();
  const dur = Math.min(700, 200 + n * 12);
  function tick(now) {
    if (token !== _summaryToken) return;
    const p = Math.min(1, (now - start) / dur);
    const eased = 1 - Math.pow(1 - p, 3);
    el.summary.textContent = p < 1 ? summaryText(Math.round(eased * n)) : final;
    if (p < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

// ── scoring ─────────────────────────────────────────────────────────────────
async function run(cfg) {
  state.cfg = cfg;
  store.set(KEY.config, cfg);
  hydrateForm(cfg);
  // Deliberately do NOT write the config into the URL — a plain reload should
  // return to a fresh chat, not silently re-run the last search. (A pasted
  // #q= share link is still honored on load; we just don't mint one here.)
  showResults();
  show(el.error, false);
  show(el.notice, false);
  show(el.carousel, false);
  show(el.loading, true);
  try {
    const r = await fetch("/api/score", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    state.all = data.jobs || [];
    show(el.loading, false);
    show(el.carousel, true);
    renderNotice(data);
    buildFacets();
    applyFilters(true);
  } catch {
    show(el.loading, false);
    show(el.error, true);
  }
}

// ── the degradation / thin-results banner (warm, honest, never alarming) ──────
function renderNotice(data) {
  let msg = "";
  if (data.degraded === "adzuna_daily_limit") {
    msg =
      "🌅 We've hit today's live-search budget — jobfitr is a free tool with a daily API allowance. These are the freshest saved matches. Fresh pulls refill tomorrow.";
  } else if (data.degraded === "fetch_error") {
    msg =
      "A job source hiccuped just now, so these are the most recent saved matches. Try again in a moment for a fresh pull.";
  } else if ((data.pool || 0) > 0 && (data.pool || 0) < 200 && (data.count || 0) < 3) {
    msg =
      "The board is still filling in for this search — check back soon and it'll be richer.";
  } else if ((data.count || 0) < 3) {
    msg =
      "Thin results — try a broader title or drop a filter, and jobfitr will pull wider next time.";
  }
  el.notice.textContent = msg;
  show(el.notice, !!msg);
}

// ── filtering + carousel render ──────────────────────────────────────────────
function seenSet() {
  const applied = store.get(KEY.applied, {});
  const dismissed = new Set(store.get(KEY.dismissed, []));
  return { applied, dismissed };
}
// Does a job hold the given facet value? Own-field groups read the card field;
// the rest read the derived job.tags array (remote / seniority / salary_band).
function jobHasFacet(job, group, value) {
  return group.own ? job[group.key] === value : (job.tags || []).includes(value);
}
// A job passes a group if it matches ANY selected value in that group (OR-within).
// It must pass EVERY group that has a selection (AND-across).
function passesFacets(job) {
  for (const g of FACET_GROUPS) {
    const sel = state.filters.facets[g.key];
    if (sel && sel.size) {
      let hit = false;
      for (const v of sel) if (jobHasFacet(job, g, v)) { hit = true; break; }
      if (!hit) return false;
    }
  }
  return true;
}
// The min salary a posting states (its FIRST number, however small), or null when the
// salary field is empty. A tiny "$10" is a real (low/hourly) value — NOT "unlisted" —
// so it must be caught by the slider, not slip through as a no-salary job.
function salaryMin(job) {
  const m = String(job.salary || "").match(/\d[\d,]*/);
  if (!m) return null;
  const n = parseInt(m[0].replace(/,/g, ""), 10);
  return Number.isFinite(n) && n > 0 ? n : null;
}
function filteredJobs() {
  const { applied, dismissed } = seenSet();
  const f = state.filters;
  let list = state.all.filter((j) => {
    if (!j.url) return false;
    if (f.seen && (applied[j.url] || dismissed.has(j.url))) return false;
    if ((j.fit_pct || 0) < f.fit) return false;
    if (f.agency && AGENCY_RE.test(`${j.title} ${j.company}`)) return false;
    // salary slider: hide only postings whose STATED salary is below the floor;
    // keep the no-salary ones (coverage is sparse — don't silently drop them).
    if (f.minSalary > 0) {
      const s = salaryMin(j);
      if (s !== null && s < f.minSalary) return false;
    }
    if (!passesFacets(j)) return false;
    return true;
  });
  list = sortJobs(list);
  return list;
}
function sortJobs(list) {
  const s = state.sort;
  const copy = list.slice();
  if (s === "new") {
    copy.sort((a, b) => new Date(b.posted || 0) - new Date(a.posted || 0));
  } else if (s === "salary") {
    copy.sort((a, b) => (salaryMin(b) || 0) - (salaryMin(a) || 0));
  } else {
    copy.sort((a, b) => (b.fit_score || 0) - (a.fit_score || 0));
  }
  return copy;
}

function applyFilters(animateCount) {
  const list = filteredJobs();
  state.view = list;
  if (state.focusIndex > list.length - 1) state.focusIndex = Math.max(0, list.length - 1);
  renderCarousel();
  writeSummary(list.length, !!animateCount);
  updateFilterCount(list.length);
  show(el.empty, !list.length);
  show(el.carousel, !!list.length);
}

function tierFor(pct) {
  if (pct >= 80) return { word: "Strong fit", cls: "strong" };
  if (pct >= 55) return { word: "Good fit", cls: "good" };
  return { word: "Fair fit", cls: "fair" };
}

// Some sources (e.g. HN) stuff the whole posting into `location`. Keep just the real
// place: the first clause, capped — so the org line stays a clean "Company · Place".
function cleanLoc(loc) {
  const first = (loc || "").split(/[.;|]/)[0].trim();
  if (!first) return "—";
  return first.length > 60 ? first.slice(0, 57) + "…" : first;
}

// The board is a SCROLLABLE vertical carousel of small cards. Each is compact by
// default (title · place, salary · posted, fit + rank); click it to expand the full
// detail (bulleted description + Apply / Pass), click again to collapse. One open at a time.
function renderCarousel() {
  const list = state.view;
  el.carousel.textContent = "";
  if (!list.length) return;
  const frag = document.createDocumentFragment();
  list.forEach((job, i) => frag.appendChild(buildCard(job, i, list.length)));
  el.carousel.appendChild(frag);
}

// Split a JD blob into a few readable bullet points — meaty sentences only, skipping
// the title echoed back and ID-number noise the harvest sometimes leaves in.
function descBullets(text, title) {
  const t = (title || "").toLowerCase().trim();
  return (text || "")
    .split(/(?<=[.!?])\s+/)
    .map((s) => s.trim().replace(/\s+/g, " "))
    .filter((s) => {
      if (s.length < 24) return false;
      const low = s.toLowerCase();
      if (low === t || low.startsWith(t + ".") || low.startsWith(t + " ")) return false;
      const digits = (s.match(/\d/g) || []).length;
      if (digits > s.length * 0.25) return false; // mostly numbers/ids
      return true;
    })
    .slice(0, 20);
}

function buildCard(job, index, total) {
  const node = el.cardTpl.content.firstElementChild.cloneNode(true);
  node.dataset.url = job.url;

  const pct = Math.max(0, Math.min(100, job.fit_pct || 0));
  requestAnimationFrame(() => ($(".fill", node).style.width = pct + "%"));

  $(".role", node).textContent = job.title || "Untitled role";
  $(".company", node).textContent = job.company || "—";
  $(".loc", node).textContent = cleanLoc(job.location);
  const t = tierFor(pct);
  const tierEl = $(".tier", node);
  tierEl.textContent = t.word;
  tierEl.classList.add(t.cls);
  $(".rank", node).textContent = `#${index + 1} of ${total}`;

  // always visible on the small card — everyone wants salary + posted at a glance
  $(".salary", node).textContent = job.salary || "";
  $(".posted", node).textContent = job.posted ? `Posted ${job.posted}` : "";
  $(".source", node).textContent = job.source ? `via ${job.source}` : "";

  // shown when the card is expanded: the why chips + the bulleted description
  const why = $(".why", node);
  (job.why || "").split(",").map((w) => w.trim()).filter(Boolean).slice(0, 5).forEach((w) => {
    const li = document.createElement("li");
    li.textContent = w;
    why.appendChild(li);
  });
  const db = $(".desc-bullets", node);
  descBullets(job.description || job.snippet, job.title).forEach((b) => {
    const li = document.createElement("li");
    li.textContent = b;
    db.appendChild(li);
  });

  const open = $(".btn-open", node);
  open.href = job.url;
  open.addEventListener("click", (e) => { e.stopPropagation(); applyJob(node, job); });
  $(".btn-dismiss", node).addEventListener("click", (e) => { e.stopPropagation(); dismissJob(node, job); });

  // click the card to expand/collapse (buttons stopPropagation so they don't toggle)
  node.addEventListener("click", () => toggleExpand(node));
  return node;
}

// One card expanded at a time. Expanding scrolls it comfortably into view.
function toggleExpand(node) {
  const wasOpen = node.classList.contains("expanded");
  el.carousel.querySelectorAll(".gcard.expanded").forEach((n) => n.classList.remove("expanded"));
  if (!wasOpen) {
    node.classList.add("expanded");
    node.scrollIntoView({ behavior: reduceMotion() ? "auto" : "smooth", block: "nearest" });
  }
}

// ── two-step apply / dismiss ────────────────────────────────────────────────
function applyJob(cardNode, job) {
  const applied = store.get(KEY.applied, {});
  if (!applied[job.url]) {
    applied[job.url] = { title: job.title, company: job.company, url: job.url, appliedAt: Date.now() };
    store.set(KEY.applied, applied);
  }
  flyToRail(cardNode, () => {
    renderRail();
    pulseRail();
    applyFilters(false); // re-render: the next card promotes to focus
  });
}
function dismissJob(cardNode, job) {
  const dismissed = new Set(store.get(KEY.dismissed, []));
  dismissed.add(job.url);
  store.set(KEY.dismissed, [...dismissed]);
  if (reduceMotion()) {
    applyFilters(false);
    return;
  }
  cardNode.classList.add("dismissing");
  cardNode.addEventListener("transitionend", () => applyFilters(false), { once: true });
}
function flyToRail(cardNode, done) {
  if (reduceMotion()) {
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

// ── the applied rail ─────────────────────────────────────────────────────────
function renderRail() {
  const applied = store.get(KEY.applied, {});
  const items = Object.values(applied).sort((a, b) => (b.appliedAt || 0) - (a.appliedAt || 0));
  el.railCount.textContent = items.length;
  show(el.rail, items.length > 0);
  show(el.railShare, items.length > 0);
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
function pulseRail() {
  if (reduceMotion()) return;
  el.railToggle.animate([{ transform: "scale(1)" }, { transform: "scale(1.18)" }, { transform: "scale(1)" }], { duration: 320, easing: "ease-out" });
}
function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

// ── the filter drawer: tag-driven facets with live counts ────────────────────
function facetCounts(key, own) {
  // count each value across the whole scored set (own field, or the tags array)
  const counts = new Map();
  for (const j of state.all) {
    const vals = own ? [j[key]] : j.tags || [];
    for (const v of vals) if (v) counts.set(v, (counts.get(v) || 0) + 1);
  }
  return counts;
}
function buildFacets() {
  el.fFacets.textContent = "";
  state.filters.facets = {};
  for (const g of FACET_GROUPS) {
    let counts = facetCounts(g.key, g.own);
    if (!g.own) {
      // the tags array mixes all three derived groups — keep only this group's values
      counts = new Map([...counts].filter(([v]) => tagGroup(v) === g.key));
    }
    if (!counts.size) continue;
    const values = [...counts.entries()].sort((a, b) => b[1] - a[1]);
    const sel = selectedFacets(g.key);

    const group = document.createElement("div");
    group.className = "fgroup";
    const head = document.createElement("span");
    head.className = "flab";
    head.textContent = g.title;
    group.appendChild(head);
    const pick = document.createElement("div");
    pick.className = "tagpick";
    for (const [value, count] of values) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "facet-chip";
      b.innerHTML = `${escapeHtml(g.label(value))}<span class="fc">${count}</span>`;
      b.addEventListener("click", () => {
        b.classList.toggle("sel");
        if (b.classList.contains("sel")) sel.add(value);
        else sel.delete(value);
        applyFilters(false);
      });
      pick.appendChild(b);
    }
    group.appendChild(pick);
    el.fFacets.appendChild(group);
  }
}
function updateFilterCount(n) {
  el.fCount.innerHTML = `Showing <b>${n}</b> of ${state.all.length}`;
}

// ── views ────────────────────────────────────────────────────────────────────
function show(node, on) {
  if (node) node.hidden = !on;
}
function showResults() {
  // board mode: the AI stays on screen (chat + echo up top), the job carousel below.
  const bar = el.chatView.querySelector(".barwrap");
  const entering = !document.body.classList.contains("board");
  const first = entering && bar ? bar.getBoundingClientRect() : null;

  document.body.classList.add("board");
  show(el.formView, false);
  show(el.chatView, true);
  show(el.resultsView, true);
  show(el.filters, true); // the left filter drawer is only available on the board
  state.focusIndex = 0;

  // On a fresh search: FLIP the chat bar from its centered spot UP into its board
  // position, and flow the results up beneath it.
  if (first && bar && !reduceMotion()) {
    const dy = first.top - bar.getBoundingClientRect().top;
    if (Math.abs(dy) > 4) {
      bar.style.transition = "none";
      bar.style.transform = `translateY(${dy}px)`;
      requestAnimationFrame(() => {
        bar.style.transition = "transform 620ms cubic-bezier(0.22, 1, 0.36, 1)";
        bar.style.transform = "";
      });
      bar.addEventListener("transitionend", () => { bar.style.transition = ""; bar.style.transform = ""; }, { once: true });
    }
    el.resultsView.classList.remove("flow-up");
    void el.resultsView.offsetWidth; // restart the animation
    el.resultsView.classList.add("flow-up");
  }
}
function showChat() {
  document.body.classList.remove("board");
  show(el.resultsView, false);
  show(el.formView, false);
  show(el.filters, false);
  el.filters.classList.remove("open");
  show(el.chatView, true);
}
function showForm() {
  document.body.classList.remove("board");
  show(el.chatView, false);
  show(el.resultsView, false);
  show(el.filters, false);
  show(el.formView, true);
}

// ── wiring ────────────────────────────────────────────────────────────────────
el.form.addEventListener("submit", (e) => {
  e.preventDefault();
  run(readForm());
});
// The manual "quick form instead" link was removed for the minimal front door;
// the form is now only reached as the automatic no-AI fallback (showForm()).
if (el.toForm) {
  el.toForm.addEventListener("click", () => {
    hydrateForm(state.cfg || store.get(KEY.config, null));
    showForm();
  });
}
el.toChat.addEventListener("click", showChat);
if (el.refine) el.refine.addEventListener("click", showChat); // Refine button removed from the toolbar
el.sort.addEventListener("change", () => {
  state.sort = el.sort.value;
  applyFilters(false);
});
el.filtersToggle.addEventListener("click", () => {
  const open = el.filters.classList.toggle("open");
  el.filtersToggle.setAttribute("aria-expanded", String(open));
});
el.fFit.addEventListener("input", () => {
  state.filters.fit = +el.fFit.value;
  el.fFitVal.textContent = state.filters.fit === 0 ? "any" : `${state.filters.fit}%+`;
  applyFilters(false);
});
if (el.fSalary) {
  el.fSalary.addEventListener("input", () => {
    state.filters.minSalary = +el.fSalary.value;
    el.fSalaryVal.textContent =
      state.filters.minSalary === 0 ? "$ any" : `$${Math.round(state.filters.minSalary / 1000)}k+`;
    applyFilters(false);
  });
}
el.fAgency.addEventListener("change", () => {
  state.filters.agency = el.fAgency.checked;
  applyFilters(false);
});
el.fSeen.checked = true;
el.fSeen.addEventListener("change", () => {
  state.filters.seen = el.fSeen.checked;
  applyFilters(false);
});
el.railToggle.addEventListener("click", () => {
  const open = el.rail.classList.toggle("open");
  el.railToggle.setAttribute("aria-expanded", String(open));
});
el.railShare.addEventListener("click", async () => {
  const applied = store.get(KEY.applied, {});
  const n = Object.keys(applied).length;
  const text = `I used jobfitr and applied to ${n} ${n === 1 ? "role" : "roles"} today. ${location.origin}`;
  try {
    if (navigator.share) await navigator.share({ text });
    else {
      await navigator.clipboard.writeText(text);
      el.railShare.textContent = "Copied ✓";
      setTimeout(() => (el.railShare.textContent = "Share these"), 1600);
    }
  } catch {
    /* user dismissed the share sheet */
  }
});

// expose the surface chat.js drives
// Confirmation-on-ready: the chat calls this once it has enough. Pre-fill the form
// with the extracted config so the user can review/edit before the search runs (the
// form's own submit calls run()). Reuses the fallback form as the confirmation surface.
function confirmConfig(cfg) {
  hydrateForm(cfg);
  showForm();
}

window.jobfitr = { run, showChat, showResults, showForm, confirm: confirmConfig };

// ── boot ─────────────────────────────────────────────────────────────────────
(function init() {
  renderRail();
  // A plain reload starts fresh at the chat front door. Only a shared link
  // (a #q= hash someone was sent) runs a search straight away.
  const fromHash = decodeHash();
  if (fromHash) {
    hydrateForm(fromHash);
    run(fromHash);
  } else {
    showChat();
  }
})();
