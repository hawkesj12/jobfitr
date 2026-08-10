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

import json
import os
import re
import sqlite3
import time
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
DB_PATH = os.environ.get("JOBFITR_DB_PATH", "jobs.db")
# Where the shared harvest snapshot lives. Re-imported whenever the harvest rewrites
# it (see sync_snapshot) — this is how a per-slot store stays current, and it's also
# the rollback artifact old code reads.
JOBS_JSON_PATH = os.environ.get("JOBFITR_JOBS_PATH", "jobs.json")

SEARCH_TTL_SECONDS = int(os.environ.get("JOBFITR_SEARCH_TTL", str(24 * 3600)))  # 24h
EVICT_UNSEEN_DAYS = int(os.environ.get("JOBFITR_EVICT_UNSEEN_DAYS", "14"))
EVICT_POSTED_DAYS = int(os.environ.get("JOBFITR_EVICT_POSTED_DAYS", "60"))
MAX_ROWS = int(os.environ.get("JOBFITR_MAX_ROWS", "50000"))  # LRU cap (saturation)

BODY_CAP = 2000

# ── US-only intake ────────────────────────────────────────────────────────────
# jobfitr serves a US audience, so a posting in Berlin costs storage, scoring time
# (0.7 ms/candidate, and every retrieved candidate is scored) and a board slot for a
# job nobody here can take. job_radar stays international on purpose — it is a
# general-purpose engine on PyPI and the filter is OUR opinion, so it lives here.
#
# THE RULE IS NOT `country == "US"`. A remote posting has no country by nature
# ("Remote", "Anywhere", "Worldwide" are not places), so a bare country test deletes
# most of them. A US-only board with no remote work on it is the opposite of the
# product. Measured on the 21,495-row capture, under the four-state tagging:
#
#   keep  remote            5,509 rows — location-independent, wanted regardless
#   keep  country == "US"   9,080
#   keep  country unknown   3,617 — overwhelmingly US ATS boards; see the caveat
#   drop  country foreign   3,289 — IN 457 · GB 404 · DE 323 · CA 290 · JP 222
#
# 84.7% kept, and 100% of remote jobs retained. The drop grew from 2,990 when remote
# stopped being a binary: 572 of those 3,289 are stated-HYBRID roles abroad, which the
# old tagging called "remote" and therefore exempted from the country test. A hybrid
# job in London is not servable to a US audience — it requires being in London.
#
# KNOWN LEAK, measured, not guessed: unknown-country rows pass. ~1,850 of the 7,346
# name a foreign place in their location text, so some foreign postings survive. The
# fix is better country derivation upstream, not a regex of country names here — a
# name list produces false positives on real US cities (Dublin CA, Berlin CT, Toronto
# OH) and rots. Separately ~20 rows arrive already mislabelled `country: "US"` because
# job_radar read a foreign country code as a US state ("Toronto, ON, CA" -> CA
# California; "Berlin, DE" -> DE Delaware); those leak too, and that is a job-radar fix.
US_ONLY = os.environ.get("JOBFITR_US_ONLY", "1") != "0"


def servable_in_us(job: dict, row: dict) -> bool:
    """True when a posting belongs on a US board. `job` is the engine row (carries
    `country`); `row` is its normalized form (carries the derived remote tag)."""
    if not US_ONLY:
        return True
    if row.get("remote") == "remote":
        return True  # location-independent — a country test would delete 73% of these
    country = _s(job.get("country")).upper()
    return country in ("", "US")  # unknown passes; only KNOWN-foreign is dropped


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


def _salary_band(salary: str) -> str:
    """Bucket a salary string into a coarse band tag (empty when none/unknown)."""
    if not salary:
        return ""
    nums = [int(n.replace(",", "")) for n in re.findall(r"[\d,]{3,}", salary)]
    if not nums:
        return ""
    top = max(nums)
    if top < 50_000:
        return "under-50k"
    if top < 80_000:
        return "50-80k"
    if top < 120_000:
        return "80-120k"
    if top < 180_000:
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

    Reconciled over the capture: 54.6% NULL · 29.8% remote · 8.4% hybrid · 7.2% onsite,
    replacing a flat 64.1% onsite / 35.9% remote that was 3/4 guess.
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
        "salary_band": _salary_band(salary),
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
        "state": _s(job.get("state")),
        "country": _s(job.get("country")),
        "locations": _json(job.get("locations")),
        "remote_region": _s(job.get("remote_region")),
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
  remote_basis TEXT,       -- 'stated' | 'board' | 'location' | 'text' | 'derived' | NULL
  remote_region TEXT,      -- where a remote worker may sit
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


SCHEMA_VERSION = 2
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
        snap = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return 0
    rows = snap.get("jobs", []) if isinstance(snap, dict) else []
    if rows:
        upsert_jobs(rows, path=path)
    _meta_set(SNAPSHOT_MTIME_KEY, repr(mtime), path)
    return len(rows)


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


def record_resolution(
    name: str,
    entry: dict | None = None,
    variant: str = "",
    status: str | None = None,
    key: str | None = None,
    path: str | None = None,
) -> None:
    """Write one company's outcome. `entry` None/empty = a cached NEGATIVE.

    `status` overrides the derived value — pass 'dead' for a board that answers but
    refuses us (a 403 Workday tenant), so the scheduler stops asking. `attempts`
    increments across runs so a company that keeps failing stays visible.

    `key` overrides the primary key. A COMPANY resolution keys on its normalized name
    (the default); a BOARD discovered from Common Crawl must NOT, because a board slug
    and a company name share this one namespace and collide by construction — a
    company's slug IS its normalized name. A discovered board passes an explicit
    `board:{ats}:{slug}` key so it can never take the identity of a name-resolved
    company (see resolve.board_key).
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
def upsert_jobs(jobs: list[dict], path: str | None = None) -> int:
    """Insert or refresh rows, deduped by url. Normalizes raw rows first.

    An existing url has its last_seen/posted/salary (and derived tags) refreshed —
    so an actively-re-fetched job's last_seen keeps resetting and it never evicts.

    Non-US postings are dropped here (see `servable_in_us`). This is the ONE funnel
    both the nightly harvest and the per-search live fetch pass through, so filtering
    here covers both without either caller knowing about it. The count is printed
    rather than dropped silently — a filter that quietly eats rows is how you spend a
    week wondering where the jobs went.
    """
    now = time.time()
    n = 0
    skipped_non_us = 0
    with _conn(path) as c:
        for raw in jobs:
            r = normalize_job(raw)
            if not r["url"]:
                continue
            if not servable_in_us(raw, r):
                skipped_non_us += 1
                continue
            c.execute(_UPSERT_SQL, {**r, "now": now})
            n += 1
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


def facet_counts(rows: list[dict]) -> dict:
    """Count the facet tags across a result set (for the filter drawer)."""
    facets: dict[str, dict] = {
        "category": {},
        "employment_type": {},
        "remote": {},
        "seniority": {},
        "salary_band": {},
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


# ── the eviction outflow (nightly; the maintenance-as-normal-path) ────────────
def evict(now: float | None = None, path: str | None = None) -> int:
    """Garbage-collect: drop jobs unseen for EVICT_UNSEEN_DAYS or posted older than
    EVICT_POSTED_DAYS, then enforce the MAX_ROWS LRU cap. Returns rows deleted."""
    now = now if now is not None else time.time()
    unseen_cut = now - EVICT_UNSEEN_DAYS * 86400
    posted_cut = date.fromtimestamp(now).toordinal() - EVICT_POSTED_DAYS
    deleted = 0
    with _conn(path) as c:
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
    """CLI entry for the nightly eviction timer."""
    init()
    n = evict()
    stamp = datetime.now(_ET).isoformat(timespec="seconds")
    print(f"jobfitr-evict: removed {n} stale jobs; pool now {pool_size()} @ {stamp}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
