// Test the REAL app.js, not a transcription of it. app.js is a browser global script, so it
// is evaluated in a vm with the thinnest possible DOM stub and the functions read back out.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";
import test from "node:test";

const src = readFileSync("web/app.js", "utf8");
const noop = () => {};
// A Proxy stub rather than a hand-listed fake DOM. app.js calls init() on load and touches
// dozens of DOM methods on the way; enumerating them was a rabbit hole and would rot the
// moment the UI changed. Any property read returns something callable AND indexable that
// yields more of itself, so every DOM call is a silent no-op and the module reaches the end.
const anyEl = () =>
  new Proxy(function () {}, {
    get(_t, k) {
      if (k === "classList") return { add: noop, remove: noop, toggle: noop, contains: () => false };
      if (k === "hidden") return false;
      if (k === "textContent" || k === "innerHTML" || k === "className" || k === "value") return "";
      if (k === Symbol.toPrimitive || k === "toString") return () => "";
      if (k === Symbol.iterator) return [][Symbol.iterator].bind([]);
      if (k === "length") return 0;
      return anyEl();
    },
    set: () => true,
    apply: () => anyEl(),
    has: () => true,
  });
const ctx = {
  document: anyEl(),
  window: anyEl(),
  localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  console,
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) }),
  setTimeout, clearTimeout, setInterval: noop, clearInterval: noop,
  requestAnimationFrame: noop, cancelAnimationFrame: noop,
  navigator: { userAgent: "" },
  location: { href: "", search: "", hash: "", pathname: "/", replace: noop },
  history: { replaceState: noop, pushState: noop },
  URL, URLSearchParams, AbortController,
  IntersectionObserver: class { observe() {} disconnect() {} unobserve() {} },
  ResizeObserver: class { observe() {} disconnect() {} unobserve() {} },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
new vm.Script(src + "\n;({FACET_GROUPS, state, passesFacets, jobLabeledFor, unlabelledPasses});")
  .runInContext(ctx);
const api = new vm.Script("({FACET_GROUPS, state, passesFacets, jobLabeledFor, unlabelledPasses})")
  .runInContext(ctx);

const { state, passesFacets, jobLabeledFor, unlabelledPasses } = api;
const select = (key, ...vals) => { state.filters.facets = {}; state.filters.facets[key] = new Set(vals); };

test("a job labelled with the chosen category passes", () => {
  select("category", "Engineering");
  assert.equal(passesFacets({ category: "Engineering", tags: [] }), true);
});

test("a job labelled with a DIFFERENT category is filtered out", () => {
  select("category", "Engineering");
  assert.equal(passesFacets({ category: "Healthcare", tags: [] }), false);
});

test("THE FIX: a job with NO category survives a category selection", () => {
  // 168 of 200 jobs vanished on one chip click; ~136 for having no category at all.
  select("category", "Engineering");
  assert.equal(passesFacets({ category: "", tags: [] }), true);
  assert.equal(passesFacets({ tags: [] }), true);
  assert.equal(passesFacets({ category: null, tags: [] }), true);
});

test("the same exemption applies to a TAG group (remote), not just own-fields", () => {
  select("remote", "remote");
  assert.equal(passesFacets({ tags: ["remote"] }), true);
  assert.equal(passesFacets({ tags: ["onsite"] }), false);
  assert.equal(passesFacets({ tags: [] }), true, "unstated work style must not delete the row");
  assert.equal(passesFacets({ tags: ["senior"] }), true, "a seniority tag is not a work-style label");
});

test("a seniority tag does not make a job 'labelled' for salary", () => {
  assert.equal(jobLabeledFor({ tags: ["senior"] }, { key: "salary_band", own: false }), false);
  assert.equal(jobLabeledFor({ tags: ["120-180k"] }, { key: "salary_band", own: false }), true);
});

test("AND-across still holds for labelled rows", () => {
  state.filters.facets = { category: new Set(["Engineering"]), remote: new Set(["remote"]) };
  assert.equal(passesFacets({ category: "Engineering", tags: ["remote"] }), true);
  assert.equal(passesFacets({ category: "Engineering", tags: ["onsite"] }), false);
  assert.equal(passesFacets({ category: "Healthcare", tags: ["remote"] }), false);
});

test("the exemption is COUNTED so it can be shown, not silently applied", () => {
  select("category", "Engineering");
  const list = [{ category: "Engineering", tags: [] }, { category: "", tags: [] }, { tags: [] }];
  assert.equal(JSON.stringify(unlabelledPasses(list)), JSON.stringify([{ title: "Field", n: 2 }]));
});

test("no selection means no exemption note", () => {
  state.filters.facets = {};
  assert.equal(JSON.stringify(unlabelledPasses([{ tags: [] }])), JSON.stringify([]));
});
