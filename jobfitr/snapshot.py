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
    applied later at request time. `watchlist_path` may be None (skips ATS depth
    sources — breadth boards still run).
    """
    # Source fetchers read config.active() for UA/timeout; set it once here. Safe:
    # the harvester is a single-threaded batch job, unlike the concurrent server.
    config.set_active(cfg)
    rows, _discovered, errors = engine.harvest(cfg, watchlist_path)
    jobs = [_clean_row(r) for r in rows]

    source_ids = sorted({s for r in jobs for s in _as_list(r.get("sources"))})
    meta = {
        "harvested_at": datetime.now(_ET).isoformat(timespec="seconds"),
        "count": len(jobs),
        "sources": source_ids,
        "errors": errors,
    }
    snapshot = {"meta": meta, "jobs": jobs}

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(out) + ".tmp")
    tmp.write_text(json.dumps(snapshot, default=_json_default) + "\n")
    os.replace(tmp, out)  # atomic on POSIX — an interrupted write leaves the old file
    return meta


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

    Existing environment variables win — an explicit `export` or the launchd
    plist's EnvironmentVariables is never clobbered by the file. Blank lines,
    `#` comments, and a leading `export ` are tolerated; missing file is a no-op.
    Returns the number of keys set (for the CLI to report).
    """
    p = Path(path)
    if not p.exists():
        return 0
    set_count = 0
    for line in p.read_text().splitlines():
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
    if explicit:
        return explicit
    for c in _CONFIG_CANDIDATES:
        if Path(c).exists():
            return c
    return None  # pure defaults (still a valid, if narrow, harvest)


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
