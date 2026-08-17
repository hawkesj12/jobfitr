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

# Per-row JD text is the biggest contributor to file size; cap it. But it is also the
# BOOST EVIDENCE, and this cap is upstream of the store's — it truncates before
# jobs.json is written, so a body cut here can never be recovered by the scorer.
# Raised 2,000 -> 8,000 with store.BODY_CAP on 2026-08-10; the two must stay equal or
# the harvest and the live fetch feed the scorer different amounts of the same job.
TEXT_CAP = 8000

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
    # DROP the engine's copy. `rows` and `jobs` hold the same ~47,000 records — bodies and
    # all — and `rows` is dead from here on, so keeping the name alive costs a full second
    # copy at exactly the moment the write below needs the headroom. Measured: the harvest
    # peaked at 2,876 MB on a 363 MB snapshot.
    rows = None

    source_ids = sorted({s for r in jobs for s in _as_list(r.get("sources"))})
    # `count` is EVERY harvested row; `servable_count` is how many survive US-only
    # intake. Both are needed because they answer different questions, and conflating
    # them broke the deploy gate: `verify-slot.sh` compared a POST-filter pool_size
    # against this PRE-filter count at a 70% floor, so the two numbers had never
    # measured the same thing. Today's slack hid it — but on a freshly rebuilt slot,
    # which is exactly when the gate runs, an 18% intake drop lands the ratio near 0.67
    # and reports DO NOT FLIP for a slot that is fine.
    #
    # A gate that fails for its own reasons is worse than no gate: it trains you to
    # override it. See the same lesson recorded in verify-slot.sh about ARG_MAX.
    from . import store as _store  # local, like the resolution import above — avoids a cycle

    servable = sum(1 for r in jobs if _store.servable_in_us(r))
    meta = {
        "harvested_at": datetime.now(_ET).isoformat(timespec="seconds"),
        "count": len(jobs),
        "servable_count": servable,
        "sources": source_ids,
        "errors": _capped_errors(errors),
    }
    snapshot = {"meta": meta, "jobs": jobs}

    # EVERY error, in full, to stdout — systemd captures it, so the journal is the durable
    # record and `meta.errors` can stay bounded (see _capped_errors).
    for e in errors:
        print(f"  harvest error: {e}")

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
    # STREAM the write. `json.dumps(...)` built the entire ~363 MB document as one Python
    # string and then handed that string to write_text — a third full copy of the data, on
    # top of `jobs`, at the peak of the run. `json.dump(fp)` iterates and writes in chunks
    # instead, so the document is never resident.
    #
    # This is what capped board discovery: ~4,900 more boards projected a ~777 MB snapshot
    # and a ~6.1 GB peak against 7.9 GB of RAM. An OOM-kill here is quiet, too — the atomic
    # replace below means the store keeps the OLD file, so the pool freezes while
    # /api/health reads fine.
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(snapshot, fp, default=_json_default)
        fp.write("\n")
    os.replace(tmp, out)  # atomic on POSIX — an interrupted write leaves the old file
    return meta


# `meta.errors` is the ONE unbounded field in the snapshot's meta block, and it grows on
# exactly the night you least want trouble: one line per failing board. That matters because
# `store.snapshot_meta` reads meta from a BOUNDED PREFIX to keep /api/health O(1) — overflow
# the prefix and it falls back to parsing the whole document, which is the ~1 GB shape the
# streaming work removed. Measured: 16 errors today (0.001 MB), ~5,400 at a fully-resolved
# universe (0.392 MB), and overflow at ~14,000.
#
# So the list is capped HERE, at the writer, which makes the overflow structurally impossible
# rather than merely unlikely. Nothing is lost: every error is printed in full below, so the
# journal keeps the complete record while the snapshot carries a bounded sample plus a count.
META_ERROR_CAP = 200


def _capped_errors(errors: list) -> list:
    """A bounded sample of the harvest's errors, plus a marker for the remainder."""
    if len(errors) <= META_ERROR_CAP:
        return list(errors)
    extra = len(errors) - META_ERROR_CAP
    return [*errors[:META_ERROR_CAP], f"... and {extra:,} more (see the journal)"]


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
# Only the small meta block is ever retained — see load_meta for what the old
# whole-document cache cost (1,168 MB per web process).
_meta_cache: dict[str, tuple[float, dict]] = {}


def load_meta(path: str | os.PathLike = DEFAULT_JOBS_PATH) -> dict:
    """The snapshot's `meta` block, cached by mtime. THE PRODUCTION READ PATH.

    ── WHY THIS REPLACED `load_snapshot` FOR /api/health ────────────────────────
    `/api/health` needs five numbers. It used to get them from `load_snapshot`, which parsed
    the WHOLE document and cached it in `_cache` — permanently, keyed by mtime, so a slot held
    every job dict until the next harvest. Measured `[live prod]`: one call took a web process
    from **27 MB to 1,168 MB**, and with both blue-green slots warm that was **3,447 MB of
    7,941 MB** resident as two copies of the same document — including the IDLE slot, which
    serves nothing.
    That cache, not the harvest's peak, was the binding constraint on board discovery: at a
    777 MB snapshot it projects ~2.4 GB per slot, ~4.8 GB for the pair, before the harvest
    asks for anything.
    `store.snapshot_meta` streams the meta value and stops, so only the small dict is held.

    Missing file → the empty meta (a fresh box before the first harvest).
    """
    p = Path(path)
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError:
        return dict(_EMPTY["meta"])
    key = str(p)
    cached = _meta_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    from . import store  # local, to avoid an import cycle

    meta = store.snapshot_meta(p)
    _meta_cache[key] = (mtime, meta)
    return meta


def load_snapshot(path: str | os.PathLike = DEFAULT_JOBS_PATH) -> dict:
    """The WHOLE snapshot, parsed. Not for the server — see load_meta.

    DELIBERATELY UNCACHED. It used to memoize the parsed document in a module-level dict,
    which is what held 1,168 MB per web process; nothing in production needs the jobs array
    in memory any more (the store imports it via `store.sync_snapshot`, streaming). Kept for
    tests and one-off inspection, where holding it briefly is fine and retaining it is not.
    """
    p = Path(path)
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        return _EMPTY


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
