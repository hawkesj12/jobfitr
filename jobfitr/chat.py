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

import asyncio
import json
import logging
import os
import re
import secrets
import time

import httpx
from datetime import datetime
from zoneinfo import ZoneInfo

from . import checks, prompts, searchlog, semantic, store

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
    # ── added 2026-11-11 for the exec-readiness runs; ids and prices read off the live
    #    OpenRouter catalog that day ($/M in, $/M out): haiku-5.5 0.10/0.50, sonnet-5.5
    #    2/10, gpt-4o-mini is what the box had been running through CHAT_MODEL.
    "haiku-5.5":         {"id": "anthropic/claude-haiku-5.5",  "ctx": 1_000_000, "session_usd": 0.020},
    "sonnet-5.5":        {"id": "anthropic/claude-sonnet-5.5", "ctx": 1_000_000, "session_usd": 0.400,
                          "label": "Claude Sonnet 5.5"},
    "gpt-4o-mini":       {"id": "openai/gpt-4o-mini",          "ctx":   128_000, "session_usd": 0.025},
    # NOTE grok-4.3 carries TWICE the context of grok-4.6 at 60% of the cost, and grok-4.20
    # carries four times it for the same price. If the reason for reaching for Grok is
    # capability rather than the specific 4.6 build, 4.3 is the better entry point.
}

# ↓↓↓ THE ONE LINE TO CHANGE ↓↓↓
# sonnet-5.5 since 2026-11-11, and the reason is the tester runs, not the bench above: on
# gpt-4o-mini (what the box ran) and on deepseek the interview ignored "just search",
# re-asked what it had been told, and wrote "aligns well" over jobs that did not fit. The
# checks in checks.py now catch what a model gets wrong about the brief; this choice is
# about the parts no check can do: asking sensible questions, reading a posting, and
# saying plainly why it fits. About $0.40 a conversation against $0.01, with the daily
# ceiling lowered to match.
ACTIVE_MODEL = "sonnet-5.5"

DEFAULT_MODEL = os.environ.get("CHAT_MODEL") or MODELS[ACTIVE_MODEL]["id"]
MAX_TOOL_CALLS = int(os.environ.get("CHAT_MAX_TOOL_CALLS", "16"))
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
            "description": "Deliver the picks. The server checks every pick against `brief` and against the posting's own text before the person sees anything, and tells you what it removed. Then write a short closing message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "brief": {
                        "type": "object",
                        "description": "What the person said they need, as data. Only what they actually said; leave a field out when they did not say it. The server removes any pick that clearly breaks it.",
                        "properties": {
                            "remote_only": {"type": "boolean", "description": "True only if they said the job must be remote."},
                            "location": {"type": "string", "description": "Where they live or want to work, e.g. 'Louisville, KY'. '' if never said."},
                            "salary_floor": {"type": "integer", "description": "The lowest annual USD pay they will take. Omit if never said."},
                            "exclude": {"type": "array", "items": {"type": "string"},
                                        "description": "NAMES of employers they ruled out. Turn a category into names: 'no big consulting' becomes Accenture, Deloitte, PwC, KPMG, EY, McKinsey, BCG, Bain, Capgemini, Cognizant, Infosys, Wipro, TCS, IBM Consulting, Slalom, Booz Allen. The person is shown this list."},
                            "no_agencies": {"type": "boolean", "description": "True if they ruled out recruiters or staffing agencies."},
                            "must_mention": {
                                "type": "array",
                                "description": "Things the work should involve, each as a label plus the words a posting would use for it. A pick that never uses any of the words gets a warning on its card. Use it for the industry or the key skill they named.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string", "description": "e.g. 'manufacturing or distribution'"},
                                        "any_of": {"type": "array", "items": {"type": "string"},
                                                   "description": "e.g. manufacturing, manufacturer, distribution, distributor, warehouse, supply chain, industrial, factory, plant"},
                                    },
                                    "required": ["label", "any_of"],
                                },
                            },
                            "experience_years": {"type": "integer", "description": "Years they have done THIS kind of work. 0 if they are changing fields. Omit if unknown."},
                        },
                    },
                    "picks": {
                        "type": "array",
                        "description": "The jobs you recommend, best first. Up to five. Fewer is fine; never pad.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "description": "A url you passed to read_jobs in this conversation."},
                                "why": {"type": "string", "description": "One or two plain sentences: what this person would be doing in this job and why that suits what they told you. Name the work. Never 'aligns well' or 'great fit'."},
                                "quote": {"type": "string", "description": "One sentence copied WORD FOR WORD from the posting that backs up `why`. It is checked against the posting."},
                                "warning": {"type": "string", "description": "Anything in the posting that conflicts with what they told you, or that they are missing (a required skill, more seniority). '' only if there is truly nothing."},
                            },
                            "required": ["url", "why", "quote"],
                        },
                    },
                    "ruled_out": {
                        "type": "array",
                        "description": "Jobs you looked at that they would expect to see, and what ruled each one out. Only urls a tool returned.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string"},
                                "why_not": {"type": "string", "description": "One plain sentence."},
                                "quote": {"type": "string", "description": "Optional: the words in the posting that ruled it out, copied exactly."},
                            },
                            "required": ["url", "why_not"],
                        },
                    },
                    "nothing_fits": {
                        "type": "boolean",
                        "description": "True if nothing you found is worth applying to. Say so rather than padding.",
                    },
                },
                "required": ["brief", "picks"],
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


# The place tests live in checks.py, because the search filter and the per-pick check must
# agree on what "in Louisville" means. `_place_filter` used to take `parts[1].upper()[:2]`
# for the state, which read "Minneapolis, Minnesota" as MI (Michigan).
_place_filter = checks.place_filter
_in_place = checks.in_place


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


MAX_PICKS = 5
MAX_RULED_OUT = int(os.environ.get("CHAT_MAX_RULED_OUT", "8"))
# Ask each chosen posting whether it is still up before showing it. Off in tests (no
# network) via tests/conftest.py; `CHAT_CHECK_LIVE=0` turns it off on a box.
CHECK_LIVE = os.environ.get("CHAT_CHECK_LIVE", "1") != "0"


def _card(row: dict, check: dict, why: str, quote: str, warning: str) -> dict:
    """One pick, as the page renders it. Every field is the store's or a check's, except
    `why` and `warning`, which are the model's words, and `quote`, which is the model's
    choice of the employer's words and has been found in the posting."""
    warnings = list(check["flags"])
    if check["asks"]:
        warnings.append(check["asks"])
    return {
        "url": row.get("url", ""),
        "title": row.get("title", ""),
        "company": checks.company_name(row),
        "location": row.get("location", ""),
        "state": row.get("state") or "",
        # The CHECKED tag, not the stored one: a "Remote" chip above a body that says
        # five days in the office is the card contradicting itself.
        "remote": check["tag"],
        "salary": checks.pay_label(row),
        "salary_min": store.annual_salary(row),
        "posted": row.get("posted", ""),
        "age_days": checks.age_days(row),
        "link": checks.link_kind(row),
        "why": why,
        "quote": quote,
        "warning": warning,
        "warnings": warnings,
    }


def _ruled(row: dict, why_not: str, by: str, quote: str = "") -> dict:
    return {"url": row.get("url", ""), "title": row.get("title", ""),
            "company": checks.company_name(row), "location": row.get("location", ""),
            "why_not": why_not, "quote": quote, "by": by}


def recommend(args: dict, state: dict | None = None) -> dict:
    """The picks, as DATA, checked before anyone sees them.

    A tool rather than a parse of the closing message, for two reasons. The front end has
    to render cards, and pulling five urls out of free text works in testing and fails on
    the first answer written slightly differently. And it is the one place the server can
    stand between the model and the person:

      1. every url must be in the pool (an invented listing is the failure the product
         cannot survive, because the person goes looking for a job that does not exist);
      2. every pick must have been READ in this conversation, and its `quote` must be in
         the posting word for word. A reason that cannot point at the employer's own
         words is an opinion ("aligns well with your B2B SaaS experience", on a hardware
         job, 2026-11-11);
      3. every pick is tested against the brief by `checks.check_pick`. A dealbreaker
         broken means the job is RULED OUT and listed with the reason, never shown as a
         pick. Silence or a mismatch of degree becomes a warning on the card;
      4. each survivor is asked whether it is still up.

    Problems the model can fix (2) are sent back ONCE per turn with nothing delivered.
    After that they are settled here by removal, because a loop that bounces forever is a
    conversation that never answers.

    `state` is the conversation's memory: which urls a tool returned and which were read.
    Without it (a direct call, a test) the read and seen rules are skipped.
    """
    brief = checks.clean_brief(args.get("brief"))
    raw_picks = [p for p in (args.get("picks") or []) if isinstance(p, dict) and p.get("url")]
    raw_out = [r for r in (args.get("ruled_out") or args.get("rejected") or [])
               if isinstance(r, dict) and r.get("url")]
    urls = list(dict.fromkeys([p["url"] for p in raw_picks] + [r["url"] for r in raw_out]))
    known = {r["url"]: r for r in store.rows_by_url(urls) if r.get("url")}
    invented = list(dict.fromkeys(p["url"] for p in raw_picks if p["url"] not in known))
    if invented:
        log.warning("recommended %d url(s) not in the store: %s", len(invented), invented)
    seen = state["seen"] if state is not None else None
    read = state["read"] if state is not None else None

    def text_of(row: dict) -> str:
        return f"{row.get('title') or ''}\n{store.plain(row.get('body') or '')}"

    # ── what the model can fix: sent back once ───────────────────────────────
    if state is not None and not state.get("bounced"):
        fix = []
        for p in raw_picks:
            row = known.get(p["url"])
            if not row:
                fix.append(f"{p['url']} is not a url any tool returned. Remove it.")
            elif p["url"] not in read:
                fix.append(f"{row.get('title')}: you have not read this posting. Call read_jobs "
                           "on it before recommending it.")
            elif not checks.quote_in(str(p.get("quote") or ""), text_of(row)):
                fix.append(f"{row.get('title')}: `quote` is not in the posting word for word. "
                           "Copy one full sentence exactly as the posting writes it.")
            elif checks.FLATTERY.search(str(p.get("why") or "")):
                fix.append(f"{row.get('title')}: `why` says nothing specific "
                           f"(“{checks.FLATTERY.search(p['why']).group(0)}”). Say what the "
                           "person would be doing in this job, in plain words.")
        if fix:
            state["bounced"] = True
            return {"delivered": 0, "not_delivered": True, "fix": fix,
                    "rejected_by_server": invented or None,
                    "note": "NOTHING has been shown to the person yet. Fix these and call "
                            "recommend again with the complete list."}

    # ── settle everything else here ──────────────────────────────────────────
    chosen: list[tuple[dict, dict]] = []      # (row, card)
    ruled: list[dict] = []
    removed: list[dict] = []                  # told back to the model
    for p in raw_picks:
        row = known.get(p["url"])
        if not row:
            continue
        if read is not None and p["url"] not in read:
            removed.append({"title": row.get("title", ""), "reason": "never read, so not shown"})
            continue
        if any(r["url"] == row["url"] or checks.same_job(r, row) for r, _ in chosen):
            continue                          # one job posted twice is one pick
        verdict = checks.check_pick(row, brief)
        if verdict["reject"]:
            ruled.append(_ruled(row, verdict["reject"], "check"))
            removed.append({"title": row.get("title", ""), "reason": verdict["reject"]})
            continue
        quote = str(p.get("quote") or "").strip()
        if not checks.quote_in(quote, text_of(row)):
            quote = ""                        # an unverified quote is never printed
        chosen.append((row, _card(row, verdict, str(p.get("why") or "").strip(), quote,
                                  str(p.get("warning") or p.get("caveat") or "").strip())))
    chosen = chosen[:MAX_PICKS]

    if CHECK_LIVE and chosen:
        dead = checks.dead_urls([r["url"] for r, _ in chosen])
        for row, _ in [c for c in chosen if c[0]["url"] in dead]:
            why_not = "The posting has been taken down since it was collected."
            ruled.append(_ruled(row, why_not, "check"))
            removed.append({"title": row.get("title", ""), "reason": why_not})
        chosen = [c for c in chosen if c[0]["url"] not in dead]

    for r in raw_out:
        row = known.get(r["url"])
        if not row or (seen is not None and r["url"] not in seen):
            continue                          # only postings this conversation looked at
        if any(c["url"] == row["url"] or checks.same_job(c, row) for c, _ in chosen):
            continue                          # never both a pick and ruled out
        if any(x["url"] == row["url"] for x in ruled):
            continue
        quote = str(r.get("quote") or "").strip()
        ruled.append(_ruled(row, str(r.get("why_not") or "").strip(), "model",
                            quote if checks.quote_in(quote, text_of(row)) else ""))

    cards = [card for _, card in chosen]
    out = {
        "delivered": len(cards),
        "picks": cards,
        "ruled_out": ruled[:MAX_RULED_OUT],
        "nothing_fits": bool(args.get("nothing_fits")) or not cards,
        "brief": checks.brief_lines(brief),
        # Told back to the MODEL so its closing message describes what is on screen.
        "rejected_by_server": invented or None,
        "removed": removed or None,
    }
    notes = [f"{len(cards)} pick(s) are now on the person's screen."]
    if removed:
        notes.append(f"{len(removed)} of yours were removed and are listed for them as ruled "
                     "out, with the reason. Do not describe a removed job as a pick.")
    if any(c["warnings"] for c in cards):
        notes.append("Some cards carry warnings the server added; they can see them.")
    if len(cards) < 3 and removed:
        notes.append("If another search could find better ones, search, read, and call "
                     "recommend again with the full list. Otherwise say plainly that few passed.")
    out["note"] = " ".join(notes)
    return out



# ═══════════════════════════════════════════════════════════════
# cost control and message hygiene
# ═══════════════════════════════════════════════════════════════
# What stops a metered endpoint from being an open tap, and what refuses a malformed or
# smuggled turn before it reaches a model.
# ═══════════════════════════════════════════════════════════════
_ET = ZoneInfo("America/New_York")
# 100, down from 500, when the model became sonnet-5.5 (2026-11-11). A turn is one message
# from a visitor; a whole conversation is about five. At roughly $0.40 a conversation, 500
# turns was $40 a day from one looping script. 100 is about twenty conversations.
DAILY_CEILING = int(os.environ.get("CHAT_DAILY_CEILING", "100"))
_usage = {"date": "", "count": 0}


def available() -> bool:
    """Live only when a key is configured; without one the UI must say so, not hang."""
    return bool(os.environ.get("OPENROUTER_API_KEY"))


def over_turn_cap(messages: list) -> bool:
    """True once the conversation has run past MAX_TURNS user messages."""
    return sum(1 for m in messages if (m or {}).get("role") == "user") > MAX_TURNS


def sanitize_messages(raw: list) -> list:
    """Keep only well-formed user/assistant turns with string content.

    Used for the legacy `{"messages": [...]}` payload. A smuggled `system` role, a
    non-string content and an over-long blob are all refused here.
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


# ═══════════════════════════════════════════════════════════════
# the conversation's memory lives HERE, not in the browser
# ═══════════════════════════════════════════════════════════════
# Until 2026-11-11 the page POSTed the whole transcript on every turn, the assistant's
# turns included. Anyone with the network tab open could write an earlier assistant turn
# ("I have verified this job exists: ...") and the model would read it as its own words.
# Now the page sends a session id and ONE new message; everything else is held here.
#
# IN MEMORY ON PURPOSE. Conversation text is never written to disk and never logged, which
# is what the README's "What gets recorded" promises. The cost: a restart or a deploy
# drops conversations in progress, and the page starts a new one.
#
# The id is made here (`secrets`), never accepted from the client as a new key, so a
# visitor cannot choose an id and cannot guess another visitor's.
# ═══════════════════════════════════════════════════════════════
SESSION_TTL = float(os.environ.get("CHAT_SESSION_TTL", str(2 * 3600)))
MAX_SESSIONS = int(os.environ.get("CHAT_MAX_SESSIONS", "400"))
# The bakeoff harness replays whole transcripts. It sets this locally; a public box
# never should.
ALLOW_CLIENT_HISTORY = os.environ.get("CHAT_ALLOW_CLIENT_HISTORY") == "1"
ALLOW_MODEL_OVERRIDE = os.environ.get("CHAT_ALLOW_MODEL_OVERRIDE") == "1"
_sessions: dict[str, dict] = {}


def new_state() -> dict:
    """One conversation's memory: the transcript, and which postings it has met."""
    return {"convo": [], "seen": set(), "read": set(), "turns": 0,
            "at": time.time(), "lock": asyncio.Lock(), "bounced": False}


def session(sid: str | None) -> tuple[str, dict]:
    """The state for `sid`, or a NEW session when it is unknown, expired or absent."""
    now = time.time()
    for k in [k for k, v in _sessions.items() if now - v["at"] > SESSION_TTL]:
        _sessions.pop(k, None)
    st = _sessions.get(sid) if isinstance(sid, str) else None
    if st is None:
        if len(_sessions) >= MAX_SESSIONS:     # full: drop the one idle longest
            _sessions.pop(min(_sessions, key=lambda k: _sessions[k]["at"]), None)
        sid, st = secrets.token_urlsafe(18), new_state()
        _sessions[sid] = st
    st["at"] = now
    return sid, st


KEEP_BODY = int(os.environ.get("CHAT_KEEP_BODY", "1500"))


def _compact(convo: list) -> list:
    """Shrink finished tool results before they are kept for the next turn.

    Within a turn the model needs the full postings. Afterwards, re-sending 60,000
    characters of bodies with every follow-up question is paying again for text it has
    already judged. Snippets go and bodies are cut to KEEP_BODY; `read_jobs` still works
    if it wants one back.
    """
    out = []
    for m in convo:
        if m.get("role") != "tool":
            out.append(m)
            continue
        try:
            d = json.loads(m["content"])
            for j in d.get("jobs") or []:
                j.pop("snippet", None)
                if len(j.get("body") or "") > KEEP_BODY:
                    j["body"] = j["body"][:KEEP_BODY] + " …[trimmed; read_jobs again for the rest]"
            out.append({**m, "content": json.dumps(d)})
        except (ValueError, AttributeError, TypeError):
            out.append({**m, "content": str(m.get("content"))[:2000]})
    return out


# "Just search." Six questions later it was still asking (2026-11-11, twice). The prompt
# has always said "go when told". Now the server notices the phrase itself, says so in the
# transcript, and if the model answers with another question anyway, throws that answer
# away and tells it once more. The person never sees the question.
#
# NOT `tool_choice`. That was the first design and the obvious one, and Anthropic refuses
# it: `tool_choice: type "tool" and "any" are not supported for this model` (measured on
# claude-sonnet-5.5, 2026-11-11). A retry in the transcript works on every model.
_GO = re.compile(
    r"\b(?:just search|search now|go ahead(?: and search)?|just show me|show me (?:the )?(?:jobs|results)"
    r"|start (?:the )?search(?:ing)?|run (?:the|a) search|skip the questions|no more questions"
    r"|that'?s everything|that is everything|that'?s all|busca ya|solo busca|busca ahora)\b", re.I)
# ...but only when there is something to search FOR. A bare "search now" as the first
# message carries nothing, and forcing it would make the model invent a job title.
GO_MIN_CHARS = 60
_GO_NOTE = ("(From jobfitr, not the person: they have told you to search now. Call "
            "`search_jobs` in this reply with what you already know. Do not ask another "
            "question and do not ask them to confirm.)")
_GO_AGAIN = ("You answered with a question. They told you to search. Call `search_jobs` "
             "now with what you have; anything you still do not know, leave out of the search.")


def told_to_search(convo: list) -> bool:
    users = [m.get("content") or "" for m in convo
             if m.get("role") == "user" and isinstance(m.get("content"), str)]
    if not users or not _GO.search(users[-1]):
        return False
    return sum(len(_GO.sub("", u)) for u in users) >= GO_MIN_CHARS


# ═══════════════════════════════════════════════════════════════
# talking to the model
# ═══════════════════════════════════════════════════════════════
STREAM = os.environ.get("CHAT_STREAM", "1") != "0"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://jobfitr.app",
        "X-Title": "jobfitr",
    }


async def _call(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.post(OPENROUTER_URL, headers=_headers(), json=payload)
        r.raise_for_status()
        return r.json()


def _fold(acc: dict, chunk: dict) -> str:
    """Fold one streamed chunk into the message being assembled; return its new text.

    A streamed tool call arrives in pieces keyed by `index`: the id and name once, the
    arguments as fragments of a JSON string. Pure, so the assembly is testable without a
    network.
    """
    if chunk.get("usage"):
        acc["usage"] = chunk["usage"]
    if chunk.get("model"):
        acc["model"] = chunk["model"]
    delta = ((chunk.get("choices") or [{}])[0] or {}).get("delta") or {}
    text = delta.get("content") or ""
    acc["content"] += text
    for tc in delta.get("tool_calls") or []:
        slot = acc["tool_calls"].setdefault(
            tc.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
        if tc.get("id"):
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] = fn["name"]
        slot["function"]["arguments"] += fn.get("arguments") or ""
    return text


def _folded(acc: dict) -> dict:
    """The assembled stream, in the shape `_call` returns."""
    msg: dict = {"role": "assistant", "content": acc["content"] or None}
    if acc["tool_calls"]:
        msg["tool_calls"] = [acc["tool_calls"][i] for i in sorted(acc["tool_calls"])]
    return {"choices": [{"message": msg}], "model": acc["model"], "usage": acc["usage"] or {}}


async def _model(payload: dict):
    """One model call. Yields ("text", fragment) as words arrive, then ("final", data).

    Streamed because a question turn used to be 10-17 s of nothing. With the words
    arriving as they are written the first ones are on screen in about a second.
    `STREAM` off (tests, or CHAT_STREAM=0) falls back to one blocking `_call`.
    """
    if not STREAM:
        yield ("final", await _call(payload))
        return
    acc = {"content": "", "tool_calls": {}, "model": None, "usage": None}
    body = {**payload, "stream": True, "stream_options": {"include_usage": True}}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        async with client.stream("POST", OPENROUTER_URL, headers=_headers(), json=body) as r:
            if r.status_code >= 400:
                await r.aread()
                r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue                  # keep-alive comments, blank lines
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    chunk = json.loads(raw)
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise RuntimeError(str((chunk["error"] or {}).get("message") or chunk["error"]))
                text = _fold(acc, chunk)
                if text:
                    yield ("text", text)
    yield ("final", _folded(acc))


def _cached(convo: list, model_id: str) -> list:
    """Mark cache breakpoints for Anthropic models; everyone else gets `convo` untouched.

    The loop re-sends the whole transcript on every call, and by the last call that is
    the system prompt plus every search result and every posting read. Anthropic bills a
    cached read at a tenth of the input price, so two breakpoints (the system prompt, and
    the newest tool result or user message) make each call pay full price only for what
    is new.
    """
    if not model_id.startswith("anthropic/"):
        return convo

    def mark(m: dict) -> dict:
        return {**m, "content": [{"type": "text", "text": m["content"],
                                  "cache_control": {"type": "ephemeral"}}]}

    out = list(convo)
    if isinstance(out[0].get("content"), str):
        out[0] = mark(out[0])
    for i in range(len(out) - 1, 0, -1):
        if out[i].get("role") in ("user", "tool") and isinstance(out[i].get("content"), str) and out[i]["content"]:
            out[i] = mark(out[i])
            break
    return out


def resolve_model(name: str | None) -> str:
    """The model for this request. A caller may choose one only where that is allowed.

    "model" is the field that decides what a request COSTS. Until 2026-11-11 any visitor
    could name any bench entry in the payload, including the dearest. Now the override
    needs CHAT_ALLOW_MODEL_OVERRIDE=1, which the bakeoff sets locally and the box does not.
    """
    if ALLOW_MODEL_OVERRIDE and name and name in MODELS:
        return MODELS[name]["id"]
    return DEFAULT_MODEL


def model_label(model_id: str) -> str:
    for m in MODELS.values():
        if m["id"] == model_id:
            return m.get("label") or model_id
    return model_id


def _run_tool(name: str, args: dict, state: dict) -> dict:
    """Looked up by NAME at call time, so a test that replaces `chat.search_jobs` is
    actually exercised (a dict built at import kept pointing at the original)."""
    if name == "recommend":
        return recommend(args, state)
    if name in ("search_jobs", "read_jobs"):
        return globals()[name](args)
    return {"error": f"no tool {name}"}


async def turn(messages: list, model: str | None = None, state: dict | None = None) -> dict:
    """Run a whole turn and return the final dict.

    A thin drain over `turn_events()`, so /api/chat, the deploy gate and the bakeoff
    harness read one dict while the page reads the stream.
    """
    final: dict = {}
    async for ev in turn_events(messages, model, state):
        if ev.get("type") == "done":
            final = ev["result"]
    return final


async def turn_events(messages: list, model: str | None = None, state: dict | None = None):
    """Run the model until it produces its answer, yielding an EVENT at each step.

    `messages` is what is NEW this turn (one user message from the page). `state` is the
    conversation's memory; None means a one-off conversation whose whole transcript is
    `messages` (the deploy gate, the tests).

    Events are dicts with a `type`:
      status      the turn has started; sent before anything slow happens
      text        a fragment of the model's words, as they are written
      reset       throw away the text so far (it turned out to precede a tool call)
      tool_start  a tool is about to run, with what it is searching for
      tool        that tool returned
      picks       the checked picks and the ruled-out list, the moment they exist
      done        exactly one, carrying the same dict `turn()` returns
    Anything consuming this must ignore unknown types rather than assume the set is closed.

    Tools run in a worker thread. They used to run inline, and they are synchronous (a
    live fetch can take 20 s), so one visitor's search froze every other request on the
    process. That, not the model, was most of the "10-17 s for a plain question" measured
    on 2026-11-11 while four testers ran at once.

    Bounded twice over: MAX_TOOL_CALLS across the loop and MAX_TURNS on the transcript.
    """
    model_id = resolve_model(model)
    if state is None:
        state = new_state()
    state["bounced"] = False
    convo = [{"role": "system", "content": system_prompt()}, *state["convo"], *messages]
    trace, calls = [], 0
    picks: list = []
    ruled_out: list = []
    brief_shown: list = []
    nothing_fits = False
    delivered = False
    nudged = False
    fabricated = 0
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
    t0 = time.perf_counter()
    force = told_to_search(convo)
    if force:
        convo.append({"role": "user", "content": _GO_NOTE})
    yield {"type": "status", "text": "thinking"}

    def _log_delivery() -> None:
        """Shape only — counts, never content. See searchlog.record_delivery.

        Called on BOTH exits, including the budget-exhausted one: a conversation that ran
        out of tool calls without delivering is exactly the failure worth counting.
        """
        searchlog.record_delivery(
            turns=sum(1 for m in convo if m.get("role") == "user"),
            searches=sum(1 for t in trace if t["tool"] == "search_jobs"),
            reads=sum(1 for t in trace if t["tool"] == "read_jobs"),
            picks=len(picks),
            rejected=len(ruled_out),
            fabricated=fabricated,
            nothing_fits=nothing_fits,
            model=model_id,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

    def _result(reply: str, model_name, error: str | None = None) -> dict:
        searches = sum(1 for t in trace if t["tool"] == "search_jobs")
        out = {
            "reply": reply, "trace": trace, "tool_calls": calls, "model": model_name,
            "picks": picks, "ruled_out": ruled_out, "rejected": ruled_out,
            "nothing_fits": nothing_fits, "brief": brief_shown,
            # `fabricated` is in the RESPONSE, not only in searchlog, because searchlog is
            # off unless JOBFITR_SEARCH_LOG is set.
            "fabricated": fabricated, "usage": usage,
            # THE RECEIPT. The model name, the cost and the trace were already visible in
            # the network tab; printing them on the page turns an exposure into a
            # statement. Only a turn that searched gets one.
            "receipt": {
                "searches": searches,
                "read": len({u for t in trace if t["tool"] == "read_jobs"
                             for u in (t["args"].get("urls") or [])}),
                "seconds": round(time.perf_counter() - t0),
                "cost": round(usage["cost"], 4),
                "model": model_label(model_id),
            } if searches else None,
        }
        if error:
            out["error"] = error
        return out

    while calls <= MAX_TOOL_CALLS:
        data: dict = {}
        streamed = False
        # ONE RETRY on a transport fault or an upstream 429/5xx. The first full run on
        # sonnet (2026-11-11) searched four times, read thirteen postings, delivered, and
        # then lost the connection during the closing sentence (`httpx.ReadError`): the
        # person saw "something went wrong" under a finished answer. A turn costs a
        # minute and real money by then; one dropped socket must not void it.
        for attempt in (1, 2):
            try:
                async for kind, val in _model({
                    "model": model_id, "messages": _cached(convo, model_id), "tools": TOOLS,
                    "tool_choice": "auto",
                }):
                    if kind == "text":
                        streamed = True
                        yield {"type": "text", "delta": val}
                    else:
                        data = val
                break
            except (httpx.TransportError, httpx.HTTPStatusError, RuntimeError) as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                retryable = code is None or code == 429 or code >= 500
                log.warning("model call failed (attempt %d): %s: %s", attempt, type(e).__name__, e)
                if attempt == 2 or not retryable:
                    # Keep what happened so far: the picks are already on screen, and a
                    # follow-up should not find the conversation has forgotten them.
                    state["convo"] = _compact(convo[1:])
                    raise
                if streamed:
                    streamed = False
                    yield {"type": "reset"}
                await asyncio.sleep(1.0)
        # A conversation is many requests; the caller wants the total for the whole loop.
        u = data.get("usage") or {}
        usage["prompt_tokens"] += u.get("prompt_tokens") or 0
        usage["completion_tokens"] += u.get("completion_tokens") or 0
        usage["cost"] += float(u.get("cost") or 0.0)
        msg = data["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls and force:
            # Told to search, and it asked something instead. Once.
            force = False
            if streamed:
                yield {"type": "reset"}
            convo.append({"role": "assistant", "content": msg.get("content") or "(no text)"})
            convo.append({"role": "user", "content": _GO_AGAIN})
            continue
        force = False
        if not tool_calls:
            # THE NUDGE, and it is load-bearing rather than defensive. Measured: a model
            # that has searched and read will write five jobs into its prose and never
            # call `recommend` — the answer reads fine and the screen shows no cards. So
            # if it tries to finish after searching without delivering, it is told once,
            # in the transcript, which is the only place a model reliably notices anything.
            searched = any(t["tool"] == "search_jobs" for t in trace)
            if searched and not delivered and not nudged:
                nudged = True
                if streamed:
                    yield {"type": "reset"}
                convo.append({"role": "assistant", "content": msg.get("content") or "(no text)"})
                convo.append({"role": "user", "content":
                    "You have not delivered with `recommend`, so the person's screen is empty — "
                    "whatever you wrote is invisible to them. Call `recommend` now with the "
                    "brief, the jobs you chose (url, why, quote, warning) and the ones worth "
                    "naming as ruled out, using only urls a tool returned. If nothing is "
                    "worth recommending, call it with no picks and nothing_fits true. Then "
                    "write your closing message."})
                continue
            reply = msg.get("content") or ""
            if delivered:
                _log_delivery()
            # Remember the turn. Only now, at the end: a turn abandoned halfway (the
            # person closed the tab) leaves the conversation exactly as it was.
            state["convo"] = _compact(convo[1:]) + [{"role": "assistant", "content": reply or "(no text)"}]
            yield {"type": "done", "result": _result(reply, data.get("model"))}
            return
        if streamed:
            yield {"type": "reset"}
        convo.append({"role": "assistant", "content": msg.get("content"), "tool_calls": tool_calls})
        for tc in tool_calls:
            calls += 1
            name = tc["function"]["name"]
            args: dict = {}
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
                if not isinstance(args, dict):
                    args = {}
                yield {"type": "tool_start", "tool": name, "why": args.get("why", ""),
                       "titles": (args.get("titles") or [])[:6] if isinstance(args.get("titles"), list) else [],
                       "count": len(args.get("urls") or args.get("picks") or []), "n": calls}
                result = await asyncio.to_thread(_run_tool, name, args, state)
            except Exception as e:  # a tool fault is a RESULT the model can react to,
                log.warning("chat tool %s failed: %s", name, e)  # never a dead conversation
                result = {"error": f"{type(e).__name__}: {e}"}
            fresh = None
            jobs = result.get("jobs") if isinstance(result.get("jobs"), list) else []
            got = [j.get("url") for j in jobs if isinstance(j, dict) and j.get("url")]
            if name == "search_jobs":
                # "37 found" on two different searches read as a stuck counter. How many
                # of them are NEW to this conversation is the number that shows a second
                # search did something.
                fresh = sum(1 for g in got if g not in state["seen"])
                state["seen"].update(got)
            elif name == "read_jobs":
                state["seen"].update(got)
                state["read"].update(got)
            bounced = bool(result.get("not_delivered"))
            if name == "recommend":
                fabricated += len(result.get("rejected_by_server") or [])
                if not bounced and "picks" in result:
                    delivered = True
                    picks = result.get("picks") or []
                    ruled_out = result.get("ruled_out") or []
                    nothing_fits = bool(result.get("nothing_fits"))
                    brief_shown = result.get("brief") or []
            step = {"tool": name, "args": args,
                    "returned": (result.get("returned") if "returned" in result
                                 else result.get("read") if "read" in result
                                 else result.get("delivered", 0)),
                    "why": args.get("why", "")}
            trace.append(step)
            # THE LIVE EVENT, emitted the moment the tool returns. It carries `titles`
            # deliberately: seeing the model pivot to "1st Shift Supervisor" after someone
            # says they want off nights is the moment that shows the reasoning is real.
            ev = {"type": "tool", "tool": name, "why": step["why"],
                  "returned": step["returned"],
                  "titles": (args.get("titles") or [])[:6] if isinstance(args.get("titles"), list) else [],
                  "location": args.get("location") or "",
                  "remote_only": bool(args.get("remote_only")),
                  "n": calls}
            if fresh is not None:
                ev["new"] = fresh
            if name == "recommend":
                ev["sent_back"] = len(result.get("fix") or []) if bounced else 0
                ev["removed"] = len(result.get("removed") or [])
                ev["brief"] = result.get("brief") or []
            yield ev
            if name == "recommend" and not bounced and "picks" in result:
                yield {"type": "picks", "picks": picks, "ruled_out": ruled_out,
                       "nothing_fits": nothing_fits}
            convo.append({"role": "tool", "tool_call_id": tc["id"],
                          "content": json.dumps(result)[:120000]})
    _log_delivery()
    state["convo"] = _compact(convo[1:])
    yield {"type": "done", "result": _result("", None, "tool_budget_exhausted")}
