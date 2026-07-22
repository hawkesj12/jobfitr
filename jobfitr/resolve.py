"""Resolve the companies in the store to live ATS boards, and remember the answer.

The store knows ~3,000 employers by name, but a name is not a feed: the depth lane
needs an (ats, slug) key before it can pull a company's whole board. This module
closes that gap — for every company without a resolution, try to find its board,
then WRITE DOWN WHAT HAPPENED either way.

Recording the failures is what makes this affordable. Most of those employers are
federal agencies, hospitals, and staffing firms that will never have a Greenhouse
board; without a negative cache every run re-probes all of them forever. With one,
a run only touches companies it has never seen, so this can ride along with the
nightly harvest instead of needing its own schedule.

Two lanes, most-trustworthy first:

  1. MATCH  — the company's name against slugs already mined from Common Crawl.
              The slug is real by construction; the probe just confirms it.
  2. GUESS  — normalized variants of the name (see job_radar.discover.name_variants).
              Deliberately conservative: full-name forms only, never a bare first
              word, because `Capital One` -> `capital` resolves to a REAL board
              owned by someone else and the probe cannot tell the difference.

Nothing is written without a live probe returning >=1 role, and every resolution
stores the variant that won so a bad one can be found and undone.
"""

from __future__ import annotations

import argparse
import os

from job_radar import config, discover

from . import store
from .snapshot import load_dotenv

# Mining CDX on every run would be wasteful — the crawl updates roughly monthly.
# Off by default; the nightly path uses name-guessing over the (few) new companies.
MINE_LIMIT = int(os.environ.get("JOBFITR_CDX_MINE_LIMIT", "4000"))


def resolve_batch(
    limit: int = 500,
    use_cdx: bool = False,
    ats_list: list[str] | None = None,
    workers: int = 8,
    path: str | None = None,
) -> dict:
    """Resolve up to `limit` unresolved companies; persist every outcome.

    Returns a summary dict. `use_cdx=True` additionally mines Common Crawl and
    matches names against it — slower, much higher precision, worth it on a first
    full pass or a monthly refresh.
    """
    names = store.unresolved_companies(limit=limit, path=path)
    if not names:
        return {"checked": 0, "resolved": 0, "unresolved": 0}

    known = {
        (c["ats"], (c["slug"] or "").lower())
        for c in store.resolved_companies(path=path)
    }
    found: dict[str, dict] = {}

    if use_cdx:
        universe = []
        for ats in ats_list or ["greenhouse", "lever", "ashby"]:
            try:
                universe.extend(discover.mine(ats, limit=MINE_LIMIT))
            except Exception:  # noqa: BLE001 — a CDX hiccup must not kill the run
                continue
        for e in discover.match_known(names, universe):
            found.setdefault(e["name"], e)

    # Name-guessing for whatever the CDX match didn't cover.
    outcomes: list[dict] = []
    remaining = [n for n in names if n not in found]
    if remaining:
        for e in discover.from_names(
            remaining,
            ats_list=ats_list,
            known=known,
            workers=workers,
            outcomes=outcomes,
        ):
            found.setdefault(e["name"], e)

    # A board that REFUSED us (401/403/429) is not "we found nothing" — it exists and
    # has said no. Recording that as `dead` stops the nightly scheduler asking again
    # forever, which is both pointless and impolite.
    refused = {
        o["name"]
        for o in outcomes
        if o.get("outcome") == "refused" and o.get("name") and o["name"] not in found
    }

    for name in names:
        entry = found.get(name)
        store.record_resolution(
            name,
            entry,
            variant=(entry or {}).get("slug", ""),
            status="dead" if (not entry and name in refused) else None,
            path=path,
        )

    return {
        "checked": len(names),
        "resolved": len(found),
        "dead": len(refused),
        "unresolved": len(names) - len(found) - len(refused),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="jobfitr-resolve",
        description="Resolve store companies to live ATS boards; cache both outcomes.",
    )
    ap.add_argument("--limit", type=int, default=500, help="companies to check")
    ap.add_argument(
        "--cdx",
        action="store_true",
        help="also mine Common Crawl and match names against it (slower, better)",
    )
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--stats", action="store_true", help="print ledger state and exit")
    args = ap.parse_args(argv)

    load_dotenv()
    config.set_active(config.load_config(None))
    store.init()

    if args.stats:
        s = store.resolution_stats()
        print(
            f"companies in store: {s['companies_in_store']:,}  "
            f"resolved: {s['resolved']:,}  unresolved: {s['unresolved']:,}  "
            f"never checked: {s['never_checked']:,}"
        )
        return 0

    r = resolve_batch(limit=args.limit, use_cdx=args.cdx, workers=args.workers)
    print(
        f"jobfitr-resolve: checked {r['checked']:,} companies -> "
        f"{r['resolved']:,} resolved, {r['unresolved']:,} cached as unresolved"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
