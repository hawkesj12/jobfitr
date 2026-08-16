"""The web API — a live-fetch-on-search hybrid over a SQLite/FTS5 store.

A search either serves a FRESH cache (a title|location fetched < TTL ago → zero API
calls) or does a bounded LIVE fetch (Adzuna + USAJOBS, ~1-2s, single-flighted so
concurrent identical searches share one upstream call), then ranks the store with
FTS5 BM25 + a personalized rerank. The daily-fetch ceiling load-sheds to the cache
with a `degraded` banner, so the free quota can never run away.

score_jobs is a sync def so FastAPI runs it in a threadpool — the blocking live
fetch never stalls the event loop, and live.coalesced_fetch (threading) coalesces.
The only metered LLM path is /api/chat.
"""

from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from job_radar.util import age_int
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import chat as chatmod
from . import live, searchlog, snapshot, store
from .config_builder import _clean_list, config_from_dict, search_inputs
from .match import has_term, norm_key, term_hits, title_score
from .snapshot import load_dotenv

_ET = ZoneInfo("America/New_York")

# Local dev: pull ./.env into os.environ so OPENROUTER_API_KEY (and the harvest keys)
# are present when the server is started directly (python -m jobfitr.server). In
# production these come from systemd's EnvironmentFile; load_dotenv only fills vars
# NOT already set, so it never overrides the deployed secrets — a safe no-op there.
load_dotenv()

log = logging.getLogger("jobfitr")

# Build the SQLite store schema + pull in the current harvest snapshot.
store.init()

# How often a running slot re-checks the shared jobs.json for a newer harvest.
# The harvest is nightly, so this is a cheap stat() ~96x/day; the import itself only
# runs when the mtime actually moved. 0 disables the poller (tests, one-off runs).
SNAPSHOT_SYNC_SECONDS = int(os.environ.get("JOBFITR_SNAPSHOT_SYNC_SECONDS", "900"))
_sync_stop = threading.Event()


def _snapshot_sync_loop() -> None:
    """Re-import the harvest snapshot on an interval, off the request path.

    Without this a slot only picked up new jobs on restart: the nightly harvest
    rewrites the SHARED jobs.json, but each slot serves its OWN SQLite store. Doing
    it on a background thread (not lazily in a request) means no user ever eats the
    multi-second import.
    """
    while not _sync_stop.wait(SNAPSHOT_SYNC_SECONDS):
        try:
            n = store.sync_snapshot()
            if n:
                log.info(
                    "snapshot sync: imported %d jobs (pool %d)", n, store.pool_size()
                )
        except Exception:  # noqa: BLE001 — a sync failure must never kill the server
            log.exception("snapshot sync failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    thread = None
    if SNAPSHOT_SYNC_SECONDS > 0:
        thread = threading.Thread(
            target=_snapshot_sync_loop, name="jobfitr-snapshot-sync", daemon=True
        )
        thread.start()
    try:
        yield
    finally:
        _sync_stop.set()
        if thread:
            thread.join(timeout=5)


DEFAULT_LIMIT = 100
MAX_LIMIT = 500
SNIPPET_CHARS = 240
DESC_CHARS = 1200

# ── the scoreboard ────────────────────────────────────────────────────────────
# A listing's score is a plain integer a person can read off the card and check:
#
#     points = title tier  +  Σ boost points  −  penalties (30 title/company, 15 body)
#
# It replaced `bm25 + boost_bonus − penalties`, a float with two defects. First it was
# unreadable — nobody could say why a job scored 4.31. Second, and worse, the boost half
# was a FRACTION of the boosts the user gave, so a listing matching 3 of 14 boosts scored
# LOWER than one matching 0 of 0. The interview tells people to name as many skills as
# they can think of; the scorer then punished everyone who did.
#
# A scoreboard has neither problem. Nothing is earned except by evidence, and evidence
# only ever adds — so naming another skill can never cost you.
#
# BM25 IS NOT PART OF THE SCORE — BUT IT IS THE TIE-BREAK, AND THAT IS SPEC, NOT TRIVIA.
# It no longer contributes to what a job is WORTH. It still ORDERS the candidates coming
# out of FTS5, and `_rank` finishes with a STABLE sort on points, so two listings on the
# same score keep the order retrieval gave them — which is BM25 order.
#
# Ties are the normal case, not the edge: 23 of the top 50 for a one-word query land on
# exactly the same number, and 56% of a typical board sits at the flat related-title 30.
# Measured, shuffling the candidate order moves 51-65 of the delivered top 200.
#
# So a reader hand-computing a golden gets the NUMBER right and cannot predict the
# ORDER, and `store.py`'s note that `d["bm25"]` is read by nothing is true of the VALUE
# and misleading about the ORDERING. Deleting `ORDER BY rank` to save the sort is a
# ranking change wearing an optimisation's clothes; if it ever goes, it has to be
# replaced by an explicit deterministic tie-break, not by nothing.
BOOST_DECAY = (8, 6, 4, 2)  # points for the 1st..4th occurrence of one term → 20 max
# Penalties read the BODY as well as the title and company, weighted by where the tell
# appears. An earlier version scored title+company only, on the measured grounds that
# "senior" is 18.8% of bodies and "sales" 13.1% — but those are EXCLUDE terms, which hide
# a listing, not rank_down terms, which sink it. Measured against the rank_down terms the
# 57 test users actually use, the worst is "our client" at 4.1% of bodies and the most
# popular ("staffing", 14 users) is 0.5%.
#
# And three of the clearest agency tells — "our client", "consultancy", "staff
# augmentation" — appear in ZERO titles or company names. A title-only rule caught none
# of them, which is precisely the listing type users are asking to sink.
#
# One hit per term, strongest wins: a term in the title/company does NOT also collect the
# body penalty.
PENALTY_TITLE = 30  # the tell is in the title or the employer's own name
PENALTY_BODY = 15  # the tell is buried in the description

# ── the words that mean two things ───────────────────────────────────────────
#
# Some avoid-terms are a whole signal on their own — "our client", "staff augmentation",
# "talent solutions", "c2c" — because nobody writes those unless they are placing you at
# somebody else's company. Others are ordinary English that happens to collide with the
# agency vocabulary, and penalising the bare word is almost always wrong.
#
# The 1.5 audit caught this: five independent readers scored 250 listings by hand, and
# the single largest cluster of disagreements was the code subtracting points for a word
# the reader could see meant something else. Measured afterwards on the 39,597-row
# corpus, the bare words are overwhelmingly innocent:
#
#   "agency"      860 rows.  17 clearly a firm, 94 clearly prose ("we hire people with
#                 HIGH AGENCY"), and in company names it is 420 listings of FEDERAL
#                 GOVERNMENT — Defense Logistics Agency, Farm Service Agency, "Department
#                 of State - Agency Wide". A user avoiding "agency" was docking the
#                 entire federal government 30 points.
#   "recruiting"  1,094 rows. 12 clearly a firm. The rest is boilerplate ("if you suspect
#                 a RECRUITING scam"), a company's own internal team, a closing date, or
#                 — for an HR role — the duties of the job being advertised.
#
# So a bare ambiguous word now only counts when something next to it makes it an
# EMPLOYER TYPE. Multi-word terms are untouched: they were never ambiguous, and they are
# what actually catches a staffing shop ("our client is seeking…").
#
# The qualifier applies to the BODY for every term below. It applies to the COMPANY NAME
# for "agency" ONLY, and that asymmetry is measured, not taste:
#
#   companies whose name contains "staffing" — 38 listings, every one a real staffing
#     shop (Kforce Technology Staffing, KE Staffing, Bravo Global Staffing). Naming
#     yourself that IS the disclosure, so the bare word in a company name is good signal.
#   companies whose name contains "recruiting" — 7 listings, all genuine.
#   companies whose name contains "agency" — 420 listings, and they are the FEDERAL
#     GOVERNMENT: Defense Logistics Agency, Farm Service Agency, Defense Commissary
#     Agency. Nothing about that name discloses a staffing arrangement.
#
# So "Bravo Global Staffing" is still caught by its name, and the Defense Logistics
# Agency is not — which is the whole point.
#
# "our client" was ADDED on 2026-08-03, and it is the largest correction of the set. It
# was left out of the first pass on the reasoning that multi-word phrases are inherently
# unambiguous — which was wrong, and the measurement is lopsided: of 1,619 bodies using
# it, 753 have the client as the party being SERVED ("help our clients transform complex
# data", "empower our clients' success", "the best products for our clients") against 39
# where the client is the party HIRING ("our client is a leading govtech"). 19 to 1.
#
# That matters more than anything else here: "our client" was firing on 3,660 pairs, by
# far the biggest penalty term in the corpus, and the 1.5 audit had already flagged it —
# four readers independently docked nothing where the code took 15 points off an ordinary
# B2B company for saying it serves customers.
#
# The qualifier keys on WHO IS HIRING. A staffing shop writes "our client is seeking" or
# "on behalf of our client"; a consultancy writes "for our clients".
#
# "consulting" was in this list and came OUT, because the same measurement that put the
# others in kept it out: 931 rows containing it, 308 in the firm sense ("global leader in
# technology and management consulting") against 18 in the prose sense ("consulting with
# stakeholders") — 17:1. It is simply not an ambiguous word, and qualifying it created a
# false NEGATIVE that the goldens caught: a real consultancy stopped being penalised.
# The mirror case is "recruiter", which stays: 280 rows, 4 firm against 137 prose, almost
# all of them "our recruiter will reach out to you".
#
# Deliberately four entries. If this needs to grow much past that, the rule is wrong and
# dropping body penalties outright is the better trade.
QUALIFIED_PENALTY = {
    "agency": r"\b(staffing|recruiting|recruitment|employment|temp|talent)[\s-]+agenc(y|ies)\b",
    "recruiting": r"\brecruiting[\s-]+(agency|agencies|firm|company)\b|\b(third[\s-]party|external|agency)[\s-]+recruiting\b",
    "recruiter": r"\b(agency|third[\s-]party|external|contract)[\s-]+recruiter\b",
    "staffing": r"\bstaffing[\s-]+(agency|agencies|firm|company|solutions|services|partner)\b",
    "our client": r"\bour clients?[,']?\s+(is|are|has|have|was|were)\b|\bon behalf of (our|a) clients?\b|\bour client,\s|\bfor one of our clients?\b",
}

# Compiled ONCE, here, not per candidate. `re.compile` was inside the scoring loop, so
# every one of these was rebuilt for every listing of every search.
#
# Each pattern is paired with a LITERAL that every one of its alternatives requires, so
# a cheap substring test can decide "definitely not here" before the automaton runs —
# the same gate `match._stem` puts in front of boost matching, which was built and then
# not applied to this path. Soundness is by inspection of the alternations above:
# `agenc` is in both `agenc(y|ies)` branches, `client` in all four "our client" ones,
# and `recruiting`/`recruiter`/`staffing` lead their own. Measured on 2,869 real bodies,
# results IDENTICAL: staffing 620->46 ms, agency 754->188, our client 381->100.
_QUALIFIED_COMPILED = {
    key: (re.compile(pat, re.IGNORECASE).search, lit)
    for key, pat, lit in (
        ("agency", QUALIFIED_PENALTY["agency"], "agenc"),
        ("recruiting", QUALIFIED_PENALTY["recruiting"], "recruiting"),
        ("recruiter", QUALIFIED_PENALTY["recruiter"], "recruiter"),
        ("staffing", QUALIFIED_PENALTY["staffing"], "staffing"),
        ("our client", QUALIFIED_PENALTY["our client"], "client"),
    )
}

# The one term whose bare form is untrustworthy even in a company name — 420 federal
# listings say so.
QUALIFY_IN_COMPANY_TOO = frozenset({"agency"})

# ── the mirror case: a term that counts ONLY in the company name ─────────────
#
# Reading u11's board turned up a Forward Deployed Engineer role at a company called
# "TechTree's client", scoring 122 with no penalty. The user's avoid-term is "our
# client"; the employer field says "TechTree's client"; phrase matching does exactly what
# it says and misses it. A company whose NAME is somebody else's client has disclosed the
# arrangement in the one field that is supposed to say who you would work for.
#
# It is company-only, and that scoping is load-bearing rather than tidy: 244 job TITLES
# in the corpus contain "client" — Client Success Director, Client Support Engineer,
# Client Services Project Manager — all perfectly ordinary roles. The existing penalty
# path tests title and company as one blob, so a term added the usual way would sink all
# 244. Against 35 listings at the one company whose NAME contains the word.
#
# It is deliberately NOT a body term either. "helping our clients succeed" is the
# ordinary-language case 1.7b just finished removing, and re-adding it here would undo
# that through a side door.
COMPANY_ONLY_PENALTY = frozenset({"client"})

# Whether the model's suggested titles join the FTS query as well as the scoring. ON in
# production — it is what rescues a search whose exact phrasing matches nothing. The OFF
# setting exists only so the before/after harness can capture an arm where related titles
# score but do not retrieve, which is the difference between "the ranker helped" and
# "the retrieval helped". Bundled, those two are indistinguishable in the numbers.
RELATED_IN_RETRIEVAL = os.environ.get("JOBFITR_RELATED_IN_RETRIEVAL", "1") != "0"

# ── the one cap left, and why the other four are gone ────────────────────────
#
# RESULT_CAP is the DELIVERY slice: how many scored rows we hand the client. It is about
# BANDWIDTH, not taste — 200 rows cost 54.6 KB gzipped, and the tail is what the board's
# own salary/work-style filters eat. It is the only limit the server still imposes.
#
# Four other mechanisms used to sit between the score and the user. Measured across the
# 57 test users, together they withheld 4,766 of 7,995 already-scored listings — 60% of
# the board, computed and then thrown away:
#
#   RESULT_LADDER    five re-ranking passes over the same pool, tightening freshness and
#                    pickiness until ~50 results were found. 34 of 57 users fell all the
#                    way to the loosest rung — whose floor is 0.0 — and still averaged 22
#                    results. Five passes to arrive at "don't filter."
#
#   MIN_SCORE_FRAC   kept only candidates scoring above a fraction of the TOP score. Being
#                    relative to the top made it read the score SCALE: when the scorer
#                    moved from floats to integers, the rung distribution shifted from 8
#                    to 14 users on the strictest tier with retrieval byte-identical. A
#                    filter that moves when an unrelated number moves cannot be reasoned
#                    about. Pickiness is the board's fit slider now.
#
#   MAX_PER_COMPANY  capped one employer's contribution — by DROPPING their listings past
#                    the 4th, not demoting them. It was solving a real problem (Anduril
#                    has 1,063 rows, VHA 915) with an instrument that deletes evidence
#                    invisibly. If one employer dominates a board, the user should be able
#                    to SEE that and filter it, not have it quietly corrected.
#
#   CANDIDATE_LIMIT  scored only the top 500 BM25 candidates. Retrieval was deciding what
#                    the ranker was allowed to consider. Scoring is deterministic Python
#                    and it is cheap: uncapped, the widest user in the fixture is 5,129
#                    rows in 1.14s (fetch + score). Cost tracks boost count × body length
#                    rather than row count — the 6,012-row user scores FASTER — so a row
#                    limit was never protecting the expensive thing anyway.
RESULT_CAP = int(os.environ.get("JOBFITR_RESULT_CAP", "200"))

CHAT_RATE_LIMIT = os.environ.get("CHAT_RATE_LIMIT", "20/minute")
SCORE_RATE_LIMIT = os.environ.get("SCORE_RATE_LIMIT", "40/minute")
# Daily cap on live keyed-source fetches (Adzuna + USAJOBS + Google/SerpApi) — the
# actuator saturation. When tripped, we serve the cache with a `degraded` banner
# instead of burning a free quota. Default is UNDER Adzuna's ~250/day free tier so
# the valve fires before the real quota is blown; raise the env to match a higher
# plan. (Env name kept as ADZUNA_DAILY_CEILING so the deployed box's config still
# applies; it now governs all three keyed sources, not just Adzuna.)
ADZUNA_DAILY_CEILING = int(os.environ.get("ADZUNA_DAILY_CEILING", "200"))

app = FastAPI(title="jobfitr", version="0.1.0", lifespan=lifespan)

# The scoring response is repetitive JSON, which is what gzip is best at: the measured
# 50-row payload is 55 KB raw and 13 KB compressed, so at RESULT_CAP=200 this is the
# difference between shipping ~220 KB and ~52 KB per search. Two lines, and it is what
# makes a bigger board cheap enough to hand a phone on cell data.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Per-IP rate limiting.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── daily live-fetch ceiling (the load-shed) ──────────────────────────────────
# The tally is persisted in the store (store.live_fetch_count/note_live_fetch), so a
# restart or crash cannot reset it and defeat the ceiling. Only the last-success
# timestamp stays in-process — it's a display value, fine to be per-process.
_last_fetch_ok = {"at": None}


def _today() -> str:
    return datetime.now(_ET).date().isoformat()


def _fetch_ceiling_reached() -> bool:
    return store.live_fetch_count() >= ADZUNA_DAILY_CEILING


def _note_fetch() -> None:
    store.note_live_fetch()
    _last_fetch_ok["at"] = datetime.now(_ET).isoformat(timespec="seconds")


# Greenhouse (and most ATS feeds) ship the JD as HTML. Rendered with textContent the
# markup came through as literal text on the card — the top result opened with
# '<div class="content-intro"><h3>About Arize</h3>'. Cleaning it here rather than in the
# client fixes every consumer at once and shrinks the payload.
_SCRIPT_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_TAG_RE = re.compile(r"<[^>]+>")
# A tag needs a closing '>' to match above, and the harvest caps body text at 8,000
# chars — so a body cut mid-tag ends with an UNTERMINATED one that survived the strip
# and rendered as literal markup on the card ('<a href="https://www.cnbc.com/2022/05').
# Anchored at the end AND requiring a tag name right after the '<', so a real
# less-than survives: "a < b" has a space next and is left alone, as is a bare
# trailing '<'.
_DANGLING_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*$")


def _plain_text(text) -> str:
    """HTML (or already-plain text) → clean display text.

    Tags become a SPACE, not nothing: '<p>One</p><p>Two</p>' must read "One Two", never
    "OneTwo". Entities are decoded after stripping, so text that was escaped in the
    source ('&lt;b&gt;') stays literal instead of becoming markup.
    """
    if not isinstance(text, str):
        return ""
    s = _SCRIPT_RE.sub(" ", text)  # drop script/style bodies outright
    s = _TAG_RE.sub(" ", s)
    s = _DANGLING_TAG_RE.sub(" ", s)  # a body truncated mid-tag leaves an unclosed one
    s = html.unescape(s)  # &amp; &nbsp; &#8217; …
    return " ".join(s.split())  # collapses \xa0 too — it is whitespace to str.split


def _snippet(text) -> str:
    return _plain_text(text)[:SNIPPET_CHARS]


def _description(text) -> str:
    """A fuller (but still capped) JD body for the expand-to-detail view.

    Still served from the cached snapshot — never a live fetch. The full untruncated
    body is never returned; the harvest already caps text at 8,000 chars.
    """
    return _plain_text(text)[:DESC_CHARS]


# ── facet normalization ───────────────────────────────────────────────────────
# A filter chip is a PROMISE that picking it shows you every matching job. Raw source
# values break that promise: the live pool carries "full_time", "Full Time", "Full-Time"
# and "Full-time" as four separate chips over what is one thing (621 rows split four
# ways), plus five spellings of contract. Both fields are also used as dumping grounds
# for free text — schedule prose ("Monday-Friday 8:00am-4:30pm"), German seniority
# tokens, and whole sentences — which are not types at all.
#
# So this is a WHITELIST, not a cleanup: a value that does not map to a known token is
# dropped rather than surfaced. An unfilterable chip is worse than no chip.
_EMPLOYMENT_ALIASES = {
    "full time": "full_time",
    "fulltime": "full_time",
    "permanent": "full_time",
    "part time": "part_time",
    "parttime": "part_time",
    "contract": "contract",
    "contractor": "contract",
    "contract long": "contract",
    "contract short": "contract",
    "temporary": "temporary",
    "temp": "temporary",
    "internship": "internship",
    "intern": "internship",
    "freelance": "freelance",
    "seasonal": "seasonal",
    "flex time": "flexible",
    "flextime": "flexible",
    "flexitime": "flexible",
}

# `category` used to be cleaned here by a DENYLIST — regexes for USAJOBS agency names
# ("Department of the Navy" — an EMPLOYER, not a field), a seniority ("Mid-Senior
# Level"), an ATS code ("220 - Solutions PS"), plus a 40-character length cap. It is
# gone because jobfitr.vocab replaced it with an ALLOWLIST at the store boundary: 22
# canonical fields, and NULL for everything else.
#
# That is strictly better and the reason is structural, not stylistic. A denylist has
# to enumerate every bad value and passes anything it forgot — it let through "Solutions
# PS", "Engineering - Pipeline", "Go To Market", 2,239 distinct strings in all. An
# allowlist can only ever emit one of 22 known-good values, so the failure mode flips
# from "a garbage chip appears in the Field drawer" to "a real field is missing", which
# is visible, countable, and fixed by adding one line to vocab._CATEGORY_MAP.


def _norm_employment_type(value) -> str:
    """Canonical employment-type token, or '' when the value is not a type at all."""
    key = " ".join(
        "".join(ch if ch.isalnum() else " " for ch in str(value or "")).split()
    ).lower()
    return _EMPLOYMENT_ALIASES.get(key, "")


# The gauge is explicitly "relative to your best match", so it is normalized ACROSS the
# returned set rather than divided by the top score. The old ratio (score / top) died
# whenever the top score was <= 0, which is the normal case for a common one-word query:
def _apply_via(direct_apply) -> str:
    """'employer' | 'aggregator' | '' — the third is "the source did not say"."""
    if direct_apply is None:
        return ""
    return "employer" if direct_apply else "aggregator"


def _shape(c: dict, points: int, why: str, parts: list) -> dict:
    """The lean per-card payload the front end renders (store row → card)."""
    body = c.get("body") or c.get("text") or ""
    # the derived facet tags (real facets category/employment_type sit in their own keys)
    tags = [t for t in (c.get("remote"), c.get("seniority"), c.get("salary_band")) if t]
    return {
        "title": c.get("title", ""),
        "company": c.get("company", ""),
        "location": c.get("location", ""),
        "url": c.get("url", ""),
        "posted": c.get("posted", ""),
        "source": c.get("source", ""),
        "salary": c.get("salary", ""),
        "category": c.get("category") or "",
        "employment_type": _norm_employment_type(c.get("employment_type")),
        # ── the filter fields ────────────────────────────────────────────────
        # `state` is a USPS code or "" (jobfitr.vocab.us_state), so it is a closed set
        # the drawer can offer. `city` rides along for the card line only — 1,129
        # distinct values in the live pool is a search box, not a facet.
        "state": c.get("state") or "",
        # 100% filled, and it IS the product's promise. A filter, never a score input:
        # a direct link is a better EXPERIENCE, not a better MATCH, and folding it into
        # the fit number would tell someone a job suits them because of its URL.
        #
        # Emitted as a STRING rather than the stored 0/1 so it behaves like every other
        # facet. facet_counts skips falsy values — which is right for "the source said
        # nothing" but wrong for an integer 0 that means "we checked, it's an
        # aggregator". A word cannot be accidentally falsy.
        #
        # THREE states, not two. A NULL `direct_apply` means the source never said, and
        # collapsing that to "aggregator" is exactly the unearned assertion the facet
        # rule forbids — the same shape as the 12,623 rows that used to read "On-site"
        # on no evidence. 0 rows are NULL today because live.py now crosses _coerce;
        # this keeps that from silently re-arming if a source stops sending it.
        "apply_via": _apply_via(c.get("direct_apply")),
        # An ANNUAL USD figure (see store.annual_salary), not the raw column — the slider
        # compares these against each other and the raw values are in five currencies
        # and four periods. None means "we could not put this on the scale", and the
        # front end falls back to reading the display string.
        "salary_min": store.annual_salary(c),
        "tags": tags,
        "points": points,  # THE score — an absolute integer, the same meaning every day
        "parts": parts,  # what earned it: [(label, delta)] — the receipt under the number
        # COMPATIBILITY, and they leave next release. A browser holding the previous
        # app.js reads fit_score and why; dropping them the same day the new payload
        # ships would blank that user's board on a cache they did not ask for. `points`
        # is the truth — fit_score is the identical integer under its old name, and the
        # card no longer reads either.
        "fit_score": points,
        "why": why,
        "snippet": _snippet(body),
        "description": _description(body),
    }


def _eligible(candidates: list, exclude, remote_only: bool, max_age_days) -> list:
    """The user's three HARD filters — terms they said to hide, remote-only if they
    chose it, their own recency preference. Everything else is ranking, not filtering.

    THIS MUST RUN BEFORE `_dedupe_listings`, AND THAT ORDER IS A BUG FIX, NOT A STYLE
    CHOICE. Dedupe collapses on (company, title) and keeps the row with the longest
    body — a tiebreak that knows nothing about whether the survivor is one the user can
    actually receive. Filtering afterwards therefore had a hole with no error in it: a
    REMOTE listing whose non-remote twin carried more text was discarded by dedupe, and
    the surviving twin was then removed by the remote-only filter. The job vanished from
    a search that had asked for exactly it.

    Measured on the frozen corpus: **42 remote listings erased across 10 of the 20
    remote-only profiles**, including the owner's own daily search. The same hole exists
    for `exclude` and `max_age_days` — it is a property of the ordering, not of remote —
    which is why the fix is to filter first rather than to push one predicate into SQL.

    Idempotent, so `_rank` calls it again harmlessly and there is no second copy of a
    predicate to drift.
    """
    out = []
    for c in candidates:
        title = (c.get("title") or "").lower()
        # Exclusions test the COMPANY as well as the title: "recruiting agency" is an
        # employer trait, not a job trait, so a title-only test let a staffing firm's
        # normal-sounding listings through under the very term meant to remove them.
        #
        # WHOLE-WORD, not substring. This read `x in blob` — the exact substring test
        # deliberately removed from scoring, still living in the one path that DELETES a
        # listing rather than ranking it. Measured: 220 of 2,573 exclusions (8.6%) were
        # false hides — `intern` deleting "Software Engineer, Internal Systems", `sales`
        # deleting "Sr. Forward Deployed Engineer - Salesforce Health Cloud", 13 of them
        # exact title matches. Hiding a job is the most destructive thing this code does
        # and it was the least precise test in it.
        if any(
            has_term(x, f"{title} {(c.get('company') or '').lower()}") for x in exclude
        ):
            continue
        if remote_only and c.get("remote") != "remote":
            continue
        # `None` means the user never expressed a recency preference, so we do not
        # invent one. The old ladder always imposed an age — 90 days at its loosest —
        # which meant nothing older was reachable no matter what anyone wanted.
        if max_age_days is not None:
            age = age_int(c.get("posted", ""))
            if age is not None and age > max_age_days:
                continue
        out.append(c)
    return out


def _dedupe_listings(candidates: list) -> list:
    """Collapse rows that are the same job posted twice, keeping the richest one.

    RUN `_eligible` FIRST — see its docstring. The longest-body tiebreak below is blind
    to the user's filters, so deduping an unfiltered set silently deletes jobs.

    The same opening reaches the pool from more than one source (an aggregator and the
    employer's own ATS), and the two rows are rarely byte-identical — different URLs,
    different salary formatting — so the store cannot dedup them on a key. To a user
    they are simply the same job listed twice, which reads as a broken search: the live
    run showed one role at both #17 and #18 and another at #5 and #6.

    Identity is (normalized company, normalized title). Where duplicates disagree we
    keep the row with the LONGEST body, because body text is what the rerank and the
    card's description bullets both feed on — keeping the thin copy would throw away
    evidence and, under the boost scoring above, quietly change where the job ranks.
    """
    best: dict[tuple[str, str], int] = {}
    out: list = []
    for c in candidates:
        key = (norm_key(c.get("company")), norm_key(c.get("title")))
        if not any(key):  # no company AND no title — nothing to dedup on
            out.append(c)
            continue
        idx = best.get(key)
        if idx is None:
            best[key] = len(out)
            out.append(c)
        elif len(c.get("body") or "") > len(out[idx].get("body") or ""):
            out[idx] = c
    return out


# ═══════════════════════════════════════════════════════════════
# scoreboard()
# ═══════════════════════════════════════════════════════════════
# Score one listing and SHOW THE WORKING. Returns the total plus the
# parts that made it, because the parts are the deliverable: the card
# renders them as the receipt under the number, and the arithmetic
# tests read them to prove each component independently. A scorer
# that returned one opaque integer would make both impossible.
#
# Where each signal is read from is measured, not assumed:
#   BOOSTS  → title + body. "rag" appears in 42 titles and 4,274
#             bodies; "fastapi" in 0 titles and 11. The signal lives
#             in the body, and repetition is real evidence — a posting
#             naming RAG twenty times is more about RAG than one that
#             mentions it once.
#   PENALTIES → title + company ONLY. Whole-word in bodies, "senior"
#             is 18.8% of the corpus and "sales" 13.1% — ordinary
#             prose, not signals. Penalising on the body would sink
#             a fifth of every board for saying "senior engineer".
#
# Freshness is absent by design. A three-day-old wrong job is not a
# better fit than a month-old perfect one; recency is a filter.
# ═══════════════════════════════════════════════════════════════
def scoreboard(
    title: str,
    company: str,
    body: str,
    titles: list,
    boosts: list,
    penalties: list,
    related_titles: list | None = None,
    title_root: str = "",
    lowered: bool = False,
) -> dict:
    """Score one listing. `lowered` asserts title/company/body are ALREADY lowercase and
    lets the term gate skip re-copying the body once per term — see `match._stem`. It
    defaults False so the goldens, which pass raw-case text, keep the safe path."""
    parts: list[tuple[str, int]] = []

    # Hoisted out of the term loops below. `f"{title} {body}"` was rebuilt once per boost
    # term and `f"{title} {company}"` once per penalty term, so a seven-boost search
    # copied an 8,000-character body seven times for every candidate on the board.
    # `body_lc` exists only for the penalty gate, which tests a plain literal.
    blob = f"{title} {body}"
    title_company = f"{title} {company}"
    body_lc = body if lowered else body.lower()

    # Title: the BEST single tier across the titles the user named — and only if none
    # of them land, a flat 30 for matching one the MODEL suggested. Tiers do not stack
    # and titles do not add; one role, one score. `related_titles` defaults to None so
    # every stored search from before the field existed scores exactly as it did.
    title_pts, is_related = title_score(titles, related_titles, title, title_root)
    if title_pts:
        # The label reaches the card's why-chips, so a user can tell a match on their
        # own words from a match on the machine's guess.
        parts.append(("related title" if is_related else "title", title_pts))

    boost_pts = 0
    for term in boosts:
        if not term:
            continue
        hits = term_hits(term, blob, lowered=lowered)
        if not hits:
            continue
        # Diminishing per occurrence: the 5th mention says little the 4th did not,
        # and the decay is what stops a term like "data" — 46% of the corpus —
        # from running away with the board on sheer repetition.
        pts = sum(BOOST_DECAY[:hits])
        boost_pts += pts
        parts.append((f"{term} ×{hits}", pts))

    penalty_pts = 0
    for term in penalties:
        if not term:
            continue
        # An ambiguous bare word has to earn its penalty by appearing in a phrase that
        # makes it an employer type. Everything else keeps the plain whole-word test.
        key = norm_key(term)
        if key in COMPANY_ONLY_PENALTY:
            # Company field alone — not the title, and never the body. See the constant.
            in_title = has_term(term, company, lowered=lowered)
            in_body = False
        elif (compiled := _QUALIFIED_COMPILED.get(key)) is not None:
            found, literal = compiled
            # The gate: no alternative can match without this literal present.
            in_body = bool(found(body)) if literal in body_lc else False
            # A company that names itself "…Staffing" has disclosed what it is; a body
            # that says "high agency" has not. Same word, different evidential weight,
            # so the bare form is still trusted in a name unless measurement says no.
            in_title = (
                bool(found(title_company))
                if key in QUALIFY_IN_COMPANY_TOO
                else has_term(term, title_company, lowered=lowered)
            )
        else:
            in_title = has_term(term, title_company, lowered=lowered)
            in_body = has_term(term, body, lowered=lowered)
        if in_title:
            hit = PENALTY_TITLE  # naming itself a staffing firm is the strongest tell
        elif in_body:
            hit = PENALTY_BODY  # "our client…" buried in the description
        else:
            continue
        penalty_pts += hit
        parts.append((term, -hit))

    return {
        "points": title_pts + boost_pts - penalty_pts,
        "title_points": title_pts,
        "boost_points": boost_pts,
        "penalty_points": penalty_pts,
        "parts": parts,
    }


def _rank(
    candidates,
    titles,
    boosts,
    penalties,
    exclude,
    remote_only,
    max_age_days,
    limit,
    related_titles=None,
):
    """Score every candidate on the scoreboard, hard-filtered by exclude/remote/age,
    sorted by points, sliced to `limit`.

    ONE pass. It used to run five times over the same pool under a freshness/pickiness
    ladder, then cut everything below a fraction of the top score, then drop each
    employer's roles past the fourth. All three are gone — the only thing between a
    listing's score and the user is now `limit`, which is a bandwidth decision.

    The three filters that remain are the ones the USER asked for: terms they said to
    hide, remote-only if they chose it, and their own recency preference.

    The tuple is (candidate, points, why, parts) and stays POSITIONAL: the sort reads
    t[1] and the caller unpacks all four.
    """
    scored = []
    for c in _eligible(candidates, exclude, remote_only, max_age_days):
        title = (c.get("title") or "").lower()
        body = (c.get("body") or "").lower()
        board = scoreboard(
            title,
            (c.get("company") or "").lower(),
            body,
            titles,
            boosts,
            penalties,
            related_titles,
            (c.get("title_root") or "").lower(),
            lowered=True,  # every string above is already .lower()'d — see match._stem
        )
        # `why` is built FROM the parts, not computed separately. It used to test
        # `term in blob` — the same substring bug removed from scoring — so boosting
        # "rag" listed "rag" as a matched signal because "leverage" contains it. One
        # computation now feeds both the number and the chips that explain it.
        why = ", ".join(label for label, _ in board["parts"][:4])
        scored.append((c, board["points"], why, board["parts"]))
    scored.sort(key=lambda t: t[1], reverse=True)
    top = scored[0][1] if scored else 0
    return scored[:limit], top


def _warm_cache(titles: list, location: str) -> str | None:
    """Ensure the store holds fresh jobs for this (titles, location): serve the fresh
    cache untouched, or do ONE bounded live fetch (Adzuna + USAJOBS, single-flighted).

    Returns a `degraded` reason (or None). Idempotent + coalesced, so calling it early
    from /api/prefetch and again from /api/score costs at most one upstream fetch —
    mark_fetched makes the second call see a fresh cache.
    """
    if not titles:
        return None
    key = store.search_key(titles, location)
    if store.search_fresh(key):
        return None  # fresh (< TTL) — no API call
    if _fetch_ceiling_reached():
        return "live_search_limit"  # load-shed: serve the cache
    try:
        rows = live.coalesced_fetch(
            titles, location
        )  # blocking (threadpool), single-flight
        if rows:
            store.upsert_jobs(rows)
        store.mark_fetched(key)
        _note_fetch()
        # A fetch that SUCCEEDED can still be short a source. Google for Jobs is the only
        # lane reaching the Workday/iCIMS postings large local employers use — measured
        # on one Louisville search, 14 of its 17 employers appeared in no other source —
        # so losing it quietly costs a local searcher most of the real employers on their
        # board while everything reads healthy. Say so instead.
        why = live.last_source_report().get("google_jobs", {}).get("why", "")
        if why == "quota":
            return "monthly_source_limit"
        return None
    except Exception:  # noqa: BLE001
        return "fetch_error"  # serve whatever's cached


@app.post("/api/prefetch")
@limiter.limit(SCORE_RATE_LIMIT)
def prefetch(request: Request, payload: dict = Body(...)) -> dict:
    """Warm the cache for a search-in-progress the moment titles + location are known,
    so the 3-4s live fetch overlaps the rest of the chat and /api/score is instant.
    Reuses _warm_cache — coalesced + mark_fetched dedup it against the later score."""
    titles, location = search_inputs(payload)
    degraded = _warm_cache(titles, location)
    # The candidate count rides along because this is the one moment it is both KNOWN
    # and USEFUL: titles are settled, the rows have not been fetched yet, and the client
    # still has a question or two to ask before it needs the board. A COUNT over the FTS
    # index is milliseconds. It lets the loading state say "ranking 25,849 matches"
    # instead of showing a spinner for eighteen seconds and looking broken.
    related = _clean_list(payload.get("related_titles"))
    search_titles = titles + related if RELATED_IN_RETRIEVAL else titles
    return {
        "ok": degraded is None,
        "warmed": bool(titles),
        "degraded": degraded,
        "candidates": store.candidate_count(search_titles) if search_titles else 0,
    }


@app.post("/api/score")
@limiter.limit(SCORE_RATE_LIMIT)
def score_jobs(request: Request, payload: dict = Body(...)) -> dict:
    """Live-fetch (or serve the fresh cache) → BM25 candidates → personalized rerank.

    Runs as a sync def, so FastAPI executes it in a threadpool — the blocking live
    fetch never stalls the event loop, and live.coalesced_fetch (threading) coalesces
    concurrent identical searches. Degrades to the cache when the daily ceiling trips.
    """
    started = time.perf_counter()
    cfg = config_from_dict(payload)
    titles, location = search_inputs(payload)
    boosts = _clean_list(payload.get("boosts"))
    # The user's own rank_down terms, or job-radar's twelve generic staffing defaults.
    # COMPANY_ONLY_PENALTY is appended here rather than living with its siblings because
    # that default list is job_radar.config.DEFAULT_AGENCY_PENALTY — inside the
    # dependency, not this repo, so it cannot be edited from here.
    penalties = list(cfg.agency_penalty.keys()) + sorted(COMPANY_ONLY_PENALTY)
    exclude = list(cfg.exclude_titles)
    # The model's own suggestions, added once the user's title list was final. They
    # score a flat 30 — below every tier a title the user NAMED can earn.
    related = _clean_list(payload.get("related_titles"))

    degraded = _warm_cache(titles, location)

    # Every listing FTS5 matches, not a top-N slice of them. Retrieval decides what is
    # RELEVANT; it has no business deciding what the ranker is allowed to consider.
    #
    # Related titles join the query, and that is what gives a search somewhere to go
    # when the user's own phrasing finds nothing: _fts_query ORs quoted exact phrases,
    # so "High School Teacher" matches 0 rows while the suggested "Teacher" matches 225.
    #
    # The env flag exists for MEASUREMENT, not for production. With it off the titles
    # still score but stay out of retrieval, which separates what the ranker did from
    # what the retrieval did — otherwise v1 bundles two changes and the before/after
    # cannot say which one worked.
    search_titles = titles + related if RELATED_IN_RETRIEVAL else titles

    # Read recency off the payload rather than cfg, because cfg.max_age_days carries a
    # 60-day DEFAULT and a default is not a preference. Absent → no age filter at all.
    # Hoisted above retrieval because `_eligible` needs it — see the next block.
    raw_age = payload.get("max_age_days")
    max_age_days = (
        int(raw_age)
        if isinstance(raw_age, (int, float)) and not isinstance(raw_age, bool)
        else None
    )

    # FILTER, THEN DEDUPE. The order is load-bearing: dedupe keeps the longest-bodied
    # row of each (company, title) pair, which is blind to whether that row is one the
    # user can receive — so deduping first silently deleted 42 remote listings across
    # half the remote-only profiles. See `_eligible`.
    candidates = (
        _dedupe_listings(
            _eligible(
                store.bm25_candidates(search_titles),
                exclude,
                cfg.remote_only,
                max_age_days,
            )
        )
        if search_titles
        else []
    )
    kept, top = _rank(
        candidates,
        titles,
        boosts,
        penalties,
        exclude,
        cfg.remote_only,
        max_age_days,
        RESULT_CAP,
        related,
    )
    results = [_shape(c, points, why, parts) for c, points, why, parts in kept]
    # Count facets over NORMALIZED rows so the counts match the chips the cards produce.
    # Normalizing on a copy (not the shaped result) keeps remote/seniority/salary_band,
    # which live as top-level columns here but are folded into `tags` by _shape.
    facets = store.facet_counts(
        [
            {
                **c,
                "category": c.get("category") or "",
                "employment_type": _norm_employment_type(c.get("employment_type")),
                "apply_via": _apply_via(c.get("direct_apply")),
            }
            for c, _, _, _ in kept
        ]
    )
    pool = store.pool_size()
    # AFTER the response is fully built, so the log records what was actually delivered
    # and a logging fault cannot cost a user their search. `record` swallows everything
    # for the same reason — see its block header.
    searchlog.record(
        titles=titles,
        related=related,
        boosts=boosts,
        exclude=exclude,
        location=location,
        remote_only=cfg.remote_only,
        max_age_days=max_age_days,
        min_score=payload.get("min_score"),
        pool=pool,
        candidates=len(candidates),
        kept=kept,
        degraded=degraded,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        # Empty when the search served a fresh cache and called nobody — which is itself
        # worth being able to tell apart from "every source returned nothing".
        sources=live.last_source_report() or None,
        # verify-slot.sh sets this so its pre-flip searches do not read as user demand.
        probe=payload.get("probe") is True,
    )
    return {
        "count": len(results),
        "degraded": degraded,
        "facets": facets,
        "pool": pool,
        "jobs": results,
    }


@app.post("/api/chat")
@limiter.limit(CHAT_RATE_LIMIT)
async def chat_endpoint(request: Request, payload: dict = Body(...)) -> dict:
    """One structured chat turn → {reply, config, ready}. The ONLY metered path;
    never touches scoring.

    Fails CLOSED to the form: a 503 (no key / daily ceiling) or 429 (rate/turn cap)
    tells the front end to fall back to the search form.
    """
    if not chatmod.chat_available():
        raise HTTPException(status_code=503, detail="chat_unavailable")
    if chatmod.daily_ceiling_reached():
        raise HTTPException(status_code=503, detail="daily_ceiling")

    messages = chatmod.sanitize_messages(payload.get("messages"))
    if not messages:
        raise HTTPException(status_code=422, detail="messages required")
    if chatmod.over_turn_cap(messages):
        raise HTTPException(status_code=429, detail="turn_cap")

    current = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    # The client flips this on once the board has been shown — after that the user is
    # adjusting a live search, not answering intake questions.
    refining = bool(payload.get("refining"))
    chatmod.note_request()
    return await chatmod.turn(messages, current, refining=refining)


def _code_sha() -> str:
    """The commit THIS process is running, resolved once at import.

    The server is the only thing that knows which checkout it was started from. A
    measurement harness reading git from its own directory records where the SCRIPT
    lives, not where the CODE came from — which silently mislabels every run made
    against a worktree, exactly the setup the three-version comparison depends on.
    """
    import subprocess

    try:
        return (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(Path(__file__).parent.parent),
                    "rev-parse",
                    "--short",
                    "HEAD",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            or "unknown"
        )
    except Exception:
        return "unknown"


CODE_SHA = _code_sha()


@app.get("/api/meta")
def meta() -> dict:
    """Pool freshness for the UI (how many jobs, newest posting) + which build is live."""
    return {
        "count": store.pool_size(),
        "harvested_at": store.newest_posted(),
        "code_sha": CODE_SHA,
    }


@app.get("/api/health")
def health() -> dict:
    """Status for you + an uptime monitor: which feeds are live, budget used, freshness."""
    return {
        "ok": True,
        "adzuna_ok": bool(
            os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY")
        ),
        "openrouter_ok": chatmod.chat_available(),
        "daily_fetches_used": store.live_fetch_count(),
        "daily_fetch_ceiling": ADZUNA_DAILY_CEILING,
        "pool_size": store.pool_size(),
        # The size of the harvest snapshot this slot should be serving. The pool is
        # that snapshot plus live-fetch accumulation, so pool_size < snapshot_count
        # means the slot UNDER-ingested — the exact regression verify-slot.sh gates on
        # with a ratio, instead of a fixed floor that a 90%-smaller harvest slips past.
        "snapshot_count": snapshot.load_snapshot(store.JOBS_JSON_PATH)["meta"].get(
            "count", 0
        ),
        "last_successful_fetch": _last_fetch_ok["at"],
        # The harvest snapshot this slot has actually ingested. Stale here means the
        # slot is serving an aging pool even while the nightly harvest is green — the
        # exact failure the background sync exists to prevent, so it's worth surfacing.
        "snapshot_imported_at": store.snapshot_imported_at(),
    }


def _web_dir() -> Path:
    """The static front end lives in ./web at the repo root; overridable for deploy."""
    override = os.environ.get("JOBFITR_WEB_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "web"


# Serve the front end at / — mounted LAST so the /api/* routes above still win.
# Guarded so the API still boots headless (e.g. before the front end exists).
_WEB = _web_dir()
if _WEB.is_dir():
    app.mount("/", StaticFiles(directory=str(_WEB), html=True), name="web")


def main(argv=None) -> int:  # pragma: no cover — exercised via jobfitr-serve
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(
        prog="jobfitr-serve", description="Run the jobfitr API locally."
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args(argv)
    uvicorn.run(
        "jobfitr.server:app", host=args.host, port=args.port, reload=args.reload
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
