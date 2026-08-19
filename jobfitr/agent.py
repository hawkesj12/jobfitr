"""The metered agentic path: a model that drives its own search loop.

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
paragraphs, and said so as a flaw. read_jobs serves the whole posting with its sections
labelled, so responsibilities and requirements are separable from benefits and EEO.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

from . import prompts
from . import sections as sectionsmod
from . import semantic, store

log = logging.getLogger("jobfitr.agent")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "google/gemini-2.5-flash")
MAX_TOOL_CALLS = int(os.environ.get("AGENT_MAX_TOOL_CALLS", "12"))
MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", "24"))
REQUEST_TIMEOUT = float(os.environ.get("AGENT_TIMEOUT", "90"))
SHALLOW_CHARS = int(os.environ.get("AGENT_SHALLOW_CHARS", "320"))
DEEP_CHARS = int(os.environ.get("AGENT_DEEP_CHARS", "6000"))
MAX_READ = int(os.environ.get("AGENT_MAX_READ", "25"))

# ── what the model is told about the corpus ──────────────────────────────────
# Fill rates are MEASURED over the live pool (65,391 rows, 2026-08-19) and they are in the
# prompt for one reason: a model that does not know `seniority` is 28% filled will filter on
# it and silently delete 72% of the corpus. NULL means "the source never said", never "no".
SCHEMA_NOTE = prompts.load("agent_schema")

TOOLS_NOTE = prompts.load("agent_tools")

SYSTEM_PROMPT = prompts.render("agent_system", tools=TOOLS_NOTE, schema=SCHEMA_NOTE)

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
            "name": "read_jobs",
            "description": "Read the FULL posting for specific urls, with sections labelled. Use before judging fit.",
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
    body = sectionsmod._detag(c.get("body") or "")
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
    """Full postings, sections labelled — the handicap the experiment ran under, removed."""
    urls = [u for u in (args.get("urls") or []) if str(u).strip()][:MAX_READ]
    out = []
    for c in store.rows_by_url(urls):
        parts = sectionsmod.split_sections(c.get("body") or "")
        labelled = {}
        for kind, header, text in parts:
            if not text:
                continue
            key = kind or "other"
            labelled.setdefault(key, []).append((header + ": " if header else "") + text)
        out.append({
            "url": c.get("url", ""), "title": c.get("title", ""),
            "company": c.get("company", ""), "location": c.get("location", ""),
            "remote": c.get("remote") or "", "salary": c.get("salary", ""),
            "posted": c.get("posted", ""), "team": c.get("team") or "",
            "sections": {k: " ".join(v)[:DEEP_CHARS] for k, v in labelled.items()},
        })
    return {"read": len(out), "jobs": out}


DISPATCH = {"search_jobs": search_jobs, "read_jobs": read_jobs}


def available() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY"))


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
    convo = [{"role": "system", "content": SYSTEM_PROMPT}, *messages]
    trace, calls = [], 0
    while calls <= MAX_TOOL_CALLS:
        data = await _call({
            "model": os.environ.get("AGENT_MODEL", DEFAULT_MODEL),
            "messages": convo, "tools": TOOLS, "tool_choice": "auto",
        })
        msg = data["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return {"reply": msg.get("content") or "", "trace": trace,
                    "tool_calls": calls, "model": data.get("model")}
        convo.append(msg)
        for tc in tool_calls:
            calls += 1
            name = tc["function"]["name"]
            args: dict = {}
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
                result = DISPATCH[name](args) if name in DISPATCH else {"error": f"no tool {name}"}
            except Exception as e:  # a tool fault is a RESULT the model can react to,
                log.warning("agent tool %s failed: %s", name, e)  # never a dead conversation
                result = {"error": f"{type(e).__name__}: {e}"}
            trace.append({"tool": name, "args": args,
                          "returned": result.get("returned") or result.get("read") or 0,
                          "why": args.get("why", "")})
            convo.append({"role": "tool", "tool_call_id": tc["id"],
                          "content": json.dumps(result)[:120000]})
    return {"reply": "", "trace": trace, "tool_calls": calls,
            "error": "tool_budget_exhausted"}
