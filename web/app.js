"use strict";

// jobfitr front end — vanilla, no build step. The chat (chat.js) or the fallback
// form both produce a config, hand it to run(), which scores the cached snapshot
// via /api/score and renders the gauge-card carousel. All personal state (config,
// applied, dismissed) lives in localStorage; the only request is the scoring POST.

const KEY = { config: "jobfitr.config", applied: "jobfitr.applied", dismissed: "jobfitr.dismissed" };
const LIST_FIELDS = ["titles", "boosts", "exclude", "rank_down"];
const AGENCY_RE = /staffing|agency|recruit|talent solutions/i;
const REMOTE_RE = /remote|anywhere|work from home|wfh/i;

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
  fTags: $("#f-tags"),
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
  filters: { fit: 0, remote: "any", tags: new Set(), agency: false, seen: true },
  sort: "fit",
};

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
    maybeThinNotice(data);
    buildTagFilter();
    applyFilters(true);
  } catch {
    show(el.loading, false);
    show(el.error, true);
  }
}

function maybeThinNotice(data) {
  if ((data.count || 0) < 3) {
    el.notice.innerHTML =
      "Thin results? The free job sources skew remote-tech right now. A wider search improves once the keyed sources are on.";
    show(el.notice, true);
  } else {
    show(el.notice, false);
  }
}

// ── filtering + carousel render ──────────────────────────────────────────────
function isRemote(job) {
  return REMOTE_RE.test(job.location || "");
}
function seenSet() {
  const applied = store.get(KEY.applied, {});
  const dismissed = new Set(store.get(KEY.dismissed, []));
  return { applied, dismissed };
}
function filteredJobs() {
  const { applied, dismissed } = seenSet();
  const f = state.filters;
  let list = state.all.filter((j) => {
    if (!j.url) return false;
    if (f.seen && (applied[j.url] || dismissed.has(j.url))) return false;
    if ((j.fit_pct || 0) < f.fit) return false;
    if (f.remote === "remote" && !isRemote(j)) return false;
    if (f.remote === "onsite" && isRemote(j)) return false;
    if (f.agency && AGENCY_RE.test(`${j.title} ${j.company}`)) return false;
    if (f.tags.size) {
      const sig = (j.why || "").toLowerCase();
      for (const t of f.tags) if (!sig.includes(t)) return false;
    }
    return true;
  });
  if (f.remote !== "onsite") {
    // sort
  }
  list = sortJobs(list);
  return list;
}
function sortJobs(list) {
  const s = state.sort;
  const copy = list.slice();
  if (s === "new") {
    copy.sort((a, b) => new Date(b.posted || 0) - new Date(a.posted || 0));
  } else if (s === "salary") {
    const num = (x) => parseInt(String(x.salary || "").replace(/[^0-9]/g, ""), 10) || 0;
    copy.sort((a, b) => num(b) - num(a));
  } else {
    copy.sort((a, b) => (b.fit_score || 0) - (a.fit_score || 0));
  }
  return copy;
}

function applyFilters(animateCount) {
  const list = filteredJobs();
  renderCarousel(list);
  writeSummary(list.length, !!animateCount);
  updateFilterCount(list.length);
  if (!list.length) {
    show(el.empty, true);
    show(el.carousel, false);
  } else {
    show(el.empty, false);
    show(el.carousel, true);
  }
}

function tierFor(pct) {
  if (pct >= 80) return { word: "Strong fit", cls: "strong" };
  if (pct >= 55) return { word: "Good fit", cls: "good" };
  return { word: "Fair fit", cls: "fair" };
}

function renderCarousel(list) {
  el.carousel.textContent = "";
  const frag = document.createDocumentFragment();
  list.forEach((job, i) => frag.appendChild(buildCard(job, i, list.length)));
  el.carousel.appendChild(frag);
}

function buildCard(job, index, total) {
  const node = el.cardTpl.content.firstElementChild.cloneNode(true);
  node.dataset.url = job.url;
  const focus = index === 0;
  node.classList.add(focus ? "is-focus" : index <= 2 ? "is-near" : "is-far");
  if (!focus) node.classList.add("is-masked");

  const pct = Math.max(0, Math.min(100, job.fit_pct || 0));
  const fill = $(".fill", node);
  requestAnimationFrame(() => (fill.style.width = pct + "%"));

  $(".role", node).textContent = job.title || "Untitled role";
  $(".company", node).textContent = job.company || "—";
  $(".loc", node).textContent = job.location || "—";
  const t = tierFor(pct);
  const tierEl = $(".tier", node);
  tierEl.textContent = t.word;
  tierEl.classList.add(t.cls);
  $(".rank", node).textContent = `#${index + 1} of ${total}`;

  const why = $(".why", node);
  (job.why || "").split(",").map((w) => w.trim()).filter(Boolean).slice(0, 5).forEach((w) => {
    const li = document.createElement("li");
    li.textContent = w;
    why.appendChild(li);
  });

  $(".desc", node).textContent = job.description || job.snippet || "";
  $(".salary", node).textContent = job.salary || "";
  $(".posted", node).textContent = job.posted ? `Posted ${job.posted}` : "";
  $(".source", node).textContent = job.source ? `via ${job.source}` : "";

  const head = $(".gcard-head", node);
  const detail = $(".detail", node);
  head.addEventListener("click", () => {
    if (!node.classList.contains("is-focus")) return; // only the focused card expands
    const open = detail.hidden;
    detail.hidden = !open;
    head.setAttribute("aria-expanded", String(open));
  });

  const open = $(".btn-open", node);
  open.href = job.url;
  const applyActs = $(".acts-applied", node);
  const viewActs = $(".acts:not(.acts-applied)", node);
  open.addEventListener("click", () => {
    // opening the posting is a VIEW, not an application — reveal the two-step
    viewActs.hidden = true;
    applyActs.hidden = false;
  });
  $(".btn-dismiss", node).addEventListener("click", () => dismissJob(node, job));
  $(".btn-notthis", node).addEventListener("click", () => dismissJob(node, job));
  $(".btn-applied", node).addEventListener("click", () => applyJob(node, job));
  return node;
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

// ── the filter drawer ────────────────────────────────────────────────────────
function buildTagFilter() {
  const counts = new Map();
  for (const j of state.all) {
    for (const w of (j.why || "").split(",").map((x) => x.trim().toLowerCase()).filter(Boolean)) {
      counts.set(w, (counts.get(w) || 0) + 1);
    }
  }
  const top = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).map((e) => e[0]);
  el.fTags.textContent = "";
  state.filters.tags.clear();
  for (const tag of top) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = tag;
    b.addEventListener("click", () => {
      b.classList.toggle("sel");
      if (b.classList.contains("sel")) state.filters.tags.add(tag);
      else state.filters.tags.delete(tag);
      applyFilters(false);
    });
    el.fTags.appendChild(b);
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
  show(el.chatView, false);
  show(el.formView, false);
  show(el.resultsView, true);
}
function showChat() {
  show(el.resultsView, false);
  show(el.formView, false);
  show(el.chatView, true);
}
function showForm() {
  show(el.chatView, false);
  show(el.resultsView, false);
  show(el.formView, true);
}

// ── wiring ────────────────────────────────────────────────────────────────────
el.form.addEventListener("submit", (e) => {
  e.preventDefault();
  run(readForm());
});
el.toForm.addEventListener("click", () => {
  hydrateForm(state.cfg || store.get(KEY.config, null));
  showForm();
});
el.toChat.addEventListener("click", showChat);
el.refine.addEventListener("click", showChat);
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
document.querySelectorAll(".toggle3 button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".toggle3 button").forEach((b) => b.classList.remove("sel"));
    btn.classList.add("sel");
    state.filters.remote = btn.dataset.remote;
    applyFilters(false);
  });
});
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
window.jobfitr = { run, showChat, showResults, showForm };

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
