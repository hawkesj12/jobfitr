"""The cache layer. A scheduled *wide* harvest runs the job_radar engine with a
permissive config and writes the broad job universe to a jobs.json snapshot;
the web server scores that snapshot per-request. Nothing here runs on a user
request — this is the once-every-few-hours job.

Write is atomic (temp file + os.replace, mirroring job_radar/funnel.py) so an
interrupted harvest never leaves a half-written cache. Read is mtime-cached so
repeated requests don't re-parse the file.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from job_radar import config, engine
from job_radar.config import load_config

_ET = ZoneInfo("America/New_York")

# Per-row JD text is the biggest contributor to file size; cap it. Scoring only
# needs enough body to match keywords, and the UI shows a short snippet.
TEXT_CAP = 2000

# Where the cache lives by default. The server reads the same path (JOBS_PATH env
# override honored there); the harvester writes it.
DEFAULT_JOBS_PATH = "jobs.json"

# Config resolution order for the harvester, mirroring job_radar's CLI idiom.
_CONFIG_CANDIDATES = ("web-harvest.yaml", "web-harvest.example.yaml")


def _json_default(o):
    """Make stray non-JSON types (notably job_radar's `sources` set) serializable."""
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    return str(o)


def _clean_row(r: dict) -> dict:
    """Normalize one harvest row for JSON: set->list, truncate the JD body."""
    row = dict(r)
    src = row.get("sources")
    if isinstance(src, (set, frozenset)):
        row["sources"] = sorted(src)
    text = row.get("text")
    if isinstance(text, str) and len(text) > TEXT_CAP:
        row["text"] = text[:TEXT_CAP]
    return row


def build_snapshot(cfg, watchlist_path, out_path) -> dict:
    """Run a wide harvest and atomically write the snapshot. Returns the meta dict.

    `cfg` should be a permissive Config (broad titles, remote_only False, no
    excludes) so the cache holds the broad universe; the per-user narrow lens is
    applied later at request time.

    The company universe comes from the STORE, not a file. `watchlist_path` is now
    only a seed: its curated entries are imported into the ledger once, and from then
    on the ledger — which also holds everything resolution discovered — is the source
    of truth. That is what makes resolving a company actually produce jobs; without
    this the ledger was a table nothing read.
    """
    # Source fetchers read config.active() for UA/timeout; set it once here. Safe:
    # the harvester is a single-threaded batch job, unlike the concurrent server.
    config.set_active(cfg)

    companies = _harvest_universe(watchlist_path)
    rows, discovered, errors = engine.harvest(cfg, companies=companies)

    # Discovery now RETURNS candidates instead of appending to a file, so the store
    # is where they land. Best-effort: a ledger hiccup must not fail the harvest.
    if discovered:
        try:
            from . import store

            for d in discovered:
                store.record_resolution(
                    d.get("name") or d.get("slug", ""), d, variant="funnel"
                )
        except Exception as e:  # noqa: BLE001 — never fail the harvest over the ledger
            # But say so. Swallowing this silently meant a harvest could discover new
            # companies and fail to persist a single one, invisibly and forever. Same
            # print-style as _harvest_universe's sibling handler below.
            print(
                f"note: could not record {len(discovered)} discovered companies "
                f"to the ledger ({type(e).__name__}: {e})"
            )

    jobs = [_clean_row(r) for r in rows]

    source_ids = sorted({s for r in jobs for s in _as_list(r.get("sources"))})
    meta = {
        "harvested_at": datetime.now(_ET).isoformat(timespec="seconds"),
        "count": len(jobs),
        "sources": source_ids,
        "errors": errors,
    }
    snapshot = {"meta": meta, "jobs": jobs}

    # Feed the SQLite store — this is the demoted baseline inflow to the pool (the
    # per-search live fetch owns freshness now). upsert_jobs dedups by url and
    # refreshes last_seen, so a re-harvest keeps existing jobs alive rather than
    # thrashing them. Best-effort: a store hiccup must not fail the harvest.
    try:
        from . import store

        store.upsert_jobs(jobs)
    except Exception:  # noqa: BLE001 — the jobs.json write below is the source of truth
        pass

    # Keep writing jobs.json too: it's the rollback artifact (old code reads it) and
    # the store's one-time import seed on a fresh box.
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(out) + ".tmp")
    tmp.write_text(json.dumps(snapshot, default=_json_default) + "\n")
    os.replace(tmp, out)  # atomic on POSIX — an interrupted write leaves the old file
    return meta


def _harvest_universe(watchlist_path) -> list[dict]:
    """The companies to poll: the ledger, seeded once from the curated watchlist.

    Falls back to reading the watchlist directly if the store is unavailable, so a
    harvest never silently loses its whole depth lane over a store problem — the
    depth lane is ~40% of the corpus and 23x more productive per company than breadth.
    """
    try:
        from . import store

        store.init()
        if watchlist_path and not store.resolved_companies():
            n = store.seed_companies_from_watchlist(watchlist_path)
            if n:
                print(f"seeded {n} curated companies into the resolution ledger")
        universe = store.resolved_companies()
        if universe:
            return universe
    except Exception as e:  # noqa: BLE001
        print(f"note: ledger unavailable ({type(e).__name__}) — reading the watchlist")

    if not watchlist_path:
        return []
    try:
        with open(watchlist_path, encoding="utf-8") as f:
            return json.load(f).get("companies", [])
    except (OSError, json.JSONDecodeError):
        return []


def _as_list(v):
    if isinstance(v, (list, tuple, set, frozenset)):
        return list(v)
    if v:
        return [v]
    return []


# ── read side (used by the server) ───────────────────────────────────────────
_EMPTY = {
    "meta": {"count": 0, "harvested_at": None, "sources": [], "errors": []},
    "jobs": [],
}
_cache: dict[str, tuple[float, dict]] = {}


def load_snapshot(path: str | os.PathLike = DEFAULT_JOBS_PATH) -> dict:
    """Return the cached snapshot, re-reading only when the file's mtime changes.

    Missing file → an empty snapshot (a fresh box before the first harvest).
    """
    p = Path(path)
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError:
        return _EMPTY
    key = str(p)
    cached = _cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    snap = json.loads(p.read_text())
    _cache[key] = (mtime, snap)
    return snap


# ── CLI: jobfitr-snapshot ─────────────────────────────────────────────────────
def load_dotenv(path: str | os.PathLike = ".env") -> int:
    """Load KEY=VALUE lines from a .env into os.environ (no dependency).

    Existing environment variables win — an explicit `export` or systemd's
    EnvironmentFile is never clobbered by the file. Blank lines, `#` comments, and a
    leading `export ` are tolerated. Returns the number of keys set.

    A .env that is missing OR unreadable is a no-op. Both matter: this reads from the
    CURRENT WORKING DIRECTORY, and the CLI is routinely run as the `jobfitr` service
    user from a directory that user cannot stat (an admin's home, say). Letting that
    raise took down a whole resolution run before it read a single company — over an
    optional convenience file that production does not even use, since systemd
    supplies the environment.
    """
    p = Path(path)
    try:
        if not p.exists():
            return 0
        contents = p.read_text()
    except OSError:
        return 0
    set_count = 0
    for line in contents.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        if key and val and key not in os.environ:
            os.environ[key] = val
            set_count += 1
    return set_count


def _resolve_config(explicit: str | None) -> str | None:
    """Find the harvest config, relative to the CURRENT WORKING DIRECTORY.

    Falling through to None is a real cliff, not a soft default: job_radar's built-in
    config is narrow and tech-only, so a harvest launched from the wrong directory
    silently returns ~1,700 jobs instead of ~20,000 — no error, no warning, just a
    much smaller corpus. Measured on the box 2026-07-22. Hence the loud note below.
    """
    if explicit:
        return explicit
    for c in _CONFIG_CANDIDATES:
        if Path(c).exists():
            return c
    print(
        f"⚠ no harvest config found in {Path.cwd()} (looked for "
        f"{', '.join(_CONFIG_CANDIDATES)}) — falling back to job_radar's NARROW "
        "defaults. Expect a much smaller harvest; run from the repo root."
    )
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="jobfitr-snapshot",
        description="Run a wide harvest and write the jobs.json snapshot the web app serves.",
    )
    ap.add_argument(
        "--config", help="harvest YAML (default: web-harvest.yaml, then .example)"
    )
    ap.add_argument(
        "--watchlist", help="ATS watchlist JSON (optional; enables depth sources)"
    )
    ap.add_argument(
        "--out",
        default=DEFAULT_JOBS_PATH,
        help=f"output path (default: {DEFAULT_JOBS_PATH})",
    )
    args = ap.parse_args(argv)

    load_dotenv()  # keyed sources (Adzuna/USAJOBS) read these from os.environ
    cfg = load_config(_resolve_config(args.config))
    meta = build_snapshot(cfg, args.watchlist, args.out)
    print(
        f"snapshot: {meta['count']} jobs from {len(meta['sources'])} sources "
        f"→ {args.out} @ {meta['harvested_at']}"
        + (f"  ({len(meta['errors'])} source errors)" if meta["errors"] else "")
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
