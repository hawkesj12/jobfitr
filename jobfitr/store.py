"""The job store — SQLite + FTS5. Replaces the flat jobs.json.

Two writers feed it: the demoted periodic baseline harvest and the per-search
live fetch (live.py). Reads are the per-request scorer. It is the cache half of a
control loop: a per-(title,location) TTL decides when to re-fetch (freshness), and
a nightly eviction garbage-collects (staleness) — the ~14x gap between the two
(24h TTL vs 14d evict) is the damping that keeps actively-searched jobs from ever
being evicted, so the two never thrash.

Ranking is FTS5 BM25 for the base title/body relevance (differentiates even a
one-word query, which the old flat keyword-sum could not) + a personalized rerank
applied in server.py. FTS5 is an external-content index over the `jobs` table,
kept in sync by triggers, so an upsert-by-url is the only write path.

Concurrency: every call opens its own short-lived connection (SQLite is cheap to
open and the server runs the scorer in a threadpool) with WAL, so concurrent reads
never block and writers serialize safely.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import time
from urllib.parse import urlparse
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from job_radar.dedup import ats_from_url
from job_radar.discover import _norm_name as _jr_norm_name
from job_radar.scoring import remote_posting

from jobfitr import vocab

_ET = ZoneInfo("America/New_York")

# ── config (env-overridable) ──────────────────────────────────────────────────

# The store's schema. Declared up here rather than beside the DDL because THE FILENAME
# DERIVES FROM IT (see DB_PATH below) — bumping this number is what makes a schema change
# write a new file rather than collide with the one production is serving.
SCHEMA_VERSION = 3


def _default_db_path() -> str:
    """Where the store lives: ONE file, shared by every process, named for its schema.

    ── WHY ONE STORE, SHARED BY BOTH SLOTS ──────────────────────────────────────
    Until 2026-08-17 each blue-green slot owned its own `jobs.db` and the harvest/resolve
    lane a third. Measured on the box, that was 4.5 GB of disk for one ~300 MB cache, the
    job pool stored three times, eviction running twice on two copies — and, the reason it
    had to go, the copies DIVERGED: blue held 29,432 jobs while green held 24,652, a 16%
    gap. Under active development every deploy ends in a flip, so every deploy landed on a
    slot whose pool was some unknown distance behind. A flip was not a rollback; it was a
    change of dataset.

    ── WHY THE VERSION IS IN THE FILENAME ───────────────────────────────────────
    A schema change here is a REBUILD, not a migration, and `_check_schema_version` raises
    in BOTH directions — new code will not read an old file, and old code will not read a
    new one. So a single fixed filename would make every schema bump an outage: rebuild in
    place and the still-running old process dies too.
    Putting the version in the name removes that. Bumping SCHEMA_VERSION points the new
    build at `jobs-v4.db`, which it can build and verify while the old build keeps serving
    `jobs-v3.db`; after the flip, the old file is deleted. Same zero-downtime property
    per-slot stores were bought for, without the divergence — because the thing that
    genuinely needs two copies is the SCHEMA, which is a property of the code, not of the
    slot. The 2026-08-17 deploy proved the distinction the hard way: blue sat on v2 while
    green was on v3, because a rebuild had only touched the active slot.

    `JOBFITR_DB_PATH` still wins when set, for tests and one-off surgery.
    """
    explicit = os.environ.get("JOBFITR_DB_PATH")
    if explicit:
        return explicit
    d = os.environ.get("JOBFITR_DB_DIR")
    return os.path.join(d, f"jobs-v{SCHEMA_VERSION}.db") if d else "jobs.db"


DB_PATH = _default_db_path()
# Where the shared harvest snapshot lives. Re-imported whenever the harvest rewrites it
# (see sync_snapshot). With one shared store this is no longer how a slot "catches up" —
# there is nothing to catch up to — but it remains the inflow and the rollback artifact.
JOBS_JSON_PATH = os.environ.get("JOBFITR_JOBS_PATH", "jobs.json")

SEARCH_TTL_SECONDS = int(os.environ.get("JOBFITR_SEARCH_TTL", str(24 * 3600)))  # 24h
# How long a job survives without being SEEN again. `last_seen` is a heartbeat, not an age:
# `upsert_jobs` refreshes it on every re-harvest, so a posting the employer takes down simply
# stops being returned and its heartbeat stops advancing. That is the "the listing withdrew
# itself" rule, and it is a different question from EVICT_POSTED_DAYS ("this listing is old").
#
# TWO WINDOWS, because the heartbeat is not equally trustworthy for every source. The nightly
# harvest re-fetches 11 sources, so for those, absence is confirmed EVERY NIGHT and 14 days is
# far more patience than the evidence needs — a dead posting sits on the board for a fortnight.
# The per-search live lane (adzuna, usajobs, google_jobs) is only re-fetched when a user's
# search happens to match, so its `last_seen` means "someone searched something like this
# recently", not "this job is alive". Those keep the long window.
#
# Measured 2026-08-17, after board discovery finished: 67,851 rows (97.7%) come from harvested
# sources, 1,606 (2.3%) from the live lane only. Before discovery the harvested share was far
# smaller, which is why one window was the right call then and is not now.
EVICT_UNSEEN_DAYS = int(os.environ.get("JOBFITR_EVICT_UNSEEN_DAYS", "14"))
EVICT_UNSEEN_POLLED_DAYS = int(os.environ.get("JOBFITR_EVICT_UNSEEN_POLLED_DAYS", "3"))
EVICT_POSTED_DAYS = int(os.environ.get("JOBFITR_EVICT_POSTED_DAYS", "60"))
# LRU cap — the pool's saturation point, enforced by `evict()` after the age rules.
#
# RAISED 50,000 -> 120,000 on 2026-08-17, because the number was chosen when the harvest
# resolved ~1,400 ATS boards and finishing board discovery took that to 6,095. The harvest now
# offers **66,494 US-servable rows** of 100,993 harvested, and the pool reached 68,512 — 37%
# over the old cap. That is not saturation, it is a ceiling set against a third of the current
# supply, and honouring it would LRU-evict real US jobs for being slightly older rather than
# stale (the 14-day unseen and 60-day posted rules already handle stale).
#
# What the headroom costs, stated: ~1.2 GB of SQLite against 96 GB of disk at 7% used, so
# space is not the constraint. The real trade is that a wider pool means more BM25 candidates
# per search and therefore more scoring work — retrieval is FTS5-indexed so the fetch barely
# moves, but the scoreboard runs over every candidate by design (a candidate cannot be
# discarded before it is scored). Measured before/after when this was raised; see the release
# notes in _private.
MAX_ROWS = int(os.environ.get("JOBFITR_MAX_ROWS", "120000"))

# How much of a job description the scorer gets to read. Raised 2,000 -> 8,000 on
# 2026-08-10. The metric is not characters, it is SHARE OF BOOST POINTS CAPTURED —
# boosts decay 8/6/4/2 and stop counting at four occurrences, so what matters is
# whether the terms are reachable at all:
#
#     2,000 -> 41.6%      4,000 -> 73.1%      8,000 -> 95.8%      12,000 -> 99.6%
#
# The median description in the pool is 7,208 characters, so at 2,000 the scorer was
# reading 28% of the average posting and 14.4% of (user, listing) pairs had boost
# evidence sitting past the cut. 12,000 buys the last 3.8 points for ~10% more scoring
# time and 12% more storage, which is not worth it.
#
# MUST MOVE WITH snapshot.TEXT_CAP. The harvest truncates before jobs.json is written,
# so raising only this one is a silent no-op for every harvested row and leaves
# live-fetched rows carrying four times more text than harvested ones.
BODY_CAP = 8000

# ── US-only intake ────────────────────────────────────────────────────────────
# jobfitr serves a US audience, so a posting in Berlin costs storage, scoring time
# (0.7 ms/candidate, and every retrieved candidate is scored) and a board slot for a
# job nobody here can take. job_radar stays international on purpose — it is a
# general-purpose engine on PyPI and the filter is OUR opinion, so it lives here.
#
# THE POLICY IS US AND USD ONLY (decided 2026-08-10). It simplifies everything
# downstream: one currency on the salary scale, one subdivision vocabulary, one legal
# market. Measured on the 21,495-row capture:
#
#   keep  country == "US"  10,404
#   keep  country unknown   7,277 — overwhelmingly US ATS boards; see the caveat
#   drop  non-USD salary      188 — tested FIRST, so this absorbs rows the country
#                                   test would also have caught; 70 of them state no
#                                   country at all and are caught by nothing else
#   drop  country foreign   3,626 — IN · GB · DE · CA · JP
#
# 82.3% kept. The earlier rule EXEMPTED any row tagged remote from the country test, on
# the reasoning that a remote posting has no country by nature. That reasoning holds for
# a blank country — and a blank country passes the test anyway, so the exemption was
# doing no work there. All it uniquely did was keep 577 rows that state a foreign
# country outright ('Canada - Remote', 'Munich (Remote)', 'Paris (Remote)') — CA 163 ·
# GB 100 · IN 38 · DE 33 · BR 30 · JP 28. Remote within another country is not
# location-independent; it is a job in that country. (This said 455 until a panel
# reviewer failed to reproduce it and re-measured; the conclusion never moved.)
#
# KNOWN LEAK, measured, not guessed: unknown-country rows still pass, and some name a
# foreign place in their location TEXT. The fix is better country derivation upstream,
# not a regex of country names here — a name list produces false positives on real US
# cities (Dublin CA, Berlin CT, Toronto OH) and rots. Two narrower signals were tried:
# CURRENCY works and is above; SUBDIVISION was removed for firing zero times, since
# every row with a foreign state already carried a foreign country. Separately ~20 rows
# arrive already mislabelled `country: "US"` because job_radar read a foreign country
# code as a US state ("Berlin, DE" -> DE Delaware); those leak too, and that is a
# job-radar fix.
US_ONLY = os.environ.get("JOBFITR_US_ONLY", "1") != "0"

# Whether a posting must show positive evidence that its link reaches the employer.
DIRECT_ONLY = os.environ.get("JOBFITR_DIRECT_ONLY", "1") != "0"

# The aggregators jobfitr carries DELIBERATELY, despite their links being redirects.
#
# ADZUNA IS HERE FOR ONE MEASURED REASON: it supplies **83.0% of in-metro results on local,
# non-tech searches** — 180 of 217 across four metros on 2026-08-17 (Louisville warehouse,
# Grand Rapids nursing, Knoxville CDL, Des Moines retail), against google_jobs' 12.4% and the
# ATS lane's 4.6%. Google for Jobs does NOT carry local on its own. Every adzuna URL is
# `www.adzuna.com/land/ad/<id>` — a redirect, never the employer, and unverifiable (403 to
# both HEAD and GET) — so carrying it is a deliberate exception, paid for by saying so on the
# card and in the copy rather than by pretending otherwise.
CARRIED_AGGREGATORS = tuple(
    h.strip().lower()
    for h in os.environ.get("JOBFITR_AGGREGATORS", "adzuna.com").split(",")
    if h.strip()
)


_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</(li|p|div|h[1-6]|tr|ul|ol)\s*>", re.I)
_ENTITIES = (("&nbsp;", " "), ("&amp;", "&"), ("&#39;", "'"), ("&rsquo;", "'"),
             ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">"), ("&ndash;", "–"), ("&mdash;", "—"))


def plain(html: str) -> str:
    """Markup out, text in — measured over 400 live postings on 2026-08-19.

    55% of bodies carry raw HTML and markup is 12.9% of their characters (worst case 79%).
    Embedding that spends budget on `<span style="font-family: helvetica, arial, sans-serif">`
    and on Notion discussion GUIDs, and it does something worse than waste: every posting from
    the same ATS shares the same boilerplate markup, so it pulls unrelated jobs TOWARD each
    other in vector space.

    The tags are formatting, not data — 1,190 distinct <h*>/<strong> strings over 1,683
    occurrences, 82% of them appearing exactly once, top-30 covering 15%. There is no shared
    section vocabulary to parse into fields, so this is a cleanup, not a parser.

    </li> and friends become NEWLINES rather than spaces: 221 of 400 postings are bullet lists
    with a median of 22 bullets, and those bullets are the responsibilities and requirements.
    Collapsing them into one run of prose destroys the only sentence boundaries left.

    Markdown is not a factor: 6 of 400 bodies use `**bold**` and none use markdown headings,
    bullets, links, or code. The two `re.sub`s for it are belt-and-braces.

    THE REAL HOME FOR THIS IS job-radar's extraction, not here — this is jobfitr cleaning up
    after its dependency. Keep the function self-contained so it can move upstream unchanged.
    """
    if not html:
        return ""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = _BLOCK.sub("\n", t)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = _TAG.sub(" ", t)
    for a, b in _ENTITIES:
        t = t.replace(a, b)
    t = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", t)
    t = re.sub(r"(?m)^#{1,4}\s*", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def direct_to_employer(row: dict) -> bool:
    """True when the row may enter the store under the link policy.

    ── THE RULE ────────────────────────────────────────────────────────────────
    Positive evidence of directness (job-radar's `direct_apply`), OR a host named in
    `CARRIED_AGGREGATORS`. Everything else is dropped at intake.

    ── WHY AN ALLOWLIST AND NOT A DENYLIST OF MIDDLEMEN ────────────────────────
    The first draft of this listed 16 middleman hosts to drop. Review pointed out that this
    was a hand-maintained way of writing "direct_apply == 0 AND host is not adzuna", and that
    it rots in the DANGEROUS direction: add a source next month and its redirect links are
    carried silently until somebody notices. The allowlist fails CLOSED — a new source must
    earn its way in — and it states the policy in one sentence.

    ── WHAT THE FIRST VERSION OF THIS POLICY COST, and read this number first ───
    Dropping every `direct_apply != 1` row looked like **3,499 of 69,457 (5.04%)** of the
    POOL, and a review measured 4.46% mean loss across 57 synthetic profiles. Both numbers
    were true and both were the wrong measurement. Measured on real searches afterwards, it
    cost **92.6% of in-metro results across four metros** — `CLAUDE.md`'s own flagship local
    test went from 36 jobs led by Louisville/Watson/New Albany to **4, none of them local**.
    The corpus and the synthetic profiles skew remote/tech, so a concentrated loss in the
    segment this product is weakest at showed up as a 5% average.
    `CLAUDE.md` says it in bold — *to assess coverage, POST to /api/score, never SELECT from
    jobs* — and the pool percentage above is exactly the kind of number it is warning about.
    Keep both figures side by side so the next reader does not repeat the trade.

    ── WHY `_is_direct_apply` IS NOT THE ORACLE ────────────────────────────────
    The obvious "derive it per row from job-radar" move fails twice: it is positive-evidence
    only, so `www.adzuna.com` fails it exactly as the flag does (stricter, not looser); and it
    is WRONG on real employer pages — its company-token test rejects
    `www.buckner.org/current-nonprofit-job-openings/?gh_jid=...` because `_norm_name("Buckner
    International")` is not a substring of `wwwbucknerorg`. Measured, it rejected 11 genuine
    employer careers pages across those four searches while still killing local.
    """
    if not DIRECT_ONLY:
        return True
    if row.get("direct_apply"):
        return True
    host = urlparse(row.get("url") or "").netloc.lower()
    return any(host == a or host.endswith("." + a) for a in CARRIED_AGGREGATORS)


def servable_in_us(job: dict) -> bool:
    """True when a posting belongs on a US board. Two signals, both from the engine row.

    THE REMOTE EXEMPTION IS GONE, and deleting it is the whole fix. It used to return
    True for any row tagged remote, before the country was ever read — on the reasoning
    that a remote posting has no country by nature. That is true of the 3,729 rows whose
    country is BLANK, and those still pass on the country test below without needing a
    special case. What the exemption uniquely did was keep **577 rows that state a
    foreign country outright**: 'Enterprise Sales Director — Canada - Remote (Remote)',
    'Sales Director, DACH — Munich (Remote)', 'Strategist — Paris (Remote)'. CA 163 ·
    GB 100 · IN 38 · DE 33 · BR 30 · JP 28. Those are not location-independent jobs; they
    are remote WITHIN another country, and a US audience cannot take them.

    CURRENCY is the second signal, and unlike the subdivision test that was tried and
    removed for firing zero times, this one has teeth: 70 further rows state a salary in
    CAD/EUR/GBP/PHP/INR/PLN while carrying a blank country. Spot-checked, every one is
    'Canada (Remote)', 'Philippines (Remote)', 'Spain (Remote)' — including one labelled
    country=US whose location reads 'North America (Remote)' and pays in CAD. Nobody
    quotes a US salary in zloty.
    """
    if not US_ONLY:
        return True
    currency = _s(job.get("salary_currency")).upper()
    if currency and currency != "USD":
        return False
    country = _s(job.get("country")).upper()
    if country not in ("", "US"):
        return False

    # ── the blank-country half, which is where the leak lived ────────────────
    # 14,616 of 31,790 rows arrive with NO country, so "we don't know" and "this is
    # fine" were the same answer and ~5,600 foreign jobs reached a board the README
    # calls US-only. On a real remote user's 200 cards that was 20-27%; on the owner's
    # own live search, 11 of the top 20.
    #
    # TWO SIGNALS, STRUCTURED FIRST. `remote_areas` is job-radar 0.8.x's parse of the
    # boundary the posting actually STATED — an ISO alpha-2 list, or `US-TX`. Measured
    # on the corpus it identifies 3,398 rows kept today whose stated boundary is
    # entirely non-US, against 188 for the currency test above: 18x the reach, from the
    # engine rather than a regex, with no false-positive class of its own.
    #
    # An EMPTY list is not "no answer" — it is the posting saying "anywhere, worldwide",
    # which a US worker can take. Only a non-empty list naming no US entry is foreign.
    areas = job.get("remote_areas")
    if isinstance(areas, (list, tuple)) and len(areas) > 0:
        if not any(str(a).upper() == "US" or str(a).upper().startswith("US-") for a in areas):
            return False
        return True  # a stated US boundary is the best evidence there is

    # FALLBACK: read the location text. The engine refuses to guess here and is right to
    # ("a None here costs a filter; a wrong city is a permanently wrong row"), but its
    # curated 60-name country map cannot see Uruguay, Armenia, Slovakia or a bare foreign
    # city, and that is 2,343 rows the structured field misses. jobfitr owns the US-only
    # opinion, so jobfitr reads the text.
    return vocab.place_evidence(job.get("location")) != "foreign"


# ── tag derivation (rule-based only — no LLM, no fabrication) ──────────────────
# Remote is reconciled from the engine's stated `remote_type` first, falling back to
# job_radar's shared `remote_posting` predicate — see `_remote()`. salary_band is
# derived below. Seniority is no longer derived here at all: job_radar 0.7.0 parses
# titles properly (root/level/qualifiers) and reports what it finds, so a second,
# cruder regex in this file was a competing answer, not a safety net.
#
# The regex that used to live here bucketed lead|principal|staff|head of|vp|director|
# chief into ONE value ("lead") and returned "mid" for everything it did not match.
# Measured on the 21,495-row capture: that default fired on 23,781 of 39,597 live rows
# — a "Level: Mid" chip on jobs whose posting never said any such thing. Dropping it
# costs 1,940 rows where the regex found something the engine did not, and 9,956 rows
# of pure fabrication. That trade is the entire point.


# Hours per work-year / weeks / months, for putting every stated salary on ONE scale.
# Measured on the live pool: 2,064 yearly figures alongside 125 HOURLY (avg $47), 22
# monthly and one weekly, across five currencies.
_SALARY_PERIODS = {"year": 1, "month": 12, "week": 52, "hour": 2080, "day": 260}

# Below this, a bare number is not a yearly salary. A US full-time year at the federal
# minimum is ~$15,080, so this refuses hourly/daily figures that arrived without a
# period while accepting everything a job could actually pay for a year's work.
ANNUAL_FLOOR = 15_000

# K-notation, which the old fallback could not see at all: `[\d,]{3,}` needs three
# characters of digits-and-commas, so "$255k" contained NO match and the row banded as
# if it had no salary — 1,919 of the 5,203 salary strings in the capture write it this
# way, and they are the best-paying jobs on the board.
#
# THE 401k TRAP DOES NOT EXIST IN THIS FIELD, AND THAT WAS MEASURED, NOT ASSUMED. In a
# job BODY "401k match" would make this read $401,000. In the `salary` field the only
# two strings of that shape are "$401K – $445K" — a real $401,000 salary. The field is
# short and structured. Do NOT reuse this helper against body text without re-measuring.
_K_FIGURE = re.compile(r"([\d,]+(?:\.\d+)?)\s*[kK]\b")
_PLAIN_FIGURE = re.compile(r"[\d,]{3,}")


def annual_salary(job: dict) -> float | None:
    """A stated salary as an annual USD number, or None when it cannot be trusted.

    Two refusals, both measured. **Non-USD is dropped** rather than converted — the pool
    carries CAD/EUR/GBP/INR/JPY, and their raw numbers are what put a ¥15,500,000 and a
    ₹3,000,000 above every real dollar figure; converting needs a live FX rate that a
    salary filter does not justify. **`fixed` is dropped** because a project fee is not
    a salary. A blank period used to return None too, which threw away 154 rows carrying
    a perfectly good parsed USD figure — `('$148,000–$187,000', 148000.0, None)` arrived
    with a number, a currency and no period, and got nothing. A figure at or above
    ANNUAL_FLOOR is now read as annual: that is just under a US full-time year at the
    federal minimum, so nothing plausible as a salary is refused, and the one row in the
    capture that genuinely is not annual (`$33–$41`) still is.
    """
    lo = job.get("salary_min")
    if not isinstance(lo, (int, float)) or lo <= 0:
        return None
    cur = _s(job.get("salary_currency")).upper()
    if cur and cur != "USD":
        return None
    period = _s(job.get("salary_period")).lower()
    if not period:
        return float(lo) if lo >= ANNUAL_FLOOR else None
    mult = _SALARY_PERIODS.get(period)
    return float(lo) * mult if mult else None


def _first_figure(salary: str) -> float | None:
    """The BOTTOM of a stated salary range, read off the display string. None if none.

    Two things this gets right that the inline version it replaces did not.

    K-NOTATION. See `_K_FIGURE` — "$255k – $290k" used to contain no recognisable number
    at all and banded `under-50k`, which is where the best-paying jobs on the board were
    sitting, and which any salary floor then hid outright.

    THE MINIMUM, NOT THE MAXIMUM. This took `max(nums)` while `annual_salary` reads
    `salary_min`, so the same job got a different band depending only on whether the
    engine had parsed it — 1,113 strings in the capture disagree between the two paths.
    They have to agree, and the MINIMUM is the end that makes them agree correctly: the
    card's slider is a FLOOR filter ("pay me at least X"), so the number behind the chip
    has to be the floor too. A `$40k–$250k` posting wearing a `180k-plus` badge would be
    promising a figure the job does not guarantee.
    """
    if not salary:
        return None
    figures = [
        float(m.group(1).replace(",", "")) * 1000 for m in _K_FIGURE.finditer(salary)
    ]
    if figures:
        # THE LEADING K IS OFTEN IMPLIED: "$200-260K" means $200K-$260K, and reading it
        # literally puts the floor at $260,000 for a job whose floor is $200,000. So
        # once a string has established it is speaking in thousands, a bare number under
        # 1,000 in the SAME string is in thousands too. 26 distinct strings in the
        # capture are this exact shape and every one is a genuine range.
        #
        # A bare number of 1,000 or more is already a full figure and is kept as-is —
        # "$150,000 - 200K" has a floor of 150,000, and an earlier cut of this dropped
        # it entirely and answered 200,000.
        for n in _PLAIN_FIGURE.findall(_K_FIGURE.sub(" ", salary)):
            v = float(n.replace(",", ""))
            figures.append(v * 1000 if v < 1000 else v)
    else:
        figures = [float(n.replace(",", "")) for n in _PLAIN_FIGURE.findall(salary)]
    return min(figures) if figures else None


def _salary_band(job: dict) -> str:
    """Bucket a salary into a coarse band tag (empty when none/unknown).

    Prefers the engine's parsed figure ANNUALISED, and only falls back to scraping the
    display string when there is none. Reading the string alone is how `$200-400/hr` —
    a $416,000 job — ended up tagged `under-50k` and sitting in that drawer: the regex
    needs 3+ digits, so it saw no number at all in "$200-400/hr" and every hourly rate
    banded as if the hourly figure were the annual one.
    """
    figure = annual_salary(job)
    if figure is None:
        figure = _first_figure(_s(job.get("salary")))
    if figure is None:
        return ""
    if figure < 50_000:
        return "under-50k"
    if figure < 80_000:
        return "50-80k"
    if figure < 120_000:
        return "80-120k"
    if figure < 180_000:
        return "120-180k"
    return "180k-plus"


REMOTE_STATES = ("remote", "hybrid", "onsite")


def _remote(
    job: dict, title: str, loc: str, text: str
) -> tuple[str | None, str | None]:
    """Reconcile the work arrangement into (state, basis). FOUR states, not two.

    The rule that matters: `remote_posting()` may only ever assert **remote**. It is a
    phrase detector, so a False from it means "no evidence of remote" — it has never
    looked for evidence of onsite and cannot supply any. Reading that False as "onsite"
    is what put an On-site chip on 12,623 rows (58.7% of the corpus) that never said so;
    only 1,550 rows are actually stated-onsite.

    So NULL is a real answer here and the honest one for the majority. It renders as
    "unspecified" and stays on the board — a job that didn't state its arrangement is
    not a job you want hidden from someone who might take it either way.

    Reconciled over the capture: 55.0% NULL · 29.4% remote · 8.4% hybrid · 7.2% onsite,
    replacing a flat 64.1% onsite / 35.9% remote that was 3/4 guess. Those are the
    numbers BEFORE the US/USD intake filter, which is the layer this function feeds; what
    the store actually ends up holding is 57.2 / 32.0 / 7.0 / 3.7, because a foreign
    posting is likelier to be onsite than an American one.
    """
    stated = _s(job.get("remote_type")).lower()
    if stated in REMOTE_STATES:
        # The engine looked at a real field; its basis says how far to trust it
        # ('stated' > 'board' > 'location' > 'text').
        return stated, _s(job.get("remote_basis")) or "stated"
    if remote_posting(title, loc, text):
        return "remote", "derived"
    return None, None


def _s(v) -> str:
    """Coerce any raw field to a clean string — dirty source rows sometimes hand us a
    list (e.g. category) or None where the schema needs TEXT; a list can't bind."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return _s(v[0]) if v else ""
    return str(v)


def _json_or_null(v) -> str | None:
    """JSON-encode a scope column, preserving the difference between NULL and '[]'.

    `_json` below deliberately collapses an empty list to NULL, which is right for the
    four container columns it serves — an empty `tags` and an absent `tags` mean the same
    thing there. It is WRONG for the remote-scope columns, where job-radar 0.8.0 made the
    distinction load-bearing:

        None -> the posting said nothing about where you may sit
        []   -> the posting STATED it is open anywhere, worldwide

    `[]` satisfies every scope filter and NULL satisfies none, so collapsing them either
    hides the most permissive postings in the feed or admits bounded ones into a filter
    meant to exclude them. Upstream measured that a third of one live feed states
    worldwide with an empty array.
    """
    if v is None:
        return None
    if not isinstance(v, (list, tuple, dict)):
        return None
    return json.dumps(list(v) if isinstance(v, tuple) else v, separators=(",", ":"))


def _json(v) -> str | None:
    """JSON-encode a container column, or NULL. Empty is NULL, never the string "null".

    The four container fields (`title_qualifiers`, `locations`, `tags`, `source_extra`)
    arrive as real lists/dicts and are absent on 43-95% of rows. `json.dumps(None)`
    returns the four-character string "null", which reads as present to every SQL
    test that matters (`IS NOT NULL`, a fill-rate COUNT) — so the empty case has to
    short-circuit before the encoder, not after."""
    if v is None or v == [] or v == {}:
        return None
    return json.dumps(v, separators=(",", ":"))


def _int_bool(v) -> int | None:
    """A bool column as 0/1, preserving the difference between False and absent."""
    return None if v is None else int(bool(v))


def normalize_job(job: dict) -> dict:
    """Map a raw harvest/live row onto the store's row shape, deriving facet tags.

    Two kinds of column live in the returned dict, and the distinction is the whole
    design:

      PASS-THROUGH — the engine's value under the engine's own name, untransformed
      except JSON-encoding a container. job_radar 0.7.0 is the fidelity layer; it
      reports what the source said and it is not this function's business to argue.

      DERIVED — jobfitr's opinion: `remote`, `seniority`, `salary_band`, and the
      `category` fallback. Rule-based only, never invented.

    THREE OF THOSE DERIVATIONS ARE KNOWN-WRONG AND DELIBERATELY UNCHANGED HERE.
    0.7.0 can answer all three better, but reconciling them changes what rows the
    board shows, so it is its own measured commit (step 2 of _private/PLAN-db-v2.md):

      `remote`     — binary, from prose. 12,623 rows (58.7%) are labelled onsite on
                     no evidence at all; only 1,550 are stated-onsite. The engine's
                     `remote_type` carries a real four-state answer on 25% of rows.
      `seniority`  — from the title, so it is never NULL: 23,781 rows say "mid"
                     meaning "the title didn't say".
      `category`   — still falls back to the deprecated `department` alias, which is
                     what takes the value count to 2,403 instead of 487.

    Their `_basis` columns are written honestly for what the value ACTUALLY is today,
    so nothing in the store is a lie in the meantime.
    """
    src = _s(job.get("source")) or _s(job.get("sources"))
    loc = _s(job.get("location"))
    # A strip of " (Remote)" for adzuna lived here, on the belief that job_radar
    # appended it to every Adzuna location. It doesn't, and measurably never did in
    # this corpus: 0 of 7,308 adzuna rows end with it, so the branch never fired.
    # job_radar appends " (Remote)" for ashby/smartrecruiters/workable only, where it
    # means the job really is remote — 2,758 rows carry it and they are kept on
    # purpose. Adzuna's remote signal is `location.area == ["US"]`, which the adapter
    # discards upstream; that is a job-radar fix, not something to paper over here.
    salary = _s(job.get("salary"))
    text = _s(job.get("text"))
    if len(text) > BODY_CAP:
        text = text[:BODY_CAP]
    title = _s(job.get("title"))
    remote, remote_basis = _remote(job, title, loc, text)
    # A basis without a value is a claim with nothing behind it. It happens: 60 rows
    # arrive with seniority "Not Applicable" or "Any" under basis 'stated', which the
    # vocabulary correctly refuses to map — leaving a column asserting that a source
    # stated a level we do not have. The two travel together or not at all.
    seniority = vocab.seniority(job.get("seniority"))
    seniority_basis = (_s(job.get("seniority_basis")) or None) if seniority else None
    return {
        # ── derived: jobfitr's opinion (see the docstring) ────────────────────
        "remote": remote,
        "remote_basis": remote_basis,
        # The engine's value, put through jobfitr's ladder. NULL when the source did
        # not say — 55.3% of rows, and the honest answer for every one of them.
        "seniority": seniority,
        "seniority_basis": seniority_basis,
        # `category` ONLY. The deprecated `department` alias used to be preferred
        # here, and on ATS boards it is not a category at all — measured, it is
        # byte-identical to `team` on all 16,235 greenhouse/ashby/lever rows, i.e. the
        # employer's own org-unit name. Mapping those onto a job taxonomy looked like
        # +17.7% coverage and was mostly wrong: its single largest effect was filing
        # 895 rows of "Senior Software Engineer, Backend" under Science and
        # Engineering, because their department is called "Engineering".
        "category": vocab.category(job.get("category")),
        "salary_band": _salary_band(job),
        "body": text,
        # ── pass-through: the engine's value, the engine's name ───────────────
        "url": _s(job.get("url")),
        "title": title,
        "title_root": _s(job.get("title_root")),
        "title_level": _s(job.get("title_level")),
        "title_qualifiers": _json(job.get("title_qualifiers")),
        "company": _s(job.get("company")),
        "team": _s(job.get("team")),
        "location": loc,
        "city": _s(job.get("city")),
        # Normalised to a USPS code, or NULL. The one field on this list that is not
        # pure pass-through, because the raw value arrives in three vocabularies at
        # once (CA / Ohio / Ontario) and a filter drawer needs a closed set.
        "state": vocab.us_state(job.get("state")),
        "country": _s(job.get("country")),
        "locations": _json(job.get("locations")),
        # THREE FIELDS, EACH MEANING ONE THING — they replaced `remote_region`, which
        # held ISO country codes, ISO subdivisions, business regions and sentinels in one
        # column at once; measured upstream, 1,162 of 1,168 adapter-written values (99.5%)
        # fell outside the vocabulary its own docstring claimed.
        #
        # NULL AND '[]' ARE DIFFERENT ANSWERS and the column must preserve that:
        #   NULL -> the posting said nothing about where you may sit
        #   '[]' -> the posting STATED it is open anywhere (worldwide)
        # `[]` satisfies every scope filter; NULL does not. Collapsing them either drops
        # the most permissive postings in the feed or admits bounded ones into a filter
        # meant to exclude them — the same unearned assertion the facet rules exist to
        # stop. `_json_or_null` is what keeps them apart; plain `_json` would write '[]'
        # for both.
        "remote_areas": _json_or_null(job.get("remote_areas")),
        "remote_regions": _json_or_null(job.get("remote_regions")),
        "remote_scope_raw": _s(job.get("remote_scope_raw")),
        "tags": _json(job.get("tags")),
        "employment_type": _s(job.get("employment_type")),
        "employment_type_raw": _s(job.get("employment_type_raw")),
        "salary": salary,
        "salary_min": job.get("salary_min"),
        "salary_max": job.get("salary_max"),
        "salary_currency": _s(job.get("salary_currency")),
        "salary_period": _s(job.get("salary_period")),
        "salary_basis": _s(job.get("salary_basis")),
        # Adzuna's MODEL guess. Kept in its own pair of columns on purpose: merged
        # into salary_min it would render as a committed figure on the card.
        "salary_estimated_min": job.get("salary_estimated_min"),
        "salary_estimated_max": job.get("salary_estimated_max"),
        "source": src,
        "source_extra": _json(job.get("source_extra")),
        "direct_apply": _int_bool(job.get("direct_apply")),
        "posted": _s(job.get("posted")),
        "expires": _s(job.get("expires")),
    }


# Every column normalize_job returns — i.e. every column except the two clocks.
# Built from the function's own output rather than typed out a second time: the
# INSERT binds by NAME, so a column listed in SQL that normalize_job stopped
# returning raises at request time, not import time, and it would take down the
# nightly harvest and live search together. Deriving the list makes that class of
# drift impossible instead of merely tested. `url` is the PRIMARY KEY, so it is
# inserted but never in the update set.
#
# THE ONE WAY TO BREAK THIS: make a key CONDITIONAL. `normalize_job` must return the
# same key set for every input — it returns one dict literal, which is what makes
# `normalize_job({})` a schema declaration. An `if x: d["foo"] = …` would shrink the
# derived list for that row's shape and the column would quietly stop being written,
# with no error anywhere.
_ROW_COLUMNS: tuple[str, ...] = tuple(normalize_job({}))
_UPDATE_COLUMNS: tuple[str, ...] = tuple(c for c in _ROW_COLUMNS if c != "url")

# fetched_at is FIRST-SEEN and deliberately not refreshed; last_seen is what the
# eviction clock reads, so an actively re-fetched job never ages out.
_UPSERT_SQL = (
    f"INSERT INTO jobs({','.join(_ROW_COLUMNS)},fetched_at,last_seen) "
    f"VALUES({','.join(':' + c for c in _ROW_COLUMNS)},:now,:now) "
    "ON CONFLICT(url) DO UPDATE SET last_seen=:now,"
    + ",".join(f"{c}=excluded.{c}" for c in _UPDATE_COLUMNS)
)


# ── connection + schema ───────────────────────────────────────────────────────
@contextmanager
def _conn(path: str | None = None):
    c = sqlite3.connect(path or DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=8000")
    try:
        yield c
        c.commit()
    finally:
        c.close()


_SCHEMA = """
-- v2 (SCHEMA_VERSION below). job_radar 0.7.0 emits a 39-field record; v1 kept 13 of
-- them. Every column added here carries the engine's value under the engine's own
-- name, untransformed — the store's job is fidelity. jobfitr's OPINIONS (which of
-- four remote states a row really is, which of 487 category strings is a facet,
-- whether an unstated seniority is "mid" or unknown) are applied in normalize_job,
-- deliberately downstream of this table.
--
-- NOTE `CREATE TABLE IF NOT EXISTS`: shipping a changed DDL against an existing
-- jobs.db is a SILENT NO-OP. The old table survives and the next upsert dies with
-- `no such column: title_root`. That is why init() carries a version guard and why
-- scripts/rebuild_store.py exists — the store is a cache with 60-day eviction, so a
-- rebuild + re-harvest (~4 min) is the migration.
CREATE TABLE IF NOT EXISTS jobs(
  url TEXT PRIMARY KEY,

  -- identity, and the title decomposed ─────────────────────────────
  title TEXT,              -- VERBATIM, as the employer wrote it. Never parsed.
  title_root TEXT,         -- "Application Security Engineer" — 100% fill. What matching should use.
  title_level TEXT,        -- "II" / "III" when the title carries one
  title_qualifiers TEXT,   -- JSON array: ["remote","southeast"] — the decoration
  company TEXT,
  team TEXT,               -- the company's own group. DISPLAY ONLY, never a facet (thousands of values).

  -- where ──────────────────────────────────────────────────────────
  location TEXT,           -- VERBATIM. The audit trail when a parse is wrong.
  city TEXT, state TEXT, country TEXT,   -- indexed below; `state` is US-only by construction
  locations TEXT,          -- JSON array. 9.4% of rows carry more than one location.
  remote TEXT,             -- jobfitr's decided state. Still binary in v2; four states land in step 2.
  -- 'stated' | 'board' | 'location' | 'title' | 'text' | the engine's closed set, plus
  -- jobfitr's own 'derived' (see _remote) | NULL. `title` arrived with job-radar 0.8.0
  -- and this comment did not, which is the whole reason a closed vocabulary is written
  -- down: 462 rows carry it, from the adapters that send no structured remote field.
  remote_basis TEXT,
  remote_areas TEXT,       -- JSON list of ISO alpha-2 / US-XX. NULL = unstated, '[]' = worldwide
  remote_regions TEXT,     -- JSON list from a closed token set (EMEA, APAC, ...). NEVER a country
  remote_scope_raw TEXT,   -- the vendor's own words, verbatim
  body TEXT,

  -- the job ────────────────────────────────────────────────────────
  category TEXT,           -- the KIND of work
  tags TEXT,               -- JSON array of source-extracted skills — boost evidence for free
  seniority TEXT,
  seniority_basis TEXT,    -- 'stated' | 'title' | NULL. NULL must mean unknown, never "mid".
  employment_type TEXT,    -- the engine's 7-value enum
  employment_type_raw TEXT,-- what the vendor actually said (32 spellings)

  -- money ──────────────────────────────────────────────────────────
  salary TEXT,             -- verbatim, for the card
  salary_min REAL, salary_max REAL, salary_currency TEXT, salary_period TEXT,
  salary_basis TEXT,
  salary_estimated_min REAL, salary_estimated_max REAL,  -- a MODEL's guess (Adzuna). NEVER merged into salary_min.
  salary_band TEXT,        -- jobfitr-derived, unchanged

  -- provenance ─────────────────────────────────────────────────────
  source TEXT,
  source_extra TEXT,       -- JSON: whatever else the vendor sent. The debugging trail when a field turns out wrong.
  direct_apply INTEGER,    -- 1 = the employer's own link, not an aggregator
  posted TEXT,
  expires TEXT,            -- kept because a CLOSED posting is not recoverable by re-harvesting
  fetched_at REAL, last_seen REAL);
CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen);
CREATE INDEX IF NOT EXISTS idx_jobs_geo ON jobs(country, state, city);

-- external-content FTS5 over jobs.rowid: title/location/body are searchable, the
-- rest live in `jobs`. Kept in sync by the triggers below.
--
-- `title_root` is deliberately NOT indexed here, despite being the field this whole
-- schema exists for. Measured over 57 profiles: adding it buys 54 candidate rows
-- (+0.04%), and matching on it ALONE loses 7.8% — the phrases people search live in
-- the part the root strips ("application security" survives in 29 titles, 15 roots).
-- NEAR(...,3) already finds the root inside the full title. It is a scoring input
-- (see the max() rule), not a retrieval one.
CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
  title, location, body, content='jobs', content_rowid='rowid',
  tokenize='porter unicode61');

-- All three triggers name (title, location, body) EXPLICITLY, so widening `jobs`
-- cannot shift them — that is the whole reason they are written out rather than
-- relying on column order. The list must stay identical to jobs_fts's; a mismatch
-- does not error, it silently stops populating the index and every search returns
-- nothing. test_store.py asserts the index is non-empty after an insert and shrinks
-- on a delete, which is the only cheap way to catch that.
CREATE TRIGGER IF NOT EXISTS jobs_ai AFTER INSERT ON jobs BEGIN
  INSERT INTO jobs_fts(rowid, title, location, body)
    VALUES (new.rowid, new.title, new.location, new.body);
END;
CREATE TRIGGER IF NOT EXISTS jobs_ad AFTER DELETE ON jobs BEGIN
  INSERT INTO jobs_fts(jobs_fts, rowid, title, location, body)
    VALUES ('delete', old.rowid, old.title, old.location, old.body);
END;
CREATE TRIGGER IF NOT EXISTS jobs_au AFTER UPDATE ON jobs BEGIN
  INSERT INTO jobs_fts(jobs_fts, rowid, title, location, body)
    VALUES ('delete', old.rowid, old.title, old.location, old.body);
  INSERT INTO jobs_fts(rowid, title, location, body)
    VALUES (new.rowid, new.title, new.location, new.body);
END;

CREATE TABLE IF NOT EXISTS searches(key TEXT PRIMARY KEY, fetched_at REAL);

CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

-- The company->ATS resolution ledger. One row per distinct employer seen in `jobs`,
-- recording whether we found a live ATS board for it. Two things make this pay:
--
--   1. It CACHES THE NEGATIVE. 'checked, nothing found' is a real answer worth
--      storing — without it, every run re-probes the same ~3k dead-end employers
--      (federal agencies, hospitals, staffing firms) forever. With it, a run only
--      probes companies it has never seen, so resolution can ride along with every
--      harvest instead of needing a separate monthly job.
--   2. It KEEPS THE EVIDENCE. A wrong slug is worse than no slug — it is sticky and
--      silent, and would file a stranger's postings under this company forever
--      (measured: 'Capital One' -> the unrelated `capital` board). Storing the
--      variant that matched, the role count that proved it, and when, makes a bad
--      resolution auditable and reversible instead of folklore.
--
-- Keyed on a NORMALIZED name, not the raw string. Measured on the live store: 43
-- collision groups across 3,162 company strings ('Westhab Inc.' / 'Westhab' /
-- 'Westhab, Inc.'; 'Celsius' / 'CELSIUS'). Keying on the raw string gave each
-- spelling its own row, its own probe budget, and — worst — its own independent
-- answer, so one employer could be 'resolved' and 'unresolved' at the same time.
--
-- status:
--   resolved   — ats+slug confirmed live AND (where checkable) confirmed to belong
--   unresolved — probed, nothing found; retried after UNRESOLVED_RETRY_DAYS
--   dead       — the board answers but refuses us (e.g. a 403 Workday tenant).
--                Distinct from unresolved so it is never retried on a schedule;
--                retrying a deliberate refusal nightly is both futile and rude.
--   covered    — name-guessing cannot help: every slug this company's name would
--                generate is ALREADY held by a resolved board. Terminal, and it binds
--                nothing — it records that the guess space is exhausted, not that we
--                believe the company owns that board. Without it these names sat in the
--                retry set forever (37 of every 176-name batch once the committed board
--                universe landed) and, because the queue is ordered by job count, they
--                crowded out never-checked employers permanently.
CREATE TABLE IF NOT EXISTS companies(
  name_key TEXT PRIMARY KEY,      -- normalized: lowercased, depunctuated, suffix-free
  name TEXT NOT NULL,             -- the raw string as jobs.company holds it (display)
  ats TEXT, slug TEXT,            -- the resolved board (NULL when unresolved)
  host TEXT, site TEXT,           -- workday's extra two-thirds of its key
  status TEXT NOT NULL,
  roles INTEGER,                  -- roles the board returned when verified
  matched_variant TEXT,           -- WHICH string manipulation won — the audit trail
  checked_at REAL, attempts INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(status, checked_at);
"""


# SCHEMA_VERSION is declared in the config block at the top of this file, because the store's
# FILENAME derives from it — see DB_PATH. Bumping it there is what makes a schema change write
# a new file instead of colliding with the one production is serving.
_SCHEMA_VERSION_KEY = "schema_version"


class StaleSchemaError(RuntimeError):
    """A jobs.db built by an older schema. Not recoverable in place — see init()."""


def init(path: str | None = None) -> None:
    """Create the schema if missing, then pull in the current jobs.json snapshot.

    Raises StaleSchemaError on a store built by an older schema. This check is the
    only thing standing between a version bump and a silent failure: the DDL is
    `CREATE TABLE IF NOT EXISTS`, so running new code against an old jobs.db does
    nothing at all — the old table survives, init() reports success, and the damage
    surfaces later as `no such column` inside the nightly harvest. Failing here,
    loudly, with the command that fixes it, costs one restart instead of a night.
    """
    _check_schema_version(path)  # BEFORE executescript — see the function
    with _conn(path) as c:
        c.executescript(_SCHEMA)
        c.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING",
            (_SCHEMA_VERSION_KEY, str(SCHEMA_VERSION)),
        )
    sync_snapshot(path)


def _check_schema_version(path: str | None) -> None:
    """Raise StaleSchemaError if this store predates the current schema.

    This runs BEFORE `executescript`, not after, and the ordering is the whole
    point. `CREATE TABLE IF NOT EXISTS` no-ops against an old `jobs`, but the
    `CREATE INDEX ... ON jobs(country, state, city)` that follows it does NOT — it
    raises `no such column: country` from inside executescript, three statements
    deep, with no mention of what is actually wrong or how to fix it. Checking
    first turns that into a sentence naming the file and the command.
    """
    with _conn(path) as c:
        tables = {
            r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "jobs" not in tables:
            return  # a fresh file; the schema script is about to create it
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
        seen = None
        if "meta" in tables:
            row = c.execute(
                "SELECT value FROM meta WHERE key=?", (_SCHEMA_VERSION_KEY,)
            ).fetchone()
            seen = row[0] if row else None
        rows = c.execute("SELECT count(*) FROM jobs").fetchone()[0]

    fix = "    python scripts/rebuild_store.py" + (f" --db {path}" if path else "")
    if seen is None:
        # An unmarked store is only stale if it is actually shaped like v1. An
        # unmarked EMPTY v2 file (a half-initialized tmp DB) is fine to adopt.
        if cols and not set(_ROW_COLUMNS) <= cols:
            missing = ", ".join(sorted(set(_ROW_COLUMNS) - cols))
            raise StaleSchemaError(
                f"{path or DB_PATH} predates schema v{SCHEMA_VERSION} "
                f"({rows:,} rows; missing {missing}). The store is a cache with "
                f"60-day eviction — rebuild it rather than migrating:\n{fix}"
            )
    elif int(seen) != SCHEMA_VERSION:
        raise StaleSchemaError(
            f"{path or DB_PATH} is schema v{seen}, this code expects "
            f"v{SCHEMA_VERSION}. Rebuild:\n{fix}"
        )


def _meta_get(key: str, path: str | None = None) -> str | None:
    with _conn(path) as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(key: str, value: str, path: str | None = None) -> None:
    with _conn(path) as c:
        c.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# ── the daily live-fetch tally (Adzuna/USAJOBS load-shed) ─────────────────────
# Persisted here rather than in the web process's memory so a restart or crash-loop
# cannot silently zero it — that reset was what let the daily ceiling fail to bound
# real API usage. Stored as "YYYY-MM-DD:count" in a single meta row; a new day reads
# as 0 without needing a sweep. Per-slot (the server's own store), which is enough:
# only the ACTIVE slot serves traffic, so only it fetches.
_FETCH_USAGE_KEY = "live_fetch_usage"


def live_fetch_count(path: str | None = None) -> int:
    """Live fetches recorded so far TODAY (0 on a fresh day)."""
    raw = _meta_get(_FETCH_USAGE_KEY, path)
    if raw and ":" in raw:
        day, cnt = raw.rsplit(":", 1)
        if day == datetime.now(_ET).date().isoformat():
            try:
                return int(cnt)
            except ValueError:
                return 0
    return 0


def note_live_fetch(path: str | None = None) -> int:
    """Record one live fetch against today's tally; return the new count."""
    today = datetime.now(_ET).date().isoformat()
    n = live_fetch_count(path) + 1
    _meta_set(_FETCH_USAGE_KEY, f"{today}:{n}", path)
    return n


# ── the baseline inflow: import the harvest snapshot whenever it's newer ──────
# The nightly harvest writes ONE shared jobs.json; every slot's store pulls from it.
# Gating on the file's mtime (not "is the table empty?") is what makes a long-lived
# slot keep up: the old import-once rule meant a slot built yesterday served that
# day's pool forever, because its table was never empty again. Mtime-gated + an
# upsert that dedups by url makes re-importing cheap and idempotent, and a brand-new
# slot still seeds itself on first init.
SNAPSHOT_MTIME_KEY = "jobs_json_mtime"


class SnapshotTruncated(RuntimeError):
    """jobs.json ended mid-value. Distinct from "unparseable" on purpose.

    A truncated file yields SOME rows before it fails, and importing a prefix would look
    exactly like a successful small harvest — the silent-zero shape this codebase keeps
    getting bitten by. So the streamer raises and `sync_snapshot` declines to record the
    mtime, which means the next call retries instead of accepting the prefix forever.
    """


class SnapshotKeyless(RuntimeError):
    """A parseable document with no `jobs` ARRAY in it.

    THIS IS THE ONE THAT MATTERED, and an earlier version got the severity backwards by
    returning quietly here. Review found the consequence: a quiet zero means `sync_snapshot`
    reaches its success path and RECORDS THE MTIME, so the import is never retried — a
    PERMANENT frozen pool, not a transient miss, with `/api/health` still reading fine.
    Meanwhile the truncation case it did guard is largely closed already by the atomic
    `os.replace` in snapshot.py, so the error handling was inverted relative to the real
    risk.

    An explicitly EMPTY array is NOT this: `"jobs": []` is a real answer and imports zero
    rows. This is "the shape is wrong", which is always a bug worth shouting about.
    """


class _JsonStream:
    """Just enough buffered JSON plumbing to walk one big document without loading it.

    Shared by the two readers below so the parsing rules live in ONE place. The whole point
    is that `meta` is small and can be materialized, while `jobs` must be streamed — but both
    have to agree on how the top-level object is walked, or they will disagree about where it
    is, which is the class of bug this replaced.
    """

    CHUNK = 1 << 16

    def __init__(self, fp, name: str):
        self.fp, self.name, self.buf, self.pos = fp, name, "", 0
        self.dec = json.JSONDecoder()

    def _more(self) -> bool:
        chunk = self.fp.read(self.CHUNK)
        if not chunk:
            return False
        self.buf += chunk
        return True

    def skip(self, chars: str) -> bool:
        """Advance past any of `chars`, refilling as needed. False at EOF."""
        while True:
            while self.pos < len(self.buf) and self.buf[self.pos] in chars:
                self.pos += 1
            if self.pos < len(self.buf):
                return True
            self.buf, self.pos = self.buf[self.pos :], 0
            if not self._more():
                return False

    def decode(self):
        """raw_decode one value at the cursor, growing the buffer until it fits."""
        while True:
            try:
                val, end = self.dec.raw_decode(self.buf, self.pos)
                self.pos = end
                return val
            except ValueError:
                if not self._more():
                    raise SnapshotTruncated(
                        f"{self.name} ended mid-value at offset ~{self.pos}"
                    ) from None

    def trim(self) -> None:
        if self.pos > (1 << 20):
            self.buf, self.pos = self.buf[self.pos :], 0

    def _need(self) -> None:
        """Ensure one more character is available, trimming what is behind the cursor."""
        if self.pos < len(self.buf):
            return
        self.buf, self.pos = self.buf[self.pos :], 0
        if not self._more():
            raise SnapshotTruncated(f"{self.name} ended mid-value")

    def skip_value(self) -> None:
        """Advance past one JSON value WITHOUT parsing it.

        Why this is not `decode()`: `raw_decode` re-parses a value from its START on every
        buffer refill, and `self.buf += chunk` reallocates alongside — so skipping a large
        value that way is super-quadratic. Measured on a meta-LAST file, where the value to
        skip IS the whole jobs array: 20 ms at 0.9 MB, 84 ms at 1.9 MB, 427 ms at 3.7 MB,
        **2,447 ms at 7.4 MB** — 5.7x per doubling where linear is 2.0x, and peaking at 2.60x
        the file, i.e. exactly the `json.loads` cost the streamer was written to remove.
        Projected to a 380 MB snapshot that is hours, on `/api/health`, which is strictly
        worse than the 1,168 MB spike it replaced.

        HONEST SCOPE, after the fix that followed: `snapshot_meta` now answers from a bounded
        prefix, so in practice this only ever crosses the SMALL meta block. The quadratic it
        was written to remove is therefore no longer reachable by any caller, and mutation
        confirms the old `decode()` still passes every performance test. What earns its keep is
        correctness at the refill boundary — a scan that miscounts depth would start reading
        jobs from the wrong offset — so that is what the tests assert.

        A depth counter over braces and brackets, string- and escape-aware so a `}` inside a
        string cannot close a level. Scalars go through `decode()` — they are small by
        construction and the retry cost does not bite.
        """
        if not self.skip(" \t\r\n"):
            raise SnapshotTruncated(f"{self.name} ended where a value was expected")
        ch = self.buf[self.pos]
        if ch not in "{[\"":
            self.decode()  # number, true, false, null — tiny
            return
        if ch == '"':
            self.pos += 1
            esc = False
            while True:
                self._need()
                c = self.buf[self.pos]
                self.pos += 1
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    return
                self.trim()
        depth = 0
        in_str = False
        esc = False
        while True:
            self._need()
            c = self.buf[self.pos]
            self.pos += 1
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return
            self.trim()

    def enter_object(self) -> bool:
        """Consume the opening `{`. False if the document is empty."""
        if not self.skip(" \t\r\n\ufeff"):
            return False
        if self.buf[self.pos] != "{":
            raise SnapshotKeyless(f"{self.name} is not a JSON object")
        self.pos += 1
        return True

    def next_key(self) -> str | None:
        """The next key of the current object, or None at its closing `}`."""
        if not self.skip(" \t\r\n,"):
            raise SnapshotTruncated(f"{self.name} ended inside the top-level object")
        if self.buf[self.pos] == "}":
            return None
        key = self.decode()
        if not self.skip(" \t\r\n:"):
            raise SnapshotTruncated(f"{self.name} ended after key {key!r}")
        return key


def _walk_to(st: _JsonStream, want: str) -> bool:
    """Advance to the VALUE of top-level key `want`. False if the object has no such key.

    Walks keys STRUCTURALLY and never string-searches. An earlier version located the array
    with `buf.index('"jobs"')`, and review broke that four ways with no escaping needed: a
    string value that is exactly `jobs`, a source id named `jobs`, and a NESTED `jobs` key —
    which in the worst case yielded `[1, 2, 3]` as "jobs" and killed the background sync
    thread with `AttributeError: 'int' object has no attribute 'get'`.
    (The escaped-quote vector I had assumed cannot fire: JSON writes an interior quote as
    backslash-quote, so it never contains the bare substring.)
    """
    if not st.enter_object():
        return False
    while True:
        key = st.next_key()
        if key is None:
            return False
        if key == want:
            return True
        # SKIP, do not decode. `decode()` here was super-quadratic on a large skipped value
        # (the meta-last case, where the value is the whole jobs array) — see skip_value.
        st.skip_value()


def _iter_snapshot_jobs(p: Path) -> Iterator[dict]:
    """Yield each job from a jobs.json WITHOUT holding the file in memory.

    This used to be `json.loads(p.read_text())`, which holds the whole file three ways at
    once: raw bytes, then a decoded str, then the Python objects. Measured with tracemalloc,
    that parse peaks at **~2.6x the file size**, against **0.20x** for this streamer — a 13x
    reduction. And the read runs inside the LIVE WEB PROCESS (`server.py` calls sync_snapshot
    whenever the snapshot is newer), so on a 777 MB file the spike would be ~2 GB landing on
    the process that is serving traffic.

    Do not confuse this with the harvest's own 2,876 MB peak on a 363 MB snapshot (7.9x).
    That was the WRITE side — the engine's rows, a full copy in `jobs`, and one giant
    `json.dumps` string, all resident together — and it is fixed separately in `snapshot.py`.
    """
    with p.open("r", encoding="utf-8") as fp:
        st = _JsonStream(fp, str(p))
        if not _walk_to(st, "jobs"):
            raise SnapshotKeyless(f"{p} has no 'jobs' key")
        if st.buf[st.pos] != "[":
            raise SnapshotKeyless(f"{p}: 'jobs' is not an array")
        st.pos += 1
        while True:
            if not st.skip(" \t\r\n,"):
                raise SnapshotTruncated(f"{p} ended inside the jobs array")
            if st.buf[st.pos] == "]":
                return  # clean end — nothing after the array is needed
            yield st.decode()
            st.trim()


# How much of the file `snapshot_meta` will read before giving up on the fast path. The
# writer puts `meta` first — verified on the live file, 990 bytes ahead of the jobs key — and
# the block is a handful of scalars plus one error string per failing board (16 today, ~80
# even at 5,400 boards, so ~5 KB). 1 MB is therefore enormous headroom for the real layout
# while keeping the read O(1) instead of O(file).
# Two attempts before giving up. 1 MB covers the real layout with ~2.7x headroom over the
# worst realistic meta (~0.392 MB at one error per board across a fully-resolved universe);
# 16 MB pushes the overflow point from ~14,000 error lines to ~220,000, past anything
# plausible. Both are bounded by the PREFIX rather than by the file, which is the property
# that matters — the fallback below is bounded by the file.
_META_PREFIX_TRIES = (1 << 20, 1 << 24)


def snapshot_meta(p: Path) -> dict:
    """The snapshot's `meta` block alone, without materializing `jobs`.

    `/api/health` needs five numbers out of a 363 MB file. It used to get them via
    `snapshot.load_snapshot`, which parsed the WHOLE document and cached it in a module-level
    dict permanently — measured `[live prod]`, one call took a web process from 27 MB to
    **1,168 MB**, and with both slots warm that was **3,447 MB of 7,941 MB** held as two
    copies of the same document, the idle slot included.

    That cache, not the harvest, was the real ceiling on board discovery.
    """
    # FAST PATH: `meta` is written first, so it lives in the first few KB. Read a bounded
    # prefix and parse it there — O(1) in the file size, and it cannot hang.
    #
    # This exists because the general "walk past whatever precedes meta" path is only as fast
    # as a Python loop over characters: ~7 MB/s, so ~50 s on a 380 MB snapshot. That is fine
    # for a one-off but not for `/api/health`, which calls this on the first request after
    # every harvest (later calls hit the mtime cache). An earlier version was worse still —
    # `decode()` re-parses from a value's start on every refill, which made skipping the jobs
    # array super-quadratic: 2,447 ms at 7.4 MB, hours projected. Review caught it.
    for limit in _META_PREFIX_TRIES:
        with p.open("r", encoding="utf-8") as fp:
            head = fp.read(limit)
        try:
            st = _JsonStream(io.StringIO(head), str(p))
            if _walk_to(st, "meta"):
                meta = st.decode()
                if isinstance(meta, dict):
                    return meta
        except (SnapshotTruncated, SnapshotKeyless, ValueError):
            pass  # not in this prefix — try a bigger one, then fall through
        if len(head) < limit:
            break  # we already read the whole file; a bigger prefix cannot help

    # SLOW PATH, for a layout the writer does not produce: `meta` after `jobs`, or an older
    # rollback artifact. Correctness over speed, once, then the caller's mtime cache holds it.
    # Deliberately a plain parse rather than the streaming walk: at this point we already know
    # we have to cross the whole document, and json's C parser does it ~75x faster than a
    # character loop.
    #
    # BUT IT IS LOUD, because it costs ~3x the file in peak memory — on a 380 MB snapshot,
    # ~1.15 GB, which is the exact shape the streaming work removed. Reaching here quietly
    # would be the failure form this module raises `SnapshotKeyless` to avoid: a plausible
    # answer that silently costs a gigabyte while /api/health stays green. If this line ever
    # appears, either the key order changed or `meta` outgrew a 16 MB prefix.
    print(
        f"  store: snapshot_meta fell back to a FULL parse of {p} "
        f"(meta not within {_META_PREFIX_TRIES[-1] >> 20} MB) — peak will be ~3x the file"
    )
    try:
        with p.open("r", encoding="utf-8") as fp:
            doc = json.load(fp)
    except (OSError, json.JSONDecodeError):
        return {}
    meta = doc.get("meta") if isinstance(doc, dict) else None
    return meta if isinstance(meta, dict) else {}


def sync_snapshot(path: str | None = None) -> int:
    """Import jobs.json if it's newer than the copy this store last ingested.

    Returns the number of rows imported (0 when already current or absent). The
    mtime is recorded only AFTER a successful upsert, so an interrupted import is
    retried on the next call rather than silently skipped.
    """
    p = Path(JOBS_JSON_PATH)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return 0
    seen = _meta_get(SNAPSHOT_MTIME_KEY, path)
    if seen is not None and float(seen) >= mtime:
        return 0
    try:
        # A GENERATOR, not a list — see _iter_snapshot_jobs. upsert_jobs consumes it
        # lazily inside one transaction, so neither side ever holds the whole file.
        # Its return value is the KEPT count, which is what this function should report:
        # the old `len(rows)` counted rows READ, so it over-reported by everything the
        # US-only filter dropped (15,983 on the most recent harvest).
        imported = upsert_jobs(_iter_snapshot_jobs(p), path=path)
    except (SnapshotTruncated, SnapshotKeyless) as e:
        # Do NOT record the mtime: a partial or misshapen import must be retried, not
        # accepted. Recording it is what turns a bad read into a PERMANENT frozen pool, since
        # the mtime gate then reports "already current" forever. And say so — this used to be
        # swallowed into `return 0`, which reads identically to "nothing to do".
        print(f"  store: snapshot import ABORTED — {e}; will retry")
        return 0
    except (json.JSONDecodeError, OSError) as e:
        print(f"  store: snapshot unreadable ({type(e).__name__}: {e}); will retry")
        return 0
    _meta_set(SNAPSHOT_MTIME_KEY, repr(mtime), path)
    return imported


# ── the company -> ATS resolution ledger ──────────────────────────────────────
# How long a NEGATIVE stays trusted. A company with no board today may adopt one
# next quarter, so 'unresolved' expires — but slowly, because re-probing 3k
# dead-ends is the exact cost this ledger exists to avoid.
UNRESOLVED_RETRY_DAYS = int(os.environ.get("JOBFITR_UNRESOLVED_RETRY_DAYS", "90"))


def norm_company(name: str) -> str:
    """The ledger's primary key. Delegates to job_radar's normalizer so slug
    generation, identity comparison, and this key can never disagree about whether
    'Westhab Inc.' and 'Westhab' are the same employer."""
    return _jr_norm_name(name)


def unresolved_companies(limit: int = 500, path: str | None = None) -> list[str]:
    """Companies needing an ATS probe: never checked, or checked long enough ago
    that the negative has expired. Ordered by job count so the employers that
    actually matter to users get resolved first.

    The dedupe happens on the NORMALIZED name, so 'Westhab Inc.' and 'Westhab' are one
    company needing one probe, not two racing to disagree. Done in Python rather than
    SQL because the normalizer is job_radar's — sharing the function is what keeps the
    two sides from drifting into different answers.
    """
    cutoff = time.time() - UNRESOLVED_RETRY_DAYS * 86400
    with _conn(path) as c:
        counts = c.execute(
            "SELECT company, COUNT(*) n FROM jobs WHERE company <> '' "
            "GROUP BY company ORDER BY n DESC"
        ).fetchall()
        known = {
            r["name_key"]: (r["status"], r["checked_at"] or 0)
            for r in c.execute(
                "SELECT name_key, status, checked_at FROM companies"
            ).fetchall()
        }

    out, seen = [], set()
    for company, _n in counts:
        key = norm_company(company)
        if not key or key in seen:
            continue
        status, checked = known.get(key, (None, 0))
        # never checked, or a negative old enough to be worth one more look. 'dead'
        # and 'resolved' are both terminal — a refusal is not a maybe.
        if status is None or (status == "unresolved" and checked < cutoff):
            seen.add(key)
            out.append(company)
            if len(out) >= limit:
                break
    return out


def recently_probed_boards(path: str | None = None) -> set[tuple[str, str, str]]:
    """Every `board:` key probed inside the retry window, as (ats, slug, site) triples.

    The half of the non-answer cache that makes it DO anything. `discover_new` builds its
    `known` set from `resolved_companies()`, which returns only status='resolved' — so a
    board recorded as a 404 was invisible to the next run and got re-probed nightly forever.
    A ~6,200-board universe against a 500/night budget therefore never converged: the same
    tranche recycled indefinitely while the rest of the alphabet waited.

    Scoped to `board:%` keys deliberately. Company rows live in the same table and are the
    other lane's business; folding them in here would let a name-resolution outcome suppress
    a board probe, which are different questions about different things.

    Outside the window a board is offered again, which is the point — `empty` especially,
    since a board with no open roles today may post tomorrow.
    """
    cutoff = time.time() - UNRESOLVED_RETRY_DAYS * 86400
    out: set[tuple[str, str, str]] = set()
    with _conn(path) as c:
        rows = c.execute(
            "SELECT name_key, ats, slug, site FROM companies "
            "WHERE name_key LIKE 'board:%' AND COALESCE(checked_at, 0) > ?",
            (cutoff,),
        ).fetchall()
    for r in rows:
        ats, slug = (r["ats"] or ""), (r["slug"] or "")
        if not ats or not slug:
            # A non-answer row carries no ats/slug (nothing was found), so recover them from
            # the key itself — `board:{ats}:{slug}` or `board:workday:{slug}/{site}`.
            parts = r["name_key"].split(":", 2)
            if len(parts) == 3:
                ats, slug = parts[1], parts[2]
        site = r["site"] or ""
        if ats == "workday" and "/" in slug and not site:
            slug, site = slug.split("/", 1)
        if ats and slug:
            out.add((ats, slug.lower(), site.lower()))
    return out


def record_resolution(
    name: str,
    entry: dict | None = None,
    variant: str = "",
    status: str | None = None,
    key: str | None = None,
    path: str | None = None,
) -> None:
    """Write one company's outcome. `entry` None/empty = a cached NEGATIVE.

    `attempts` increments across runs so a company that keeps failing stays visible.

    `key` overrides the primary key. A COMPANY resolution keys on its normalized name
    (the default); a discovered BOARD passes an explicit `board:{ats}:{slug}` key so it can
    never take the identity of a name-resolved company (see resolve.board_key).

    THE REASON THIS DOCSTRING USED TO GIVE WAS FALSE, and it is corrected rather than
    deleted because the wrong reason invites the wrong change. It claimed the two "collide by
    construction — a company's slug IS its normalized name". They cannot collide: the two
    normalizations differ. `norm_company('eClinical Solutions')` is `'eclinical solutions'` —
    space PRESERVED — while `discover.name_variants` yields `'eclinicalsolutions'`. Measured
    live, 397 resolved rows have `name_key != norm_company(name)`, which is the same
    population as the 377 companies that read simultaneously resolved and unresolved.

    The namespacing is still right, defensively: it costs nothing, it states intent at the
    key, and it survives a future change to either normalizer that WOULD make them collide.
    Keep it — just do not believe it is load-bearing today.

    `status` overrides the derived value:
      'dead'    — the board answers but refuses us (a 403 Workday tenant); never retried.
      'covered' — a company whose guessable slug space is already held by a resolved board,
                  so name-guessing cannot produce anything new. Terminal, and claims NO
                  ownership of that board.
    """
    now = time.time()
    e = entry or {}
    new_status = status or ("resolved" if e else "unresolved")
    row_key = key or norm_company(name)
    with _conn(path) as c:
        # No-downgrade guard: a refusal must never bury a live resolution. Without it,
        # a discovered board that 403s could write status='dead' — terminal — over a
        # correct binding, nulling its ats/slug and removing it from both resolution
        # and discovery forever. The schema comment calls this the "sticky and silent"
        # failure; this is the belt to the key-namespacing suspenders.
        if new_status == "dead":
            existing = c.execute(
                "SELECT status FROM companies WHERE name_key=?", (row_key,)
            ).fetchone()
            if existing and existing["status"] == "resolved":
                return
        c.execute(
            """INSERT INTO companies(name_key,name,ats,slug,host,site,status,roles,
                                     matched_variant,checked_at,attempts)
               VALUES(:key,:name,:ats,:slug,:host,:site,:status,:roles,:variant,:now,1)
               ON CONFLICT(name_key) DO UPDATE SET
                 name=excluded.name,
                 ats=excluded.ats, slug=excluded.slug, host=excluded.host,
                 site=excluded.site, status=excluded.status, roles=excluded.roles,
                 matched_variant=excluded.matched_variant, checked_at=excluded.checked_at,
                 attempts=companies.attempts+1""",
            {
                "key": row_key,
                "name": name,
                "ats": e.get("ats"),
                "slug": e.get("slug"),
                "host": e.get("host"),
                "site": e.get("site"),
                "status": new_status,
                "roles": e.get("roles"),
                "variant": variant,
                "now": now,
            },
        )


def board_evidence(path: str | None = None) -> dict[str, set]:
    """What each company's OWN job URLs say about which board it owns.

    An independent authority we already hold and never used. When an aggregator hands
    us a posting for company X whose apply link is jobs.ashbyhq.com/Y, that is X
    asserting ownership of Y — not an inference we made. Crucially it works for every
    platform, including Ashby and Lever, which expose no company name and therefore
    cannot be checked any other way.

    Returns {normalized_name: {(ats, slug), ...}}. A company can legitimately map to
    more than one board, so callers must treat a match as agreement rather than
    requiring a single value.
    """
    out: dict[str, set] = {}
    with _conn(path) as c:
        rows = c.execute(
            "SELECT company, url FROM jobs WHERE company <> '' AND url <> ''"
        ).fetchall()
    for company, url in rows:
        got = ats_from_url(url or "")
        if not got:
            continue
        out.setdefault(norm_company(company), set()).add((got[0], got[1].lower()))
    return out


def audit_resolutions(path: str | None = None) -> dict:
    """Check every resolution against the apply-URL evidence.

    Returns {'checked', 'agree', 'disagree': [rows]}. A disagreement is a resolution
    contradicted by the company's own links — the strongest false-binding signal
    available, and the only one that reaches Ashby/Lever.
    """
    truth = board_evidence(path)
    agree, disagree = 0, []
    with _conn(path) as c:
        rows = c.execute(
            "SELECT name_key,name,ats,slug,matched_variant,roles FROM companies "
            "WHERE status='resolved' AND ats IS NOT NULL AND slug IS NOT NULL"
        ).fetchall()
    for r in rows:
        evidence = truth.get(r["name_key"])
        if not evidence:
            continue  # no URL evidence for this company — not checkable, not wrong
        if (r["ats"], (r["slug"] or "").lower()) in evidence:
            agree += 1
        else:
            disagree.append({**dict(r), "url_says": sorted(evidence)})
    return {"checked": agree + len(disagree), "agree": agree, "disagree": disagree}


def quarantine(name: str, reason: str = "", path: str | None = None) -> None:
    """Retract a resolution that the evidence contradicts.

    Marked 'quarantined' rather than deleted or reset to unresolved: the wrong slug
    and the variant that produced it stay on the row, so the mistake stays legible
    and the same bad guess is not simply made again tomorrow.
    """
    with _conn(path) as c:
        c.execute(
            "UPDATE companies SET status='quarantined', matched_variant=? "
            "WHERE name_key=?",
            (f"QUARANTINED:{reason}"[:120], norm_company(name)),
        )


def seed_companies_from_watchlist(
    watchlist_path: str | os.PathLike, path: str | None = None
) -> int:
    """Import a curated watchlist as already-resolved companies.

    The 94 hand-verified entries in deploy/tech-watchlist.json were each live-probed
    before being committed, so re-probing them would spend requests to re-learn a fact
    we already trust. Seeding them also stops them appearing as 'unresolved' work and
    crowding out the companies that genuinely need discovery.

    Idempotent: re-seeding refreshes the same rows rather than duplicating them.
    """
    try:
        with open(watchlist_path, encoding="utf-8") as f:
            companies = json.load(f).get("companies", [])
    except (OSError, json.JSONDecodeError):
        return 0
    n = 0
    for c in companies:
        name, ats, slug = c.get("name"), c.get("ats"), c.get("slug")
        if not (name and ats and slug):
            continue
        record_resolution(
            name,
            {
                "ats": ats,
                "slug": slug,
                "host": c.get("host"),
                "site": c.get("site"),
                "roles": None,
            },
            variant="curated",
            path=path,
        )
        n += 1
    return n


def resolved_companies(path: str | None = None) -> list[dict]:
    """Every resolved board, richest first — the rows that graduate into a watchlist."""
    with _conn(path) as c:
        rows = c.execute(
            """SELECT name,ats,slug,host,site,roles,matched_variant FROM companies
               WHERE status='resolved' AND ats IS NOT NULL AND slug IS NOT NULL
               ORDER BY COALESCE(roles,0) DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def resolution_stats(path: str | None = None) -> dict:
    """Ledger state. `companies_in_store` counts DISTINCT NORMALIZED employers, so it
    is comparable with the ledger's own row count rather than inflated by spellings."""
    with _conn(path) as c:
        rows = dict(
            c.execute(
                "SELECT status, COUNT(*) FROM companies GROUP BY status"
            ).fetchall()
        )
        names = [
            r[0]
            for r in c.execute(
                "SELECT DISTINCT company FROM jobs WHERE company <> ''"
            ).fetchall()
        ]
    total = len({norm_company(n) for n in names if norm_company(n)})
    return {
        "companies_in_store": total,
        "resolved": rows.get("resolved", 0),
        "unresolved": rows.get("unresolved", 0),
        "dead": rows.get("dead", 0),
        "never_checked": max(0, total - sum(rows.values())),
    }


def snapshot_imported_at(path: str | None = None) -> str | None:
    """ET timestamp of the harvest snapshot this store last ingested (None if never)."""
    seen = _meta_get(SNAPSHOT_MTIME_KEY, path)
    if seen is None:
        return None
    return datetime.fromtimestamp(float(seen), _ET).isoformat(timespec="seconds")


# ── writes ────────────────────────────────────────────────────────────────────
def upsert_jobs(jobs: Iterable[dict], path: str | None = None) -> int:
    """Insert or refresh rows, deduped by url. Normalizes raw rows first.

    Takes any ITERABLE, not just a list, and the loop below consumes it lazily — that is
    what lets `sync_snapshot` hand it a streaming reader so a 363 MB snapshot never lands
    in memory at once. Do not add a `len()` or a second pass over `jobs`.

    An existing url has its last_seen/posted/salary (and derived tags) refreshed —
    so an actively-re-fetched job's last_seen keeps resetting and it never evicts.

    Two policies are enforced here: non-US postings (`servable_in_us`) and aggregator links
    (`direct_to_employer`). This is the ONE funnel both the nightly harvest and the per-search
    live fetch pass through, so filtering here covers both without either caller knowing about
    it. Each count is printed rather than dropped silently — a filter that quietly eats rows is
    how you spend a week wondering where the jobs went.
    """
    now = time.time()
    n = 0
    skipped_non_us = 0
    skipped_indirect = 0
    with _conn(path) as c:
        for raw in jobs:
            r = normalize_job(raw)
            if not r["url"]:
                continue
            if not servable_in_us(raw):
                skipped_non_us += 1
                continue
            if not direct_to_employer(r):
                skipped_indirect += 1
                continue
            c.execute(_UPSERT_SQL, {**r, "now": now})
            n += 1
    if skipped_indirect:
        print(
            f"  store: dropped {skipped_indirect:,} aggregator-link postings "
            f"(JOBFITR_DIRECT_ONLY)"
        )
    if skipped_non_us:
        print(f"  store: dropped {skipped_non_us:,} non-US postings (JOBFITR_US_ONLY)")
    return n


# ── the freshness clock (per title|location) ─────────────────────────────────
def search_key(titles: list[str] | str, location: str | None) -> str:
    if isinstance(titles, (list, tuple)):
        t = ",".join(sorted(x.strip().lower() for x in titles if x and x.strip()))
    else:
        t = (titles or "").strip().lower()
    return f"{t}|{(location or '').strip().lower()}"


def search_fresh(
    key: str, ttl: int = SEARCH_TTL_SECONDS, path: str | None = None
) -> bool:
    with _conn(path) as c:
        row = c.execute(
            "SELECT fetched_at FROM searches WHERE key=?", (key,)
        ).fetchone()
    return bool(row) and (time.time() - row[0]) < ttl


def mark_fetched(key: str, path: str | None = None) -> None:
    now = time.time()
    with _conn(path) as c:
        c.execute(
            "INSERT INTO searches(key,fetched_at) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET fetched_at=?",
            (key, now, now),
        )


# ── retrieval: FTS5 BM25 candidates ──────────────────────────────────────────
# How far apart NEAR lets the title words sit. Measured on the frozen corpus: 2 is
# meaningfully tighter ("senior ai engineer" 221 vs 273) and 3 through 10 return the
# same rows, so 3 is the smallest value that costs nothing.
_FTS_SLOP = 3


def _fts_query(titles: list[str]) -> str:
    """Build an FTS5 MATCH query: OR of title-scoped NEAR clauses, one per title.

    This was an OR of quoted phrases, and a quoted phrase in FTS5 means ADJACENT AND
    IN ORDER. So `"supply chain analyst"` matched 2 rows while missing every real
    variant of itself — `Supply Chain Master Data Analyst` (a word inserted mid-title),
    `Analyst, Transportation & Supply Chain Strategy` (reordered), `Senior FP&A Analyst
    - Supply Chain`. Those rows never entered the candidate pool at all, so the
    scoreboard never scored them: an invisible RECALL bug, not a ranking one.

    `NEAR(..., 3)` asks for the same words within 3 tokens in any order, and the
    `title:` prefix scopes the match to the title column instead of title+body.

    Measured over the 57-profile fixture: candidates **-20.9%** AND score regret
    **3.04% -> 0.30%**, perfect boards 29/57 -> 53/57 — more recall and less work in
    the same change. Both halves are load-bearing: title-scoping alone measured as a
    REGRESSION (3.45% regret) because it drops body matches without gaining NEAR's
    flexibility, and NEAR alone gives no latency win.
    """
    clauses = []
    for t in titles:
        t = re.sub(r'["^*():,]', " ", (t or "")).strip()
        # Drop tokens with no alphanumerics ("&", "-"): FTS5 tokenizes them to nothing,
        # so they only make the query string harder to read.
        words = [w for w in t.split() if any(ch.isalnum() for ch in w)]
        if not words:
            continue
        terms = " ".join(f'"{w}"' for w in words)
        clauses.append(f"title: NEAR({terms}, {_FTS_SLOP})")
    return " OR ".join(clauses)


def bm25_candidates(
    titles: list[str], limit: int | None = None, path: str | None = None
) -> list[dict]:
    """Return the jobs matching the user's titles, ranked by BM25.

    Title column weighted heavily over body. Returns dicts (row + `bm25` base
    score, higher = better) for the personalized rerank in server.py.

    `limit=None` (the default, and what the server passes) means EVERY match. It used
    to be a mandatory 500, which quietly made retrieval the arbiter of what the ranker
    could even see. Scoring is deterministic Python and cheap — the widest user in the
    test fixture is 5,129 rows in about a second — so there is no reason to decide in
    advance that the 501st match cannot possibly be the best job for someone.

    The parameter stays because tests pin small pools with it and the semantic arm may
    want a bounded first stage.
    """
    q = _fts_query(titles)
    if not q:
        return []
    # `ORDER BY rank` is LOAD-BEARING even though nothing reads the bm25 VALUE:
    # server._rank's sort is stable, so this ordering survives as the tie-break for
    # equal-scoring listings. See the spec note above server.scoreboard.
    sql = """SELECT j.*, bm25(jobs_fts, 8.0, 2.0, 1.0) AS rank
             FROM jobs_fts JOIN jobs j ON j.rowid = jobs_fts.rowid
             WHERE jobs_fts MATCH ?
             ORDER BY rank"""
    params: tuple = (q,)
    if limit is not None:
        sql += " LIMIT ?"
        params = (q, limit)
    with _conn(path) as c:
        try:
            rows = c.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []  # a malformed MATCH never 500s the request
    out = []
    for r in rows:
        d = dict(r)
        d["bm25"] = -float(d.pop("rank"))  # flip: bigger = better
        out.append(d)
    return out


def rows_by_url(urls: list[str], path: str | None = None) -> list[dict]:
    """Fetch full rows for a set of urls, preserving the CALLER's order.

    The dense arm returns urls, and some of them were never in the lexical arm's candidate
    set — those rows have to be fetched before anything can filter or render them. Order
    is restored from the input because SQLite returns `IN (...)` in whatever order it
    likes, and the input order IS the fusion's ranking.

    Chunked at 400 to stay under SQLITE_MAX_VARIABLE_NUMBER (999 on older builds); a
    candidate set is only ~50 today but the depth this is called with is not capped.
    """
    if not urls:
        return []
    out: dict[str, dict] = {}
    with _conn(path) as con:
        con.row_factory = sqlite3.Row
        for i in range(0, len(urls), 400):
            batch = urls[i : i + 400]
            q = ",".join("?" * len(batch))
            for r in con.execute(f"SELECT * FROM jobs WHERE url IN ({q})", batch):
                out[r["url"]] = dict(r)
    return [out[u] for u in urls if u in out]


def facet_counts(rows: list[dict]) -> dict:
    """Count the facet tags across a result set (for the filter drawer)."""
    facets: dict[str, dict] = {
        "category": {},
        "employment_type": {},
        "remote": {},
        "seniority": {},
        "salary_band": {},
        "state": {},  # a USPS code — vocab.us_state guarantees the closed set
        # 'employer' | 'aggregator' | '' — but in practice ONE value since 2026-08-17, because
        # aggregator links are dropped at intake (see direct_to_employer). The other states are
        # reachable only with JOBFITR_DIRECT_ONLY=0. web/app.js hides a one-chip facet group, so
        # the Apply drawer stops rendering on its own rather than showing a control that cannot
        # narrow anything.
        "apply_via": {},
    }
    for r in rows:
        for f in facets:
            v = r.get(f)
            if v:
                facets[f][v] = facets[f].get(v, 0) + 1
    return facets


def candidate_count(titles: list[str], path: str | None = None) -> int:
    """How many listings this search will have to rank — WITHOUT fetching any of them.

    A COUNT over the FTS index, so it costs milliseconds where materialising the rows
    costs hundreds. It exists so the interview can tell the user what is about to happen:
    a specific title like "Data Analyst" matches a few hundred listings and scores in
    under a second, while a bare "engineer" matches 25,849 — half the corpus, all of them
    real — and takes as long as that implies. Saying so is better than a spinner that
    looks broken.
    """
    q = _fts_query(titles)
    if not q:
        return 0
    with _conn(path) as c:
        try:
            row = c.execute(
                "SELECT count(*) FROM jobs_fts WHERE jobs_fts MATCH ?", (q,)
            ).fetchone()
        except sqlite3.OperationalError:
            return 0  # a malformed MATCH never 500s the request
    return int(row[0]) if row else 0


def pool_size(path: str | None = None) -> int:
    with _conn(path) as c:
        row = c.execute("SELECT count(*) FROM jobs").fetchone()
    return int(row[0]) if row else 0


def newest_posted(path: str | None = None) -> str:
    with _conn(path) as c:
        row = c.execute("SELECT max(posted) FROM jobs").fetchone()
    return (row[0] or "") if row else ""


# ── liveness: ask the URL directly, for the rows nothing else can verify ─────
# Bounded per run. The nightly harvest confirms 97.7% of the pool by re-fetching it; this is
# for the other 2.3% (adzuna, usajobs, google_jobs), which the per-search live lane only
# revisits when a user's search happens to match. For those, `last_seen` means "someone
# searched something like this recently", not "this job is alive", so the only way to know is
# to ask.
VERIFY_BATCH = int(os.environ.get("JOBFITR_VERIFY_BATCH", "400"))
VERIFY_TIMEOUT = float(os.environ.get("JOBFITR_VERIFY_TIMEOUT", "8"))
VERIFY_WORKERS = int(os.environ.get("JOBFITR_VERIFY_WORKERS", "8"))

# ONLY these mean "the posting is gone". Deliberately narrow, because the cost of being wrong
# is deleting a live job the user could have applied to:
#   404 Gone / 410 Gone     — the posting was removed.
#   Everything else survives — 403 and 429 are the site declining to answer US, not the job
#   being closed; 5xx is their outage; a 405 means the host dislikes HEAD; and a 200 on a
#   "this role has closed" PAGE is indistinguishable from a live one without parsing, which
#   is a guess this function refuses to make.
_DEAD_STATUSES = frozenset({404, 410})


def verify_unpolled(
    limit: int | None = None, path: str | None = None, now: float | None = None
) -> dict:
    """HEAD the least-recently-seen live-lane rows; delete the dead, refresh the living.

    Returns {'checked', 'dead', 'alive', 'unknown'}.

    WHY BOTH HALVES MATTER. Deleting 404s is the obvious half — a user clicking a withdrawn
    posting is the worst experience this product can deliver. The other half is that a 200
    REFRESHES `last_seen`, which is what stops the 14-day unseen rule from evicting live
    aggregator jobs merely because nobody searched for them lately. Without that, the patient
    window is really "delete unverifiable rows on a timer".

    Ordered by `last_seen` ascending, so each run spends its budget on the rows whose liveness
    is least certain, and the pool converges instead of re-checking the same fresh rows.
    """
    import urllib.error
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    now = now if now is not None else time.time()
    limit = VERIFY_BATCH if limit is None else limit

    polled: set[str] = set()
    try:
        polled = {
            str(s) for s in (snapshot_meta(Path(JOBS_JSON_PATH)).get("sources") or [])
        }
    except (OSError, ValueError):
        pass
    if not polled:
        # Without the harvest's own record of what it fetches we cannot tell which rows lack a
        # heartbeat, and checking ALL of them would mean HEADing the whole pool. Do nothing and
        # say so, rather than pick a subset by guesswork.
        return {"checked": 0, "dead": 0, "alive": 0, "unknown": 0, "skipped": "no snapshot"}

    marks = ",".join("?" * len(polled))
    with _conn(path) as c:
        rows = c.execute(
            f"SELECT url FROM jobs WHERE source NOT IN ({marks}) "
            f"ORDER BY last_seen ASC LIMIT ?",
            (*polled, limit),
        ).fetchall()
    urls = [r["url"] for r in rows]
    if not urls:
        return {"checked": 0, "dead": 0, "alive": 0, "unknown": 0}

    def check(url: str) -> tuple[str, int | None]:
        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "jobfitr-liveness/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=VERIFY_TIMEOUT) as r:
                return url, r.status
        except urllib.error.HTTPError as e:
            return url, e.code
        except Exception:  # noqa: BLE001 — a network fault is not evidence about the posting
            return url, None

    with ThreadPoolExecutor(max_workers=VERIFY_WORKERS) as pool:
        results = list(pool.map(check, urls))

    dead = [u for u, s in results if s in _DEAD_STATUSES]
    alive = [u for u, s in results if s is not None and 200 <= s < 400]
    with _conn(path) as c:
        for u in dead:
            c.execute("DELETE FROM jobs WHERE url=?", (u,))
        for u in alive:
            c.execute("UPDATE jobs SET last_seen=? WHERE url=?", (now, u))
    return {
        "checked": len(results),
        "dead": len(dead),
        "alive": len(alive),
        "unknown": len(results) - len(dead) - len(alive),
    }


# ── the eviction outflow (nightly; the maintenance-as-normal-path) ────────────
def evict(now: float | None = None, path: str | None = None) -> int:
    """Garbage-collect, then enforce the MAX_ROWS LRU cap. Returns rows deleted.

    Three rules, and they answer different questions:
      * unseen  — the SOURCE stopped listing it, so it is gone. Window depends on whether the
                  nightly harvest re-fetches that source (EVICT_UNSEEN_POLLED_DAYS) or only a
                  user search does (EVICT_UNSEEN_DAYS).
      * posted  — the listing is simply old (EVICT_POSTED_DAYS).
      * LRU cap — the pool is at saturation (MAX_ROWS).
    """
    now = now if now is not None else time.time()
    unseen_cut = now - EVICT_UNSEEN_DAYS * 86400
    polled_cut = now - EVICT_UNSEEN_POLLED_DAYS * 86400
    posted_cut = date.fromtimestamp(now).toordinal() - EVICT_POSTED_DAYS
    deleted = 0

    # WHICH SOURCES HAVE A HEARTBEAT — read from the harvest's OWN record of what it fetched
    # rather than a list maintained here. `meta.sources` is written by the harvest that just
    # ran, so a source joining or leaving the nightly harvest moves its window automatically;
    # a hardcoded set would rot silently and this project has spent a day fixing exactly that
    # class of drift. If the snapshot cannot be read, `polled` is empty and EVERY row gets the
    # long window — the conservative direction, since the failure deletes nothing early.
    polled: set[str] = set()
    try:
        polled = {
            str(s) for s in (snapshot_meta(Path(JOBS_JSON_PATH)).get("sources") or [])
        }
    except (OSError, ValueError):
        pass

    with _conn(path) as c:
        if polled:
            marks = ",".join("?" * len(polled))
            deleted += c.execute(
                f"DELETE FROM jobs WHERE (source IN ({marks}) AND last_seen < ?) "
                f"OR (source NOT IN ({marks}) AND last_seen < ?)",
                (*polled, polled_cut, *polled, unseen_cut),
            ).rowcount
        else:
            deleted += c.execute(
                "DELETE FROM jobs WHERE last_seen < ?", (unseen_cut,)
            ).rowcount
        # posted is an ISO date string; compare ordinals via a python filter for safety
        stale = []
        for r in c.execute("SELECT url, posted FROM jobs").fetchall():
            try:
                if (
                    r["posted"]
                    and datetime.fromisoformat(r["posted"][:10]).toordinal()
                    < posted_cut
                ):
                    stale.append(r["url"])
            except ValueError:
                continue
        for u in stale:
            deleted += c.execute("DELETE FROM jobs WHERE url=?", (u,)).rowcount
        # LRU cap: keep the MAX_ROWS most-recently-seen
        over = c.execute("SELECT count(*) FROM jobs").fetchone()[0] - MAX_ROWS
        if over > 0:
            deleted += c.execute(
                "DELETE FROM jobs WHERE url IN "
                "(SELECT url FROM jobs ORDER BY last_seen ASC LIMIT ?)",
                (over,),
            ).rowcount
    return deleted


def main(argv=None) -> int:  # pragma: no cover — exercised via jobfitr-evict
    """CLI entry for the nightly eviction timer.

    Runs the liveness check FIRST, then eviction. The order matters: verifying refreshes
    `last_seen` on rows that answer 200, so a live aggregator job is proven alive before the
    unseen rule looks at it. Reversed, eviction could delete a row this pass was about to
    confirm.
    """
    import argparse

    ap = argparse.ArgumentParser(prog="jobfitr-evict")
    ap.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the liveness HEAD check (eviction only)",
    )
    ap.add_argument(
        "--verify-batch",
        type=int,
        default=None,
        help=f"how many live-lane rows to HEAD this run (default {VERIFY_BATCH}). Bounded "
        f"because the harvest already confirms 97.7%% of the pool by re-fetching it; this is "
        f"only for the ~2.3%% the per-search live lane touches.",
    )
    args = ap.parse_args(argv)

    init()
    if not args.no_verify:
        v = verify_unpolled(limit=args.verify_batch)
        if v.get("skipped"):
            print(f"jobfitr-verify: skipped ({v['skipped']})")
        elif v["checked"]:
            print(
                f"jobfitr-verify: HEADed {v['checked']:,} live-lane rows -> "
                f"{v['dead']:,} dead (removed), {v['alive']:,} confirmed alive, "
                f"{v['unknown']:,} no answer (kept)"
            )
    n = evict()
    stamp = datetime.now(_ET).isoformat(timespec="seconds")
    print(f"jobfitr-evict: removed {n} stale jobs; pool now {pool_size()} @ {stamp}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
