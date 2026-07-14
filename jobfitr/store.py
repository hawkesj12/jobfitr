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

_ET = ZoneInfo("America/New_York")

# ── config (env-overridable) ──────────────────────────────────────────────────
DB_PATH = os.environ.get("JOBFITR_DB_PATH", "jobs.db")
# Where the legacy flat snapshot lives — imported ONCE on first init so the live
# box migrates seamlessly (and rollback to the old code still reads it).
JOBS_JSON_PATH = os.environ.get("JOBFITR_JOBS_PATH", "jobs.json")

SEARCH_TTL_SECONDS = int(os.environ.get("JOBFITR_SEARCH_TTL", str(24 * 3600)))  # 24h
EVICT_UNSEEN_DAYS = int(os.environ.get("JOBFITR_EVICT_UNSEEN_DAYS", "14"))
EVICT_POSTED_DAYS = int(os.environ.get("JOBFITR_EVICT_POSTED_DAYS", "60"))
MAX_ROWS = int(os.environ.get("JOBFITR_MAX_ROWS", "50000"))  # LRU cap (saturation)

BODY_CAP = 2000

# ── tag derivation (rule-based only — no LLM, no fabrication) ──────────────────
# Title/location is short and unambiguous, so a liberal match is safe there.
_REMOTE_RE = re.compile(r"remote|anywhere|work from home|\bwfh\b", re.I)
# The description body is prose, where a bare "remote" false-positives ("remote
# servers", "the remote teams you support"). So require phrases that actually
# denote THIS role's remoteness. This is what recovers keyed-source jobs (Adzuna/
# USAJOBS carry no remote flag and lose their "(Remote)" label above) that are
# genuinely remote but never say so in the title or city.
_REMOTE_BODY_RE = re.compile(
    r"\b(?:fully|100%|completely|permanently)\s+remote\b"
    r"|\bremote[- ](?:first|friendly|eligible|position|role|opportunity|work|based)\b"
    r"|\b(?:this|the)\s+(?:is\s+a\s+)?remote\s+(?:position|role|job|opportunity)\b"
    r"|\bwork[- ]from[- ]home\b|\bwork\s+from\s+home\b"
    r"|\btelecommut\w*|\btelework\w*"
    r"|\bremote\s+(?:within|in|across|throughout|anywhere)\b",
    re.I,
)
# Suppress a body match when the text explicitly negates remoteness, so an
# "on-site only" posting that mentions "remote work" in passing stays onsite.
_REMOTE_NEG_RE = re.compile(
    r"\bno[t]?\s+(?:a\s+)?remote\b|\bno\s+remote\b"
    r"|\bon[- ]?site\s+only\b|\bin[- ]office\s+only\b|\bnot\s+(?:a\s+)?remote\s+(?:position|role|job)\b",
    re.I,
)


# ═══════════════════════════════════════════════════════════════
# _is_remote()
# ═══════════════════════════════════════════════════════════════
# True when a role is remote. Title/location match liberally; the
# body must hit a role-remoteness phrase AND not be negated — that
# recovers Adzuna/USAJOBS jobs that are remote but only say so in
# the description.
# ═══════════════════════════════════════════════════════════════
def _is_remote(title: str, loc: str, body: str) -> bool:
    if _REMOTE_RE.search(f"{title} {loc}"):
        return True
    if body and _REMOTE_BODY_RE.search(body) and not _REMOTE_NEG_RE.search(body):
        return True
    return False


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
        "remote": "remote" if _is_remote(title, loc, text) else "onsite",
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
"""


def init(path: str | None = None) -> None:
    """Create the schema if missing and import the legacy jobs.json ONCE."""
    with _conn(path) as c:
        c.executescript(_SCHEMA)
        empty = c.execute("SELECT 1 FROM jobs LIMIT 1").fetchone() is None
    if empty:
        _import_jobs_json(path)


def _import_jobs_json(path: str | None = None) -> int:
    p = Path(JOBS_JSON_PATH)
    if not p.exists():
        return 0
    try:
        snap = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return 0
    rows = snap.get("jobs", []) if isinstance(snap, dict) else []
    if rows:
        upsert_jobs(rows, path=path)
    return len(rows)


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
