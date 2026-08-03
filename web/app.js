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
  criteria: $("#criteria"),
  proof: $("#proof"),
  starters: $("#starters"),
  doorextra: $("#doorextra"),
  chatSay: $("#chat-say"),
  carousel: $("#carousel"),
  fold: $("#fold"),
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
  // The best score in the current set — the bar's denominator, and ONLY the bar's. The
  // number on each card never touches it, which is the whole point of the score being
  // absolute. Recomputed per search, so a weak board's best still fills its own bar.
  topScore: 0,
  cfg: {},
  // facets: per-group Set of selected values (OR within a group, AND across groups)
  filters: { fit: 0, minSalary: 0, facets: {}, agency: false, seen: true },
  sort: "fit",
  view: [], // the current filtered/sorted list the board pages through
  rendered: 0, // how much of `view` is actually in the DOM (the rest is one scroll away)
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
    state.topScore = state.all.reduce((m, j) => Math.max(m, j.points || 0), 0);
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
  if (data.degraded === "live_search_limit") {
    msg =
      "🌅 We've hit today's live-search limit. jobfitr is a free tool running on free APIs — no paid services — so there's a daily ceiling to stay in budget. These are the freshest saved matches; check back a bit later and the live search opens back up.";
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
    if ((j.points || 0) < f.fit) return false;
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
    copy.sort((a, b) => (b.points || 0) - (a.points || 0));
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
  updateFold(); // after the show — a hidden list has no scrollHeight to compare
}

// Colour only — no words. The bands still exist because a glance wants a signal, but
// they are no longer LABELLED, and that is the point: the chips under the title already
// say where the points came from ("title +100 · rag ×3 +18 · staffing −30"), and a second
// interpretive layer in different vocabulary was worse than nothing. It described the
// title while the number described the total, so a `title +80` listing with two boosts
// reached 100 and announced "Exact title" beside a chip saying 80.
//
// The number is the score. The chips are the reasoning. Nothing else claims to explain it.
const FIT_BANDS = [
  { min: 100, cls: "strong" },
  { min: 80, cls: "good" },
  { min: 30, cls: "fair" },
  { min: -Infinity, cls: "fair" },
];

function bandFor(points) {
  return FIT_BANDS.find((b) => points >= b.min);
}

// Some sources (e.g. HN) stuff the whole posting into `location`. Keep just the real
// place: the first clause, capped — so the org line stays a clean "Company · Place".
function cleanLoc(loc, job) {
  const first = (loc || "").split(/[.;|\n]/)[0].trim();
  if (!first) return "—";
  // Adzuna appends the county — "Austin, Travis County", "San Diego, San Diego County"
  // (474 rows in the live pool). A county means nothing to a job seeker, and there is no
  // county→state table here, so drop it rather than show it or guess a state.
  const place = first.replace(/,\s*[^,]*\bcounty\s*$/i, "").trim() || first;
  // Some sources use `location` as a dumping ground: HN leads with a place and then
  // appends the entire posting ("REMOTE (US) Do you want to help scientists…"), and
  // himalayas lists 190 countries. 145 rows in the live pool. Truncating that to 60
  // chars still put a paragraph on the org line, so when the value reads as prose fall
  // back to the work-style tag we already derive — honest, and never a fabricated city.
  if (place.split(/\s+/).length > 8) {
    const tags = (job && job.tags) || [];
    return tags.includes("remote") ? "Remote" : tags.includes("onsite") ? "On-site" : "—";
  }
  return place.length > 60 ? place.slice(0, 57) + "…" : place;
}

// Sources emit a "range" even when both ends are identical — 637 of the 1,480 salaried
// rows in the live pool render as "$40,000–$40,000". Collapse those to one figure while
// leaving real ranges ("$190–250k", "$120 - $170 /hour") untouched. The separator from
// this source is an EN DASH, not a hyphen, so match both.
function cleanSalary(s) {
  const t = (s || "").trim();
  const m = t.match(/^(.+?)\s*[-–—]\s*(.+)$/);
  return m && m[1].trim() === m[2].trim() ? m[1].trim() : t;
}

// The board is a SCROLLABLE vertical carousel of small cards. Each is compact by
// default (title · place, salary · posted, fit + rank); click it to expand the full
// detail (bulleted description + Apply / Pass), click again to collapse. One open at a time.
// The server now delivers up to RESULT_CAP (200) rows rather than 50, because the
// filters run in here and 50 rows minus a salary floor minus a work style is not a
// board. Those rows are all in memory; the DOM gets them a page at a time, so first
// paint costs the same as it did at 50 and scrolling never hits a build-200-cards
// stall. No extra request — "load more" is a render, not a fetch.
const PAGE = 50;
function renderCarousel() {
  el.carousel.textContent = "";
  state.rendered = 0;
  if (!state.view.length) { updateFold(); return; }
  appendPage();
  el.carousel.scrollTop = 0; // a new result set starts at its own top, not the old offset
  // The fold is NOT updated here: this runs while the list is still hidden behind the
  // loading state, and a hidden element has no scrollHeight to compare — it would
  // conclude there is nothing below. applyFilters updates it once the list is shown.
}
function appendPage() {
  const list = state.view;
  const from = state.rendered;
  const to = Math.min(from + PAGE, list.length);
  if (to <= from) return false;
  const frag = document.createDocumentFragment();
  for (let i = from; i < to; i++) frag.appendChild(buildCard(list[i], i, list.length));
  el.carousel.appendChild(frag);
  state.rendered = to;
  return true;
}

// ── the fold ─────────────────────────────────────────────────────────────────
// The fade at the bottom of the list, on whenever there is anything below it and
// off the moment you reach the end — a permanent fade over the last card would be
// a hint that there is more when there is not. Three scroll-box properties, no DOM
// walk, so it is cheap enough to run on every scroll frame.
function updateFold() {
  if (!el.fold) return;
  const c = el.carousel;
  const bottomGap = c.scrollHeight - (c.scrollTop + c.clientHeight);
  // Grow the DOM BEFORE the user reaches the end, so the next page is already laid out
  // by the time it scrolls into view and the list never visibly stops.
  if (!c.hidden && bottomGap < c.clientHeight && state.rendered < state.view.length) {
    if (appendPage()) return updateFold(); // re-measure against the taller list
  }
  // 2px of slack for sub-pixel layout, which otherwise leaves the last card "0.4px
  // short" of the edge and the fade permanently lit on a list you have read to the end
  el.fold.classList.toggle("on", !c.hidden && bottomGap > 2);
}
let foldTick = false;
function scheduleFold() {
  if (foldTick) return;
  foldTick = true;
  requestAnimationFrame(() => { foldTick = false; updateFold(); });
}
el.carousel.addEventListener("scroll", scheduleFold, { passive: true });
window.addEventListener("resize", scheduleFold, { passive: true });

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

  // The fit gutter. The NUMBER is absolute: 100 is an exact title match whether it is
  // alone on the board or one of fifty, and it means the same thing tomorrow. It
  // replaced a gauge that min-max normalised each result set into a 45-100 band, so a
  // 4-point real spread and a 14-point real spread rendered identically.
  //
  // The BAR is deliberately relative — its width is a fraction of the best score in this
  // set, because a bar has to be a fraction of something and the score has no ceiling.
  // So the number answers "how good is this?" and the bar answers "compared to the rest
  // of this board?". Two different questions, which is why both are here.
  const points = job.points || 0;
  const t = bandFor(points);
  $(".fit-num", node).textContent = points;
  const bar = $(".fit-bar", node);
  bar.classList.add(t.cls);
  const width = state.topScore > 0 ? Math.max(3, Math.min(100, (points / state.topScore) * 100)) : 0;
  requestAnimationFrame(() => (bar.style.width = width + "%"));
  $(".fit-word", node).textContent = `#${index + 1} of ${total}`;

  $(".role", node).textContent = job.title || "Untitled role";
  $(".company", node).textContent = job.company || "—";
  $(".loc", node).textContent = cleanLoc(job.location, job);

  // always visible on the small card — everyone wants salary + posted at a glance
  $(".salary", node).textContent = cleanSalary(job.salary);
  $(".posted", node).textContent = job.posted ? `Posted ${job.posted}` : "";
  $(".source", node).textContent = job.source ? `via ${job.source}` : "";

  // The receipt, on the COLLAPSED row — the ranking has to explain itself at the moment
  // the user is judging it, not after a click. `parts` carries [label, delta] straight
  // from the scorer, so these chips ADD UP to the number beside them. That is a tested
  // guarantee, not a hope: the suite asserts sum(parts) == points over every pair in the
  // corpus, because a breakdown that does not reconcile is worse than none at all — it
  // invites the user to trust a wrong explanation.
  //
  // This replaced splitting a comma-joined `why` string, which had already thrown away
  // the numbers by the time the card saw it.
  const why = $(".why", node);
  (job.parts || []).slice(0, 5).forEach(([label, delta]) => {
    const li = document.createElement("li");
    li.className = delta < 0 ? "part neg" : "part";
    li.textContent = `${label} ${delta > 0 ? "+" : "−"}${Math.abs(delta)}`;
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
    // after the card has actually grown, not before
    requestAnimationFrame(() => revealCard(node));
  }
  // a card that grew or shrank moved every card under it — the count is stale until
  // the new heights are laid out
  requestAnimationFrame(updateFold);
}

// Keep an expanded card clear of the sticky header. scrollIntoView({block:"nearest"})
// reasons about the VIEWPORT and knows nothing about chrome pinned over the top of it,
// so a card that grew pushed its own title up underneath the header — you clicked a
// row and the thing you clicked disappeared.
function revealCard(node) {
  // The LIST scrolls, not the page — window.scrollBy moves nothing here, so a card
  // that grew past the bottom edge simply stayed cut off. Both bounds come from the
  // scroll container itself: its top edge is the ceiling (the boardbar sits above it
  // in flow, so nothing can hide under the header any more), and its bottom edge is
  // the floor, less the fold's fade when that is showing.
  const box = el.carousel.getBoundingClientRect();
  const gap = 10;
  const ceiling = box.top + gap;
  const floor = box.bottom - (el.fold && el.fold.classList.contains("on") ? 82 : gap);
  const r = node.getBoundingClientRect();
  let dy = 0;
  if (r.top < ceiling) {
    dy = r.top - ceiling; // tucked under the top edge — bring it down
  } else if (r.bottom > floor) {
    // running off the bottom: scroll down, but never so far that the title hides
    dy = Math.min(r.bottom - floor, r.top - ceiling);
  }
  if (dy) el.carousel.scrollBy({ top: dy, behavior: reduceMotion() ? "auto" : "smooth" });
}

// ── the criteria bar: THE HANDOFF ────────────────────────────────────────────
// Four answered questions are not worth 40% of the viewport while you read results,
// but they still have to be visible and editable — otherwise the board is a black box
// you can only escape by starting over. So the transcript collapses to this: one pill
// per answer, each removable, plus a Refine that reopens the conversation.
const CRIT_FIELDS = [
  { key: "titles", neg: false },
  // The model's own suggestions, not the user's answers, so they read differently —
  // muted, and labelled. A user who cannot tell which titles they chose from which the
  // machine added has no way to judge why a listing is on their board.
  { key: "related_titles", neg: false, suggested: true },
  { key: "location", neg: false },
  { key: "boosts", neg: false },
  { key: "exclude", neg: true },
  { key: "rank_down", neg: true },
];

function critText(key, value) {
  if (key === "location") return value;
  if (key === "related_titles") return `also: ${Array.isArray(value) ? value.join(" · ") : value}`;
  return Array.isArray(value) ? value.join(" · ") : value;
}

function renderCriteria() {
  if (!el.criteria) return;
  const cfg = state.cfg || {};
  el.criteria.textContent = "";

  const lab = document.createElement("span");
  lab.className = "crit-lab";
  lab.textContent = "searching";
  el.criteria.appendChild(lab);

  let any = false;
  for (const f of CRIT_FIELDS) {
    const raw = cfg[f.key];
    const value = Array.isArray(raw) ? raw.filter(Boolean) : (raw || "").trim();
    if (!value || (Array.isArray(value) && !value.length)) continue;
    any = true;

    const pill = document.createElement("span");
    pill.className = "crit" + (f.neg ? " neg" : "") + (f.suggested ? " suggested" : "");
    const txt = document.createElement("span");
    txt.className = "txt";
    txt.textContent = critText(f.key, value);
    pill.title = `${f.key.replace(/_/g, " ")}: ${txt.textContent}`;

    const x = document.createElement("button");
    x.type = "button";
    x.className = "x";
    x.setAttribute("aria-label", `Remove ${txt.textContent} from the search`);
    x.textContent = "✕";
    x.addEventListener("click", () => dropCriterion(f.key));

    pill.append(txt, x);
    el.criteria.appendChild(pill);
  }

  // Refine opens the conversation IN PLACE, right under the criteria it edits, rather
  // than throwing the user back to the front door or parking a bar at the bottom of
  // the page. The board keeps its height until you actually ask to talk.
  const refine = document.createElement("button");
  refine.type = "button";
  refine.className = "crit-refine";
  refine.textContent = "Refine ⌄";
  refine.setAttribute("aria-expanded", String(refineOpen()));
  refine.addEventListener("click", () => toggleRefine());
  el.criteria.appendChild(refine);

  show(el.criteria, any);
}

// ── the refine panel (the conversation, docked in the header) ────────────────
function boardbar() {
  return document.querySelector(".boardbar");
}
function refineOpen() {
  const b = boardbar();
  return !!b && b.classList.contains("refining");
}
function toggleRefine(force) {
  const b = boardbar();
  if (!b) return;
  const open = force === undefined ? !b.classList.contains("refining") : !!force;
  b.classList.toggle("refining", open);
  const btn = $(".crit-refine", b);
  if (btn) btn.setAttribute("aria-expanded", String(open));
  if (open) {
    const input = $("#chat-input");
    if (input) input.focus();
  }
}
// chat.js calls this when the assistant says something while the board is up, so a
// reply can never land in a panel the user has closed.
window.jobfitrShowRefine = () => toggleRefine(true);

// Dropping a criterion re-scores. That is a real request, so guard against a user
// clearing four pills in a row and firing four searches — coalesce to the last one.
let _critTimer = null;
function dropCriterion(key) {
  const cfg = { ...(state.cfg || {}) };
  if (key === "location") delete cfg.location;
  else cfg[key] = [];
  state.cfg = cfg;
  renderCriteria();
  clearTimeout(_critTimer);
  _critTimer = setTimeout(() => run(cfg), 450);
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
  // THE HANDOFF. The chat stops being the page and becomes the control strip FOR the
  // page: the transcript collapses into the criteria bar, and the input docks to the
  // bottom instead of floating over the results header it used to collide with.
  const bar = el.chatView.querySelector(".barwrap");
  const entering = !document.body.classList.contains("board");
  const first = entering && bar ? bar.getBoundingClientRect() : null;

  document.body.classList.add("board");
  show(el.formView, false);
  show(el.chatView, true);
  show(el.resultsView, true);
  show(el.filters, true); // the filter panel is only available on the board
  state.focusIndex = 0;

  // Move the conversation into the header group so refining happens next to the
  // criteria it changes. Reparenting (rather than a second input) keeps ONE chat
  // form, one message log, one set of handlers — two would drift apart immediately.
  const header = document.querySelector(".boardbar");
  const convo = document.querySelector(".convo");
  if (header && convo && convo.parentElement !== header) header.appendChild(convo);
  // On the board the box is no longer answering an interview question — it is the way
  // back into the conversation, so say that rather than "Type your answer…".
  const chatInput = $("#chat-input");
  if (chatInput) {
    chatInput.placeholder = "Ask jobfitr to change your search…";
    chatInput.setAttribute("aria-label", "Ask jobfitr to change your search");
  }

  // On the FIRST handoff the assistant's last line is "Pulling your matches…", which
  // is stale the moment the board arrives and used to sit there contradicting it.
  // Only on the first, though: later runs are refinements, and their reply ("Senior
  // roles only — re-scoring.") is the only confirmation the user gets that the thing
  // they asked for actually happened.
  if (entering && el.chatSay) el.chatSay.textContent = "";
  renderCriteria();

  // On a fresh search: FLIP the chat bar from its centered spot DOWN into the dock,
  // and flow the results up beneath the criteria bar.
  if (first && bar && !reduceMotion()) {
    const dy = first.top - bar.getBoundingClientRect().top;
    if (Math.abs(dy) > 4) {
      bar.style.transition = "none";
      bar.style.transform = `translateX(-50%) translateY(${dy}px)`;
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
  // hand the conversation back to the front-door column it came from
  const convo = document.querySelector(".convo");
  if (convo && convo.parentElement !== el.chatView) {
    el.chatView.insertBefore(convo, el.doorextra || null);
  }
  const chatInput = $("#chat-input");
  if (chatInput) {
    chatInput.placeholder = "Type your answer…";
    chatInput.setAttribute("aria-label", "Tell jobfitr what job you're chasing");
  }
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
  // Hang the panel off the ACTUAL bottom of the sticky header group. Its height is not
  // fixed — the criteria pills wrap to a second line on a narrow screen — so a CSS
  // constant here would either overlap the toolbar or float away from it.
  if (open) {
    const bar = document.querySelector(".boardbar");
    if (bar) el.filters.style.top = Math.round(bar.getBoundingClientRect().bottom + 6) + "px";
  }
});
// Point thresholds, shown as points. The stops are still the values the score clusters
// on, but they are named after the number rather than after a story about the number.
const FIT_FILTER = [
  { min: 0, label: "any" },
  { min: 30, label: "30+" },
  { min: 80, label: "80+" },
  { min: 100, label: "100+" },
];
el.fFit.addEventListener("input", () => {
  const stop = FIT_FILTER[+el.fFit.value] || FIT_FILTER[0];
  state.filters.fit = stop.min;
  el.fFitVal.textContent = stop.label;
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

window.jobfitr = {
  run,
  showChat,
  showResults,
  showForm,
  confirm: confirmConfig,
  // The board's config is the ONE truth once results are up. chat.js used to keep its
  // own copy, which was empty for anyone who arrived by shared link and went stale the
  // moment a criteria pill was removed — so a refinement was computed against a config
  // the user was no longer looking at.
  currentConfig: () => state.cfg || {},
};

// ── the front door: proof + starters ─────────────────────────────────────────
// The proof line uses ONLY what /api/meta really returns (pool size + harvest date).
// No invented "N company boards" — an unverifiable number on the honesty pitch would
// undercut the whole product.
function fmtCount(n) {
  return Number(n).toLocaleString("en-US");
}
async function loadProof() {
  if (!el.proof) return;
  try {
    const r = await fetch("/api/meta");
    if (!r.ok) return;
    const m = await r.json();
    if (!m.count) return;
    const today = new Date().toISOString().slice(0, 10);
    const fresh = m.harvested_at === today ? "today" : m.harvested_at || "recently";
    el.proof.innerHTML =
      `<span class="dot"></span><b>${fmtCount(m.count)}</b> live roles` +
      `<span class="sep">·</span>refreshed <b>${escapeHtml(fresh)}</b>` +
      `<span class="sep">·</span>no account, no tracking`;
  } catch {
    /* the proof line is a nicety — a cold front door still works without it */
  }
}

// Real searches a visitor can run in one click. A blank first screen with a bare
// input tells them nothing about what the assistant can actually do.
const STARTERS = [
  { label: "Remote AI engineering", text: "Remote AI engineering roles" },
  { label: "Data roles, $150k+", text: "Data engineering roles paying $150k or more" },
  { label: "Posted this week, no agencies", text: "Roles posted this week, no staffing agencies" },
];
function renderStarters() {
  if (!el.starters) return;
  for (const s of STARTERS) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "starter";
    b.innerHTML = `${escapeHtml(s.label)} <span class="arw" aria-hidden="true">→</span>`;
    // chat.js registers `seed` (it owns the conversation); without the AI the
    // starter is inert rather than broken, and the form fallback still works.
    b.addEventListener("click", () => window.jobfitr.seed && window.jobfitr.seed(s.text));
    el.starters.appendChild(b);
  }
}

// ── boot ─────────────────────────────────────────────────────────────────────
(function init() {
  renderRail();
  loadProof();
  renderStarters();
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
