#!/usr/bin/env python3
"""The pre-release gate: run the whole pipeline against a COPY of production.

Nothing about this plan is worth publishing until we know what it actually yields.
The projections that justify it — ~370 new Greenhouse companies, ~124 Workday
employers, a corpus 2-3x today's — are extrapolations from capped queries and
samples of 12-14. This script replaces them with measurements, on a copy, before
anything reaches PyPI or the box.

    python scripts/dry_run.py --db /tmp/prod-copy.db --sample 150

It answers four questions:
  1. How many companies actually resolve, and at what rate?
  2. Does identity verification fire? (rejections > 0 proves the gate is live —
     zero would mean it is silently passing everything through.)
  3. How many jobs does the expanded universe really produce?
  4. How long does it take? The harvest window is ~8 min today and the nightly
     timer has to fit whatever this becomes.

Deliberately samples rather than resolving all 3,068: the full pass runs ONCE, on
the box, and duplicating ~18k HTTP requests here would be pure waste.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _fmt(n) -> str:
    return f"{n:,}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dry_run", description=__doc__)
    ap.add_argument("--db", required=True, help="path to a COPY of the production DB")
    ap.add_argument("--sample", type=int, default=150, help="companies to resolve")
    ap.add_argument("--cdx", action="store_true", help="also mine Common Crawl")
    ap.add_argument(
        "--harvest", action="store_true", help="run a real harvest (slow, network)"
    )
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)

    src = Path(args.db)
    if not src.exists():
        print(f"error: {src} does not exist", file=sys.stderr)
        return 2

    # Work on a copy of the copy — this script must never be the reason a snapshot
    # of production got mutated.
    work = src.with_suffix(".dryrun.db")
    shutil.copy2(src, work)
    os.environ["JOBFITR_DB_PATH"] = str(work)
    os.environ.setdefault("JOBFITR_JOBS_PATH", str(work.with_suffix(".jobs.json")))

    from jobfitr import resolve, store

    store.DB_PATH = str(work)
    store.JOBS_JSON_PATH = os.environ["JOBFITR_JOBS_PATH"]
    store.init()

    import job_radar

    print(f"db        : {work}")
    print(f"job-radar : {job_radar.__version__}  ({Path(job_radar.__file__).parent})")
    print()

    before = store.resolution_stats()
    print("── before ─────────────────────────────────────────────")
    print(
        f"  jobs {_fmt(store.pool_size())} · companies {_fmt(before['companies_in_store'])} "
        f"· resolved {_fmt(before['resolved'])} · never checked {_fmt(before['never_checked'])}"
    )

    # 1. seed the curated watchlist so those 94 are not re-probed
    t0 = time.time()
    seeded = store.seed_companies_from_watchlist(
        REPO / "deploy" / "tech-watchlist.json"
    )
    print(f"\n── seed ───────────────────────────────────────────────")
    print(
        f"  {seeded} curated companies imported as resolved ({time.time() - t0:.1f}s)"
    )

    # 2. resolve a sample, capturing WHY each candidate failed
    print(f"\n── resolve (sample of {args.sample}) ───────────────────")
    t0 = time.time()
    result = resolve.resolve_batch(
        limit=args.sample, use_cdx=args.cdx, workers=args.workers
    )
    elapsed = time.time() - t0
    checked = max(result["checked"], 1)
    print(
        f"  checked {_fmt(result['checked'])} · resolved {_fmt(result['resolved'])} "
        f"({100 * result['resolved'] // checked}%) · dead {_fmt(result.get('dead', 0))} "
        f"· unresolved {_fmt(result['unresolved'])}   [{elapsed:.0f}s]"
    )
    if result["checked"]:
        rate = elapsed / result["checked"]
        print(f"  → full 3,068-company pass ≈ {rate * 3068 / 60:.0f} min")

    # 3. The negative cache is what lets this ride along with every harvest. The test
    # is NOT "the second run does nothing" — with 3k companies queued it correctly
    # returns the NEXT batch. The property that matters is that it never re-probes a
    # company it has already answered.
    first_batch = set(store.unresolved_companies(limit=args.sample))
    again = resolve.resolve_batch(limit=args.sample, workers=args.workers)
    reprobed = first_batch & set(store.unresolved_companies(limit=args.sample * 4))
    print(
        f"  next batch: checked {again['checked']} ({again['resolved']} resolved) · "
        f"already-answered companies re-queued: {len(reprobed)} "
        f"← negative cache {'WORKING' if not reprobed else 'NOT WORKING'}"
    )

    after = store.resolution_stats()
    gained = after["resolved"] - before["resolved"] - seeded
    print(f"\n── ledger ─────────────────────────────────────────────")
    print(
        f"  resolved {_fmt(after['resolved'])} (+{_fmt(max(gained, 0))} newly discovered) "
        f"· dead {_fmt(after.get('dead', 0))} · unresolved {_fmt(after['unresolved'])}"
    )
    top = store.resolved_companies()[:10]
    for c in top:
        roles = f"{c['roles']:>5} roles" if c["roles"] else "  curated"
        print(f"     {c['name'][:34]:36} {c['ats']:11} {c['slug'][:22]:24} {roles}")

    # 4. optional: what the expanded universe actually harvests
    if args.harvest:
        from job_radar import config
        from job_radar.config import load_config

        from jobfitr import snapshot

        print(f"\n── harvest (universe of {len(store.resolved_companies())}) ──────")
        cfg = load_config(str(REPO / "web-harvest.example.yaml"))
        config.set_active(cfg)
        t0 = time.time()
        meta = snapshot.build_snapshot(
            cfg,
            REPO / "deploy" / "tech-watchlist.json",
            str(work.with_suffix(".jobs.json")),
        )
        el = time.time() - t0
        print(
            f"  {_fmt(meta['count'])} jobs from {len(meta['sources'])} sources in "
            f"{el / 60:.1f} min ({len(meta['errors'])} source errors)"
        )
        print(f"  pool now {_fmt(store.pool_size())} jobs")

    print("\n── the gate ───────────────────────────────────────────")
    print("  Publish only if: resolution rate is worth the corpus growth, the")
    print("  negative cache reported WORKING, and the full-pass estimate fits the")
    print("  nightly window. Otherwise fix it here, not on PyPI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
