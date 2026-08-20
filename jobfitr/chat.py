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
import os

import httpx
from datetime import datetime
from zoneinfo import ZoneInfo

from . import prompts, semantic, store

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
# NOTHING HERE IS MEASURED FOR QUALITY. The retrieval work of 2026-08-19 established that
# the interview's output (its probes and its titles) is what drives results, and that probe
# QUALITY is the lever — but no model has been compared against another on it. The bench
# exists so that comparison is a one-line change instead of a refactor.
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
    "gemini-3.5-flash":  {"id": "google/gemini-3.5-flash",     "ctx": 1_048_576, "session_usd": 0.279},
    # NOTE grok-4.3 carries TWICE the context of grok-4.6 at 60% of the cost, and grok-4.20
    # carries four times it for the same price. If the reason for reaching for Grok is
    # capability rather than the specific 4.6 build, 4.3 is the better entry point.
}

# ↓↓↓ THE ONE LINE TO CHANGE ↓↓↓
ACTIVE_MODEL = "gemini-2.5-flash"

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
        "remote": c.get("remote") or "",
        "salary": c.get("salary", ""),
        "salary_min": store.annual_salary(c),
        "posted": c.get("posted", ""),
        "snippet": body[:SHALLOW_CHARS],
    }


def search_jobs(args: dict) -> dict:
    """The hybrid arm. Pre-filter, then BOTH arms over the filtered pool, fused by RRF."""
    from .config_builder import config_from_dict

    titles = [t for t in (args.get("titles") or []) if str(t).strip()]
    probes = [p for p in (args.get("probes") or []) if str(p).strip()]
    k = max(1, min(int(args.get("k") or 40), 120))
    cfg = config_from_dict(
        {"titles": titles, "location": args.get("location") or "",
         "remote_only": bool(args.get("remote_only"))}, [])
    age = args.get("max_age_days")
    rows = store.bm25_candidates(titles) if titles else []
    # Hard filters BEFORE retrieval depth is spent. See semantic.hybrid.
    keep = []
    floor = args.get("salary_floor")
    for c in rows:
        if cfg.remote_only and (c.get("remote") or "") in ("onsite", "hybrid"):
            continue
        if age and (c.get("posted") or ""):
            from job_radar.util import age_int
            a = age_int(c.get("posted"))
            if a is not None and a > int(age):
                continue
        if floor:
            s = store.annual_salary(c)
            if s is not None and s < int(floor):   # unstated salary is KEPT — see SCHEMA_NOTE
                continue
        keep.append(c)
    by_url = {c["url"]: c for c in keep if c.get("url")}
    fused = semantic.hybrid(list(by_url), probes, k=k,
                            per_company={u: (r.get("company") or "") for u, r in by_url.items()})
    extra = [u for u in fused if u not in by_url]
    if extra:
        by_url.update({c["url"]: c for c in store.rows_by_url(extra) if c.get("url")})
    out = [_shallow(by_url[u]) for u in fused if u in by_url][:k]
    return {"searched": {"titles": titles, "probes": probes, "why": args.get("why", "")},
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


async def turn(messages: list) -> dict:
    """Run the model until it produces text for the user, executing its tool calls.

    Bounded twice over: MAX_TOOL_CALLS across the loop and MAX_TURNS on the transcript. An
    agent that can choose its own searches can also choose them forever, and this is the
    only thing standing between a curious model and the token bill.
    """
    convo = [{"role": "system", "content": system_prompt()}, *messages]
    trace, calls = [], 0
    picks: list = []
    rejected: list = []
    nothing_fits = False
    nudged = False
    while calls <= MAX_TOOL_CALLS:
        data = await _call({
            "model": DEFAULT_MODEL,
            "messages": convo, "tools": TOOLS, "tool_choice": "auto",
        })
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
            return {"reply": msg.get("content") or "", "trace": trace,
                    "tool_calls": calls, "model": data.get("model"),
                    "picks": picks, "rejected": rejected, "nothing_fits": nothing_fits}
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
                picks = result.get("picks") or picks
                rejected = result.get("rejected") or rejected
                nothing_fits = result.get("nothing_fits") or nothing_fits
            trace.append({"tool": name, "args": args,
                          "returned": (result.get("returned") if "returned" in result
                                       else result.get("read") if "read" in result
                                       else result.get("delivered", 0)),
                          "why": args.get("why", "")})
            convo.append({"role": "tool", "tool_call_id": tc["id"],
                          "content": json.dumps(result)[:120000]})
    return {"reply": "", "trace": trace, "tool_calls": calls,
            "error": "tool_budget_exhausted"}
