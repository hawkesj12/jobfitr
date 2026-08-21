"""The conversation. A model that interviews, then drives its own search loop.

WHY THIS EXISTS, and it is a different shape from chat.py. The 2026-08-17 experiment that
produced five jobs Justin called right did NOT fill in a form. Its method, recorded in
_private/experiment-runs/: seven interview questions with ZERO tool calls, then **nine
separate searches**, each one chosen from what the previous returned — an automation lane
and a controls lane run specifically to test whether his manufacturing background retrieved
a different and better set than his AI background. It concluded from results that it did
not. Then it read descriptions and picked five, with rejections.

chat.py fills a config and runs ONE search. That single difference — one query versus nine
adaptive ones — is the gap between the product and the thing that worked, and no amount of
prompt-tuning on a one-shot config closes it.

THE INTERVIEW MAKES NO TOOL CALLS. That was explicit in the experiment and it is enforced
here: questions asked while looking at stock get steered by stock, and the point of the
interview is to learn what the person wants, not what happens to be in the pool tonight.

WHAT IT RUNS ON: search_jobs is the hybrid arm (semantic.hybrid over the pre-filtered pool
— FTS5 title BM25 fused with Model2Vec by RRF), never /api/score. That endpoint is the free
deterministic floor for people who never open the chat; it is not this path's retrieval.

THE HANDICAP IS REMOVED. The experiment judged every job on 1,200 characters, about two
paragraphs, and said so as a flaw. read_jobs serves the whole posting.
"""

from __future__ import annotations

import json
import logging
import time
import os

import httpx
from datetime import datetime
from zoneinfo import ZoneInfo

from . import prompts, searchlog, semantic, store

log = logging.getLogger("jobfitr.chat")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# ═══════════════════════════════════════════════════════════════════════════
# THE MODEL BENCH — pick one by moving ACTIVE_MODEL. CHAT_MODEL env overrides.
# ═══════════════════════════════════════════════════════════════════════════
# Every entry VERIFIED tool-capable against the live OpenRouter catalog on
# 2026-08-19 (`"tools" in supported_parameters`). That filter is not optional here: this
# path is a tool-use loop, and a model without tool support does not degrade — it holds a
# conversation and never searches.
#
# `session_usd` is a MODELLED figure, not a measured one: 150k input / 6k output, which is
# an interview plus three searches at 40 rows plus a dozen full reads. Real sessions vary
# with how many searches the model chooses to run — which is the whole point of the loop,
# so treat these as a ranking, not a bill. Prices move; re-fetch before quoting them.
#
# MEASURED 2026-08-20 — a 14-model screen, persona B (a Louisville warehouse supervisor
# who cannot work nights or take a pay cut), against a pre-warmed FROZEN 34,226-row pool.
# 9 of 14 passed a binary gate: searched, READ, delivered or honestly declined, fabricated
# nothing. `_private/model-bakeoff/` holds the harness, results and the searches themselves.
#
# The five failures are worth knowing because each fails differently: gemini-2.5-flash-lite
# recommended off 320-char snippets without reading; minimax-m3 never searched at all;
# glm-4.7-flash and grok-4.6 exhausted the tool budget; grok-4.20 FABRICATED 3 urls — caught
# only because `fabricated` reached the response that morning.
#
# PRICE DOES NOT PREDICT ANY OF IT. sonnet-5 ($0.61/session) and deepseek-v4-flash ($0.009)
# both pass; grok-4.6 and gemini-2.5-flash-lite sit at opposite ends and both fail. The tiers
# below group by cost and that is ALL they group by — do not read capability into them.
#
# WHY deepseek-v4-flash IS ACTIVE, and it is not the price: it is the only model that ran
# MULTIPLE searches and let each one change the next. Its third search asked for
# "1st Shift Supervisor" / "Day Shift Supervisor" — turning the persona's stated reason for
# leaving ("six years on nights, I want off nights") into a title query. No other model did.
# All five of its picks were at her actual level; qwen3.5-flash returned five MANAGER roles
# to a supervisor and still scored 5/5 clean, because no column expresses over-levelling.
#
# TWO THINGS THIS DOES NOT ESTABLISH. It is n=2 on ONE persona — enough to separate "always
# completes" from "coin flip" (gemini-2.5-flash passed once, then recommended five jobs
# having read NONE of them) and NOT enough to rank the passers. And persona A has not been
# re-run since the harness was fixed. Do not read an order into the survivors.
#
# `session_usd` remains modelled; MEASURED per-conversation cost now comes back on the
# response as `usage` and is what the harness records.
MODELS = {
    # ── cheap, and a million tokens of context ───────────────────────────────
    "qwen3.5-flash":     {"id": "qwen/qwen3.5-flash-02-23",   "ctx": 1_000_000, "session_usd": 0.011},
    "deepseek-v4-flash": {"id": "deepseek/deepseek-v4-flash",  "ctx": 1_048_576, "session_usd": 0.013},
    "glm-4.7-flash":     {"id": "z-ai/glm-4.7-flash",          "ctx":   202_752, "session_usd": 0.011},
    "gemini-2.5-flash-lite": {"id": "google/gemini-2.5-flash-lite", "ctx": 1_048_576, "session_usd": 0.017},
    # ── mid: where a conversational protocol starts holding reliably ─────────
    "gemini-2.5-flash":  {"id": "google/gemini-2.5-flash",     "ctx": 1_048_576, "session_usd": 0.060},
    "gemini-3.7-flash":  {"id": "google/gemini-3.7-flash",     "ctx": 1_048_576, "session_usd": 0.067},
    "glm-4.7":           {"id": "z-ai/glm-4.7",                "ctx":   204_800, "session_usd": 0.070},
    "kimi-k2.5":         {"id": "moonshotai/kimi-k2.5",        "ctx":   262_144, "session_usd": 0.081},
    "minimax-m3":        {"id": "minimax/minimax-m3",          "ctx": 1_048_576, "session_usd": 0.052},
    # ── upper: the top of the band Justin drew (above Haiku, below Sonnet) ───
    "grok-4.3":          {"id": "x-ai/grok-4.3",               "ctx": 1_000_000, "session_usd": 0.203},
    "grok-4.20":         {"id": "x-ai/grok-4.20",              "ctx": 2_000_000, "session_usd": 0.203},
    "grok-4.6":          {"id": "x-ai/grok-4.6",               "ctx":   500_000, "session_usd": 0.336},
    # ── the CONTROL, not a candidate. If a model this capable also fails the loop, the
    #    fault is the tools or the prompt, not the model. That is what it is here for.
    "sonnet-5":          {"id": "anthropic/claude-sonnet-5",   "ctx": 1_000_000, "session_usd": 0.360},
    "gemini-3.5-flash":  {"id": "google/gemini-3.5-flash",     "ctx": 1_048_576, "session_usd": 0.279},
    # NOTE grok-4.3 carries TWICE the context of grok-4.6 at 60% of the cost, and grok-4.20
    # carries four times it for the same price. If the reason for reaching for Grok is
    # capability rather than the specific 4.6 build, 4.3 is the better entry point.
}

# ↓↓↓ THE ONE LINE TO CHANGE ↓↓↓
ACTIVE_MODEL = "deepseek-v4-flash"   # measured 2026-08-20; see the bench note

DEFAULT_MODEL = os.environ.get("CHAT_MODEL") or MODELS[ACTIVE_MODEL]["id"]
MAX_TOOL_CALLS = int(os.environ.get("CHAT_MAX_TOOL_CALLS", "12"))
MAX_TURNS = int(os.environ.get("CHAT_MAX_TURNS", "24"))
REQUEST_TIMEOUT = float(os.environ.get("CHAT_TIMEOUT", "90"))
SHALLOW_CHARS = int(os.environ.get("CHAT_SHALLOW_CHARS", "320"))
DEEP_CHARS = int(os.environ.get("CHAT_DEEP_CHARS", "6000"))
MAX_READ = int(os.environ.get("CHAT_MAX_READ", "25"))

# ── what the model is told about the corpus ──────────────────────────────────
# Fill rates are MEASURED over the live pool (65,391 rows, 2026-08-19) and they are in the
# prompt for one reason: a model that does not know `seniority` is 28% filled will filter on
# it and silently delete 72% of the corpus. NULL means "the source never said", never "no".

TOOLS_NOTE = prompts.load("chat_tools")



# ═══════════════════════════════════════════════════════════════
# store_glimpse()
# ═══════════════════════════════════════════════════════════════
# Show the model the store instead of describing it. Two REAL rows and the
# live column list, read at runtime — so this is correct on schema v3, v4
# or v27 without anyone remembering to update a prompt.
# ═══════════════════════════════════════════════════════════════
# It replaced a hand-written note that asserted "remote 45%, seniority 28%,
# category 12%". Those were measured once and then rotted silently as the
# harvester improved, which is the worst failure shape available: a model
# filtering confidently against a picture of the store that is no longer true.
#
# TWO rows, not one and not five. One row cannot show that a field is often
# absent — and "a blank field means the source did not say it, never no" is
# the single most important thing about this corpus, because a model that
# filters on `seniority` throws away every posting that never stated one.
# So: the most complete row available, and the sparsest, side by side. Five
# would spend tokens re-teaching what the second row already taught.
_GLIMPSE: str | None = None
GLIMPSE_BODY = int(os.environ.get("CHAT_GLIMPSE_BODY", "600"))


def store_glimpse(force: bool = False) -> str:
    """A rendered sample of the live store: its columns and two real rows.

    Returns "" when the store cannot be read — an empty section is honest, and the
    alternative (asserting field names that may not exist) is how a model writes a
    filter against a column that was renamed two schema versions ago.
    """
    global _GLIMPSE
    if _GLIMPSE is not None and not force:
        return _GLIMPSE
    try:
        import sqlite3

        with store._conn() as con:
            con.row_factory = sqlite3.Row
            cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)")]
            # richest first, then sparsest: ordering by how many of the optional
            # fields are actually populated, which is the axis being demonstrated.
            optional = [c for c in cols if c not in ("url", "title", "company", "body")]
            score = " + ".join(
                f'(CASE WHEN "{c}" IS NOT NULL AND trim(CAST("{c}" AS TEXT)) <> \'\' THEN 1 ELSE 0 END)'
                for c in optional
            ) or "0"
            rich = con.execute(
                f"SELECT * FROM jobs WHERE body IS NOT NULL AND length(body) > 800 "
                f"ORDER BY ({score}) DESC LIMIT 1").fetchone()
            thin = con.execute(
                f"SELECT * FROM jobs WHERE body IS NOT NULL "
                f"ORDER BY ({score}) ASC LIMIT 1").fetchone()
        if not rich or not thin:
            return ""
        total = len(cols)

        # LIVE fill rates, one pass. The numbers themselves were always the useful part —
        # hardcoding them was the mistake, because they drift every time the harvester
        # improves and nothing tells you the prompt went stale. Computed here they are
        # true for whatever schema and whatever corpus is actually loaded.
        with store._conn() as con2:
            sel = ", ".join(
                f'SUM(CASE WHEN "{c}" IS NOT NULL AND trim(CAST("{c}" AS TEXT)) <> \'\' THEN 1 ELSE 0 END)'
                for c in cols)
            counts = con2.execute(f"SELECT COUNT(*), {sel} FROM jobs").fetchone()
        n = counts[0] or 1
        fill = {c: counts[i + 1] / n for i, c in enumerate(cols)}
        # Only the columns worth a decision. url/title/company are always there and
        # listing them at 100% teaches nothing; the internal `_basis`/`_raw` provenance
        # columns are not the model's business.
        show = [c for c in cols
                if c not in ("url", "title", "company", "body", "fetched_at", "last_seen")
                and not c.endswith(("_basis", "_raw", "_extra"))]
        bar = lambda f: "\u2588" * round(f * 5) + "\u2591" * (5 - round(f * 5))  # noqa: E731
        stats = "\n".join(
            f"  {c:<20} {fill[c] * 100:3.0f}% {bar(fill[c])}"
            for c in sorted(show, key=lambda c: -fill[c]))

        def render(row, label):
            lines = [f"### {label}"]
            for c in cols:
                v = row[c]
                v = "" if v is None else str(v)
                if c == "body":
                    v = store.plain(v)[:GLIMPSE_BODY] + (" …" if len(v) > GLIMPSE_BODY else "")
                elif len(v) > 160:
                    v = v[:160] + " …"
                lines.append(f"{c}: {v}" if v else f"{c}:")
            return "\n".join(lines)

        _GLIMPSE = (
            f"The store holds ~{store.pool_size():,} live postings in a table of {total} "
            "columns. Here are two REAL rows from it, so you can see the shape rather than "
            "be told it.\n\n"
            + render(rich, "A richly populated posting")
            + "\n\n"
            + render(thin, "A sparse one — and this is the common case")
            + "\n\n### How often each field is actually populated, right now\n"
            + stats
            + "\n\nA BLANK FIELD MEANS THE SOURCE NEVER SAID IT. It does not mean no. Most "
            "postings state no seniority and no salary; filtering on a field you merely wish "
            "were populated silently discards the majority of the corpus."
        )
        return _GLIMPSE
    except Exception as e:  # noqa: BLE001 — no glimpse is survivable, a wrong one is not
        log.warning("store glimpse unavailable (%s: %s)", type(e).__name__, e)
        return ""


def system_prompt() -> str:
    """The system prompt, with a live look at the store spliced in."""
    return prompts.render("chat_system", tools=TOOLS_NOTE, schema=store_glimpse())

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_jobs",
            "description": "Search the job store. Returns shallow rows (facts + a short snippet). Call it several times with different framings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "titles": {"type": "array", "items": {"type": "string"},
                               "description": "Job titles as the market writes them."},
                    "probes": {"type": "array", "items": {"type": "string"},
                               "description": "2-6 SENTENCES describing the work. Prose, not keywords. Never about remote/salary/seniority, never negative."},
                    "location": {"type": "string"},
                    "remote_only": {"type": "boolean"},
                    "salary_floor": {"type": "integer", "description": "Annual USD. Rows with no stated salary are KEPT."},
                    "max_age_days": {"type": "integer"},
                    "k": {"type": "integer", "description": "How many rows to return (default 40, max 120)."},
                    "why": {"type": "string", "description": "One line: what this search is testing."},
                },
                "required": ["titles", "probes", "why"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend",
            "description": "Deliver the final picks. Call this ONCE, after reading, with the jobs you are recommending. Then write your answer to the person in your own words.",
            "parameters": {
                "type": "object",
                "properties": {
                    "picks": {
                        "type": "array",
                        "description": "The jobs you are recommending, best first. Five unless you genuinely cannot justify five.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "description": "Must be a url a tool returned in this conversation."},
                                "why": {"type": "string", "description": "One or two sentences: why this job fits THIS person. The work, not the words that matched."},
                                "caveat": {"type": "string", "description": "Anything about it that conflicts with what they told you, or '' if nothing does."},
                            },
                            "required": ["url", "why"],
                        },
                    },
                    "rejected": {
                        "type": "array",
                        "description": "Jobs they would expect to see, and what ruled each one out.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string"},
                                "why_not": {"type": "string"},
                            },
                            "required": ["url", "why_not"],
                        },
                    },
                    "nothing_fits": {
                        "type": "boolean",
                        "description": "True if the pool genuinely holds nothing worth applying to. Say so rather than padding.",
                    },
                },
                "required": ["picks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_jobs",
            "description": "Read the FULL text of specific postings. Use before judging whether someone should apply.",
            "parameters": {
                "type": "object",
                "properties": {
                    "urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["urls"],
            },
        },
    },
]


def _shallow(c: dict) -> dict:
    body = store.plain(c.get("body") or "")
    return {
        "url": c.get("url", ""),
        "title": c.get("title", ""),
        "company": c.get("company", ""),
        "location": c.get("location", ""),
        # `state` is what `_in_place` actually decides on, and it was missing here — so a
        # harness grading these picks could not reproduce the predicate it was grading,
        # and had to fall back to substring-matching the city. Two characters per row.
        "state": c.get("state") or "",
        "remote": c.get("remote") or "",
        "salary": c.get("salary", ""),
        "salary_min": store.annual_salary(c),
        "posted": c.get("posted", ""),
        "snippet": body[:SHALLOW_CHARS],
    }


# A frozen pool for measurement. Set during a bakeoff, never in production.
#
# WHY A FLAG AND NOT A PER-CELL DB CLONE: the live lane MUTATES the shared pool — an
# identical persona-B search measured 22 rows cold and 30 rows on a repeat, a 36% larger
# candidate set from nothing but running second. Under a live pool "search more" strictly
# dominates "search better", because every extra search both fetches rows and warms the
# pool for whoever runs next. Cloning the DB isolates the effect; freezing removes it, and
# is testable from outside rather than implicit in a filename.
#
# The model cannot tell: `_warm_cache` returns None both when it fetched and when the
# cache was already fresh, so a pre-warmed frozen pool shows the lane's OUTPUT with none
# of its latency and no free-riding. It is also the only affordable design — Google for
# Jobs bills one metered SerpApi search per title per page, so 14 models x 2 personas x
# ~5 searches x ~3 titles is ~420 searches against a MEASURED remaining budget of 88
# (2026-08-20). A live run would run dry around the fourth model and every later cell
# would silently lose the lane that carries local non-tech employers — reproducing the
# very bug this bakeoff exists to re-measure, invisibly, in its own second half.
FREEZE_POOL = os.environ.get("JOBFITR_FREEZE_POOL") == "1"


def _warm_live(titles: list[str], location: str) -> str | None:
    if FREEZE_POOL:
        return None
    """One bounded live fetch for this (titles, location), or the fresh cache untouched.

    Deliberately imported lazily from the server: the fetch policy, the daily ceiling and
    the load-shed all live there with the endpoints, and duplicating them here is how the
    two paths would drift into disagreeing about what "fresh" means.
    """
    try:
        from .server import _warm_cache

        return _warm_cache(titles, location)
    except Exception as e:  # noqa: BLE001 — a dead live lane must not kill the search
        log.warning("live warm failed (%s: %s) — serving the cache", type(e).__name__, e)
        return "fetch_error"


def _place_filter(location: str) -> tuple[str, str] | None:
    """A requested place -> (city_lower, STATE) to test rows against. None means anywhere."""
    loc = (location or "").strip()
    if not loc or loc.lower() in ("remote", "anywhere", "any", "us", "usa", "united states"):
        return None
    parts = [x.strip() for x in loc.split(",")]
    city = parts[0].lower() if parts else ""
    state = parts[1].upper()[:2] if len(parts) > 1 else ""
    return (city, state)


def _in_place(row: dict, place: tuple[str, str]) -> bool:
    """Is this row available to someone in `place`?

    A REMOTE row is available everywhere — that is the point of remote, and dropping it
    would delete most of what a location search should surface. Otherwise the row has to
    name the state or the city.
    """
    if (row.get("remote") or "") == "remote":
        return True
    city, state = place
    if state and (row.get("state") or "").upper() == state:
        return True
    text = f"{row.get('location') or ''} {row.get('city') or ''}".lower()
    return bool(city) and city in text


def search_jobs(args: dict) -> dict:
    """The hybrid arm. Pre-filter, then BOTH arms over the filtered pool, fused by RRF."""
    from .config_builder import filters_from_dict

    titles = [t for t in (args.get("titles") or []) if str(t).strip()]
    probes = [p for p in (args.get("probes") or []) if str(p).strip()]
    k = max(1, min(int(args.get("k") or 40), 120))
    f = filters_from_dict(
        {"titles": titles, "location": args.get("location") or "",
         "remote_only": bool(args.get("remote_only"))})
    age = args.get("max_age_days")
    # FETCH LIVE FIRST. Without this the agent only ever sees the baseline harvest, which
    # is structurally remote-and-tech (~88% greenhouse ATS boards) and — in CLAUDE.md's own
    # words — "was never meant to hold a Louisville warehouse job". The local lane IS the
    # live fetch: Google for Jobs alone supplied 14 of 17 employers on one measured
    # Louisville search that appeared in NO other source.
    #
    # This was missing until 2026-08-20 and it made local search structurally impossible on
    # the agent path, which is now the whole product. The symptom was not an error: a
    # warehouse supervisor in Louisville got out-of-state jobs and the models that refused
    # to recommend them were scored as failures.
    #
    # Idempotent and single-flighted, so several searches in one conversation cost at most
    # one upstream fetch per (titles, location).
    degraded = _warm_live(titles, args.get("location") or "")
    rows = store.bm25_candidates(titles) if titles else []
    # Hard filters BEFORE retrieval depth is spent. See semantic.hybrid.
    _t0 = time.perf_counter()
    floor = args.get("salary_floor")
    place = _place_filter(f.location)

    def eligible(c: dict) -> bool:
        """ONE predicate, applied to BOTH arms.

        The lexical arm's rows were filtered and the dense arm's were not, so a search
        could return 11 rows having reported that 8 passed — and the extras were exactly
        the ones the user had excluded. A filter that runs on one retrieval path is not a
        filter; it is a suggestion.
        """
        if f.remote_only and (c.get("remote") or "") in ("onsite", "hybrid"):
            return False
        # LOCATION. This was advertised in the tool schema and NOT IMPLEMENTED until
        # 2026-08-20, and the cost was not a missing feature — it was a wrong answer that
        # looked right. A Louisville warehouse supervisor who cannot relocate was handed
        # Aurora IL, Laredo TX, Memphis TN and two air-traffic-control roles, with zero
        # Louisville rows in 25. Models that refused to recommend from that were behaving
        # correctly and were scored as failures; models that recommended anyway were scored
        # as passing. An unimplemented filter is worse than an absent one, because the
        # caller trusts it.
        if place and not _in_place(c, place):
            return False
        if age and (c.get("posted") or ""):
            from job_radar.util import age_int

            a = age_int(c.get("posted"))
            if a is not None and a > int(age):
                return False
        if floor:
            sal = store.annual_salary(c)
            if sal is not None and sal < int(floor):  # unstated salary is KEPT
                return False
        return True

    keep = [c for c in rows if eligible(c)]
    by_url = {c["url"]: c for c in keep if c.get("url")}
    # Snapshot BEFORE the dense extras are merged in. Taking it afterwards makes every
    # dense-only hit look lexical, which is how the provenance counts read 0 on their
    # first run — the exact class of error this logging exists to surface.
    lexical_set = set(by_url)
    fused = semantic.hybrid(list(by_url), probes, k=k,
                            per_company={u: (r.get("company") or "") for u, r in by_url.items()})
    extra = [u for u in fused if u not in by_url]
    if extra:
        # THROUGH THE SAME GATE. A dense hit never met `eligible` — it was found by meaning
        # over the whole pool, not by retrieving from the filtered set.
        by_url.update({c["url"]: c for c in store.rows_by_url(extra)
                       if c.get("url") and eligible(c)})
    out = [_shallow(by_url[u]) for u in fused if u in by_url][:k]

    # SHAPE ONLY — see searchlog.record_search. `in_place` is the number that would have
    # caught the unimplemented location filter on its first ever call.
    delivered_urls = [u for u in fused if u in by_url][:k]
    searchlog.record_search(
        n_titles=len(titles), n_probes=len(probes),
        probe_chars=sum(len(p) for p in probes),
        has_location=bool(f.location), remote_only=bool(f.remote_only),
        salary_floor=int(floor) if floor else None,
        max_age_days=int(age) if age else None, k=k,
        matched=len(keep), returned=len(out),
        in_place=(sum(1 for u in delivered_urls if _in_place(by_url[u], place))
                  if place else None),
        from_lexical=sum(1 for u in delivered_urls if u in lexical_set),
        from_dense=sum(1 for u in delivered_urls if u not in lexical_set),
        from_both=0,  # the arms are fused by RRF, so "both" is not separable per row
        fetched=degraded is None and bool(titles), degraded=degraded,
        elapsed_ms=(time.perf_counter() - _t0) * 1000,
    )
    return {"searched": {"titles": titles, "probes": probes, "why": args.get("why", "")},
            "degraded": degraded,
            "matched_before_fusion": len(keep), "returned": len(out),
            "semantic": semantic.available(), "jobs": out}


def read_jobs(args: dict) -> dict:
    """The FULL posting text. Removes the handicap the 2026-08-17 experiment ran under:
    it judged every job on 1,200 characters, about two paragraphs, and said so as a flaw.

    No section parsing. jobfitr used to split the body on its HTML headers, which only
    worked because job-radar was leaking raw markup — an upstream bug, now being fixed.
    Cleaning the body upstream removes the tags and with them anything to parse, so a
    parser here would silently return nothing. Sections, if they are ever wanted, are the
    engine's job to emit; this reads whatever `body` holds.
    """
    asked = [u for u in (args.get("urls") or []) if str(u).strip()]
    urls = asked[:MAX_READ]
    # SILENTLY dropping the overflow is how a model ends up recommending a job it believes
    # it read and never saw. One run passed 35 urls and got 25 back with no indication.
    # Telling it costs a line and lets it ask for the rest.
    unread = asked[MAX_READ:]
    out = []
    for c in store.rows_by_url(urls):
        out.append({
            "url": c.get("url", ""), "title": c.get("title", ""),
            "company": c.get("company", ""), "location": c.get("location", ""),
            "remote": c.get("remote") or "", "salary": c.get("salary", ""),
            "posted": c.get("posted", ""), "team": c.get("team") or "",
            "seniority": c.get("seniority") or "", "employment_type": c.get("employment_type") or "",
            "body": store.plain(c.get("body"))[:DEEP_CHARS],
        })
    result = {"read": len(out), "jobs": out}
    if unread:
        result["not_read"] = unread
        result["note"] = (f"Only the first {MAX_READ} were read. {len(unread)} were NOT — "
                          "call read_jobs again for those before judging them.")
    missing = [u for u in urls if u not in {j["url"] for j in out}]
    if missing:
        # A url that is not in the pool: invented, or evicted since the search returned it.
        result["not_found"] = missing
    return result


def recommend(args: dict) -> dict:
    """The final picks, as DATA rather than as prose the client has to parse.

    A third tool rather than a parse of the closing message, for one reason: the front end
    has to render cards, and pulling five urls out of free text is the kind of thing that
    works in testing and fails on the first answer written slightly differently. This also
    lets the server VERIFY every url actually came from the pool before it reaches anyone —
    a hallucinated listing is the one failure the whole product cannot survive, because the
    person goes looking for a job that does not exist.
    """
    picks = args.get("picks") or []
    urls = [p.get("url", "") for p in picks if p.get("url")]
    rejected = args.get("rejected") or []
    known = {r["url"]: r for r in store.rows_by_url(urls + [r.get("url", "") for r in rejected]) if r.get("url")}
    out, dropped = [], []
    for p in picks:
        row = known.get(p.get("url", ""))
        if not row:                       # not in the pool = invented, or long evicted
            dropped.append(p.get("url", ""))
            continue
        out.append({**_shallow(row), "why": p.get("why", ""), "caveat": p.get("caveat", "")})
    if dropped:
        log.warning("recommended %d url(s) not in the store: %s", len(dropped), dropped)
    return {
        "delivered": len(out),
        "picks": out,
        "rejected": [{**_shallow(known[r["url"]]), "why_not": r.get("why_not", "")}
                     for r in rejected if r.get("url") in known],
        "nothing_fits": bool(args.get("nothing_fits")),
        # Told back to the MODEL so it can correct itself in the closing message rather
        # than describing a job the person will never see on screen.
        "rejected_by_server": dropped or None,
    }



# ═══════════════════════════════════════════════════════════════
# cost control and message hygiene
# ═══════════════════════════════════════════════════════════════
# Carried over from the retired config-filling chat. These were never part of that
# design — they are what stops a metered endpoint from being an open tap, and what
# refuses a malformed or smuggled turn before it reaches a model. They outlived the
# thing they were written for.
# ═══════════════════════════════════════════════════════════════
_ET = ZoneInfo("America/New_York")
DAILY_CEILING = int(os.environ.get("CHAT_DAILY_CEILING", "500"))
_usage = {"date": "", "count": 0}


def available() -> bool:
    """Live only when a key is configured; without one the UI must say so, not hang."""
    return bool(os.environ.get("OPENROUTER_API_KEY"))


def over_turn_cap(messages: list) -> bool:
    """True once the conversation has run past MAX_TURNS user messages."""
    return sum(1 for m in messages if (m or {}).get("role") == "user") > MAX_TURNS


def sanitize_messages(raw: list) -> list:
    """Keep only well-formed user/assistant turns with string content.

    The client holds the transcript, so this is the boundary where anything odd is
    refused — a smuggled `system` role, a non-string content, an over-long blob —
    before it reaches the model.
    """
    out: list[dict] = []
    for m in raw or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            out.append({"role": role, "content": content[:4000]})
    return out


def _roll_day() -> None:
    today = datetime.now(_ET).date().isoformat()
    if _usage["date"] != today:
        _usage["date"], _usage["count"] = today, 0


def daily_ceiling_reached() -> bool:
    """True if today's request budget is spent — the endpoint then 503s to the form."""
    _roll_day()
    return _usage["count"] >= DAILY_CEILING


def note_request() -> None:
    """Count one accepted request against today's ceiling."""
    _roll_day()
    _usage["count"] += 1


DISPATCH = {"search_jobs": search_jobs, "read_jobs": read_jobs, "recommend": recommend}




async def _call(payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://jobfitr.app",
        "X-Title": "jobfitr",
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.post(OPENROUTER_URL, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


def resolve_model(name: str | None) -> str:
    """A caller-supplied model, but only from the bench.

    The bakeoff needs to vary the model per request; a public endpoint must not let a
    caller name an arbitrary one, because "model" is the field that decides what a request
    COSTS. Restricting it to MODELS keys makes the worst case a known entry on a curated
    list rather than whatever is most expensive in the catalogue today.
    """
    if name and name in MODELS:
        return MODELS[name]["id"]
    return DEFAULT_MODEL


async def turn(messages: list, model: str | None = None) -> dict:
    """Run a whole turn and return the final dict. THE UNCHANGED API.

    A thin drain over `turn_events()`. It exists so that streaming did not become a
    breaking change: /api/chat, the 297-test suite and the bakeoff harness all read this
    dict shape, and a refactor that made them all move at once would be a much larger
    change than the feature is worth.
    """
    final: dict = {}
    async for ev in turn_events(messages, model):
        if ev.get("type") == "done":
            final = ev["result"]
    return final


async def turn_events(messages: list, model: str | None = None):
    """Run the model until it produces text, yielding an EVENT as each step completes.

    WHY THIS IS A GENERATOR. The work is the interesting part of this product and it was
    invisible: /api/chat is one blocking POST, so the activity list — "searched X, 37
    found", "read 3 postings in full" — rendered all at once AFTER the turn finished.
    Measured 2026-08-20, deepseek-v4-flash takes 180 s for a full conversation, so that is
    three minutes of spinner followed by a summary of things that already happened. The
    `why` argument has always been required on `search_jobs` for exactly this ("The person
    sees this", chat_tools.md) — it just had nowhere to arrive in time.

    Events are dicts with a `type`: `tool` as each tool call returns, then exactly one
    `done` carrying the same dict `turn()` has always returned. Anything consuming this
    must ignore unknown types rather than assume the set is closed.

    Bounded twice over: MAX_TOOL_CALLS across the loop and MAX_TURNS on the transcript. An
    agent that can choose its own searches can also choose them forever, and this is the
    only thing standing between a curious model and the token bill.
    """
    model_id = resolve_model(model)
    convo = [{"role": "system", "content": system_prompt()}, *messages]
    trace, calls = [], 0
    picks: list = []
    rejected: list = []
    nothing_fits = False
    nudged = False
    fabricated = 0
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
    t0 = time.perf_counter()

    def _log_delivery() -> None:
        """Shape only — counts, never content. See searchlog.record_delivery.

        Called on BOTH exits, including the budget-exhausted one: a conversation that ran
        out of tool calls without delivering is exactly the failure worth counting, and it
        is the exit that returns no reply at all, so nothing else would ever notice it.
        """
        searchlog.record_delivery(
            turns=len(messages),
            searches=sum(1 for t in trace if t["tool"] == "search_jobs"),
            reads=sum(1 for t in trace if t["tool"] == "read_jobs"),
            picks=len(picks),
            rejected=len(rejected),
            fabricated=fabricated,
            nothing_fits=nothing_fits,
            model=model_id,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )
    while calls <= MAX_TOOL_CALLS:
        data = await _call({
            "model": model_id,
            "messages": convo, "tools": TOOLS, "tool_choice": "auto",
        })
        # OpenRouter returns per-request usage and it was being thrown away, so the only
        # cost figures in this repo were MODELLED (chat.MODELS' session_usd) rather than
        # measured — and the bench's price tiers are, per HANDOFF, actively misleading.
        # A conversation is many requests; the caller wants the total for the whole loop.
        u = data.get("usage") or {}
        usage["prompt_tokens"] += u.get("prompt_tokens") or 0
        usage["completion_tokens"] += u.get("completion_tokens") or 0
        usage["cost"] += float(u.get("cost") or 0.0)
        msg = data["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            # THE NUDGE, and it is load-bearing rather than defensive. Measured: a model
            # that has searched and read will write five jobs into its prose and never call
            # `recommend` — the answer reads fine and the screen shows no cards at all.
            # Asking politely in the prompt did not fix it. So if it tries to finish after
            # searching without ever delivering, it gets told once, in the transcript,
            # which is the only place a model reliably notices anything.
            searched = any(t["tool"] == "search_jobs" for t in trace)
            if searched and not picks and not nudged:
                nudged = True
                convo.append(msg)
                convo.append({"role": "user", "content":
                    "You have not called `recommend`, so the person's screen is empty — "
                    "whatever you wrote is invisible to them. Call `recommend` now with the "
                    "jobs you chose (url, why, caveat) and the rejections worth naming, "
                    "using only urls a tool returned. Then write your answer."})
                continue
            if picks or nothing_fits:
                _log_delivery()
            yield {"type": "done", "result": {
                    "reply": msg.get("content") or "", "trace": trace,
                    "tool_calls": calls, "model": data.get("model"),
                    "picks": picks, "rejected": rejected, "nothing_fits": nothing_fits,
                    # `fabricated` is in the RESPONSE, not only in searchlog, because
                    # searchlog is off unless JOBFITR_SEARCH_LOG is set — so a harness
                    # grading these conversations was structurally blind to the one
                    # failure the product cannot survive.
                    "fabricated": fabricated, "usage": usage}}
            return
        convo.append(msg)
        for tc in tool_calls:
            calls += 1
            name = tc["function"]["name"]
            args: dict = {}
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
                result = DISPATCH[name](args) if name in DISPATCH else {"error": f"no tool {name}"}
            except Exception as e:  # a tool fault is a RESULT the model can react to,
                log.warning("chat tool %s failed: %s", name, e)  # never a dead conversation
                result = {"error": f"{type(e).__name__}: {e}"}
            if name == "recommend" and isinstance(result, dict):
                fabricated += len(result.get("rejected_by_server") or [])
                picks = result.get("picks") or picks
                rejected = result.get("rejected") or rejected
                nothing_fits = result.get("nothing_fits") or nothing_fits
            step = {"tool": name, "args": args,
                    "returned": (result.get("returned") if "returned" in result
                                 else result.get("read") if "read" in result
                                 else result.get("delivered", 0)),
                    "why": args.get("why", "")}
            trace.append(step)
            # THE LIVE EVENT. Emitted the moment the tool returns, so the person watches
            # the search happen instead of reading about it three minutes later. It
            # carries `titles` deliberately: seeing the model pivot to "1st Shift
            # Supervisor" after someone says they want off nights is the moment that
            # shows the reasoning is real, and `why` alone does not show it.
            yield {"type": "tool", "tool": name, "why": step["why"],
                   "returned": step["returned"],
                   "titles": (args.get("titles") or [])[:6],
                   "location": args.get("location") or "",
                   "n": calls}
            convo.append({"role": "tool", "tool_call_id": tc["id"],
                          "content": json.dumps(result)[:120000]})
    _log_delivery()
    yield {"type": "done", "result": {
        "reply": "", "trace": trace, "tool_calls": calls,
        "error": "tool_budget_exhausted",
        "fabricated": fabricated, "usage": usage}}
