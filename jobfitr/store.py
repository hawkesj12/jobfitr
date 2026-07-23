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

# ── tag derivation (rule-based only — no LLM, no fabrication) ──────────────────
# Remote is detected by job_radar's shared `remote_posting` predicate (reads
# title/location + the body, with a negation guard) — one source of truth, since
# job_radar is where the "(Remote)" noise originates and the gate already lives.
# seniority/salary_band are derived below.
_SENIORITY = [
    ("lead", re.compile(r"\b(lead|principal|staff|head of|vp|director|chief)\b", re.I)),
    ("senior", re.compile(r"\bsenior\b|\bsr\.?\b", re.I)),
    (
        "junior",
        re.compile(
            r"\b(junior|jr\.?|entry[- ]level|intern|apprentice|associate)\b", re.I
        ),
    ),
]


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


def _seniority(title: str) -> str:
    for label, rx in _SENIORITY:
        if rx.search(title or ""):
            return label
    return "mid"


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


def normalize_job(job: dict) -> dict:
    """Map a raw harvest/live row onto the store's row shape, deriving facet tags.

    `department` is Adzuna's category ('Healthcare & Nursing Jobs', …); we keep it
    as `category`. remote/seniority/salary_band are derived by rule from fields we
    already have — never invented.
    """
    src = _s(job.get("source")) or _s(job.get("sources"))
    loc = _s(job.get("location"))
    # job_radar appends " (Remote)" to EVERY Adzuna location unconditionally (their
    # API has no reliable remote flag) — it's noise, not signal. Strip it so the
    # derived remote tag and the displayed location aren't misleading. The free
    # remote boards append it only when a job is genuinely remote, so keep theirs.
    if src == "adzuna" and loc.endswith(" (Remote)"):
        loc = loc[: -len(" (Remote)")].strip()
    salary = _s(job.get("salary"))
    text = _s(job.get("text"))
    if len(text) > BODY_CAP:
        text = text[:BODY_CAP]
    title = _s(job.get("title"))
    return {
        "url": _s(job.get("url")),
        "title": title,
        "company": _s(job.get("company")),
        "location": loc,
        "source": src,
        "posted": _s(job.get("posted")),
        "salary": salary,
        "body": text,
        "category": _s(job.get("department")) or _s(job.get("category")),
        "employment_type": _s(job.get("employment_type")),
        "remote": "remote" if remote_posting(title, loc, text) else "onsite",
        "seniority": _seniority(title),
        "salary_band": _salary_band(salary),
    }


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
CREATE TABLE IF NOT EXISTS jobs(
  url TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, source TEXT,
  posted TEXT, salary TEXT, body TEXT,
  category TEXT, employment_type TEXT, remote TEXT, seniority TEXT, salary_band TEXT,
  fetched_at REAL, last_seen REAL);
CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen);

-- external-content FTS5 over jobs.rowid: title/location/body are searchable, the
-- rest live in `jobs`. Kept in sync by the triggers below.
CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
  title, location, body, content='jobs', content_rowid='rowid',
  tokenize='porter unicode61');

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


def init(path: str | None = None) -> None:
    """Create the schema if missing, then pull in the current jobs.json snapshot."""
    with _conn(path) as c:
        c.executescript(_SCHEMA)
    sync_snapshot(path)


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
    """
    now = time.time()
    n = 0
    with _conn(path) as c:
        for raw in jobs:
            r = normalize_job(raw)
            if not r["url"]:
                continue
            c.execute(
                """INSERT INTO jobs(url,title,company,location,source,posted,salary,body,
                     category,employment_type,remote,seniority,salary_band,fetched_at,last_seen)
                   VALUES(:url,:title,:company,:location,:source,:posted,:salary,:body,
                     :category,:employment_type,:remote,:seniority,:salary_band,:now,:now)
                   ON CONFLICT(url) DO UPDATE SET
                     last_seen=:now, title=excluded.title, company=excluded.company,
                     location=excluded.location, posted=excluded.posted,
                     salary=excluded.salary, body=excluded.body,
                     category=excluded.category,
                     employment_type=excluded.employment_type, remote=excluded.remote,
                     seniority=excluded.seniority, salary_band=excluded.salary_band""",
                {**r, "now": now},
            )
            n += 1
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
def _fts_query(titles: list[str]) -> str:
    """Build an FTS5 MATCH query: OR of quoted title phrases (quotes = phrase match)."""
    phrases = []
    for t in titles:
        t = re.sub(r'["^*():]', " ", (t or "")).strip()
        if t:
            phrases.append(f'"{t}"')
    return " OR ".join(phrases)


def bm25_candidates(
    titles: list[str], limit: int = 500, path: str | None = None
) -> list[dict]:
    """Return the top-`limit` jobs matching the user's titles, ranked by BM25.

    Title column weighted heavily over body. Returns dicts (row + `bm25` base
    score, higher = better) for the personalized rerank in server.py.
    """
    q = _fts_query(titles)
    if not q:
        return []
    with _conn(path) as c:
        try:
            rows = c.execute(
                """SELECT j.*, bm25(jobs_fts, 8.0, 2.0, 1.0) AS rank
                   FROM jobs_fts JOIN jobs j ON j.rowid = jobs_fts.rowid
                   WHERE jobs_fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (q, limit),
            ).fetchall()
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
