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

# What Common Crawl is mined for. Workday belongs here and ONLY here: its three-part
# key (tenant, wdN host, site slug) is visible in a crawled URL but unguessable from a
# company name, so CDX is the only route to the enterprise/government/healthcare tier.
CDX_ATS = ["greenhouse", "lever", "ashby", "workday"]
# What a company NAME can be guessed into — single-key ATSs only.
GUESSABLE_ATS = ["greenhouse", "lever", "ashby"]


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
        for ats in ats_list or CDX_ATS:
            try:
                universe.extend(discover.mine(ats, limit=MINE_LIMIT))
            except Exception:  # noqa: BLE001 — a CDX hiccup must not kill the run
                continue
        # Matching a NAME against the mined universe is the only way Workday is
        # reachable at all: its site slug ('BWCareers', 'Agent-Staff') cannot be
        # guessed from a company name, but CDX saw the whole tenant/host/site triple,
        # so a name that matches the tenant inherits the other two. That is the entire
        # non-tech tier — insurance, manufacturing, municipalities, national labs.
        for e in discover.match_known(names, universe):
            found.setdefault(e["name"], e)

    # Name-guessing for whatever the CDX match didn't cover. Deliberately NOT Workday:
    # a guessed tenant without the right site slug is a wasted request, not a company.
    outcomes: list[dict] = []
    remaining = [n for n in names if n not in found]
    if remaining:
        for e in discover.from_names(
            remaining,
            ats_list=[a for a in (ats_list or GUESSABLE_ATS) if a != "workday"],
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


def discover_new(
    ats_list: list[str] | None = None,
    limit: int = MINE_LIMIT,
    workers: int = 8,
    path: str | None = None,
) -> dict:
    """Add companies we have NEVER heard of, straight from Common Crawl.

    The mirror image of resolve_batch, and the half that actually carries the
    non-tech tier. resolve_batch walks employers already in the store and asks "does
    this one have a board?" — so it can only ever find companies an aggregator
    already showed us. But the Workday universe (Barry-Wehmiller, Argonne, Baltimore
    City, Ace Hardware) never appears in the store at all: those employers do not
    syndicate to the free boards, which is precisely why they are missing. Matching
    names against CDX cannot reach them, because there is no name to match.

    So this goes the other way: mine the index for every board that exists, drop the
    ones already in the ledger, probe the rest, and keep what answers. No identity
    check — there is no company name to verify against, and none is claimed; the
    company name arrives with the jobs themselves.
    """
    known = {
        (c["ats"], (c["slug"] or "").lower(), (c.get("site") or "").lower())
        for c in store.resolved_companies(path=path)
    }
    candidates, mine_errors = [], []
    for ats in ats_list or CDX_ATS:
        try:
            mined = discover.mine(ats, limit=limit)
        except Exception as e:  # noqa: BLE001 — one bad pattern must not kill the sweep
            # Say so. Swallowing this made "Common Crawl is refusing us" look exactly
            # like "Common Crawl had nothing new" — a silent zero, which is the same
            # failure shape as the frozen pool. CDX rate-limits by IP after a heavy
            # sweep, so this is an expected condition, not an exotic one.
            mine_errors.append(f"{ats}: {type(e).__name__}")
            continue
        for c in mined:
            key = (c["ats"], c["slug"].lower(), (c.get("site") or "").lower())
            if key not in known:
                known.add(key)
                candidates.append(c)

    outcomes: list[dict] = []
    verified = discover.probe(candidates, workers=workers, outcomes=outcomes)
    for e in verified:
        store.record_resolution(_board_name(e), e, variant="cdx-discovery", path=path)
    # ONLY a hard refusal is terminal. A 429 is the rate limiter asking us to slow
    # down — recording that as dead would blacklist good employers wholesale, since
    # sweeping a few hundred Workday tenants reliably trips it.
    refused = [o for o in outcomes if o.get("outcome") == "refused"]
    for o in refused:
        store.record_resolution(_board_name(o), None, status="dead", path=path)
    throttled = sum(1 for o in outcomes if o.get("outcome") == "throttled")
    return {
        "mined": len(candidates),
        "added": len(verified),
        "dead": len(refused),
        "throttled": throttled,
        "roles": sum(e.get("roles", 0) for e in verified),
        "mine_errors": mine_errors,
    }


def _board_name(entry: dict) -> str:
    """The ledger identity of a discovered BOARD.

    For most ATSs a company has one board and the slug is enough. Workday tenants
    routinely run several — Ace Hardware has External, ARG_External and AHHS_External,
    all different divisions with different jobs — so keying on the tenant alone
    collapses them and silently keeps only the last one seen.
    """
    if entry.get("ats") == "workday" and entry.get("site"):
        return f"{entry['slug']}/{entry['site']}"
    return entry.get("name") or entry["slug"]


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
    ap.add_argument(
        "--discover",
        action="store_true",
        help="add NEW companies from Common Crawl that the store has never seen "
        "(the only route to the Workday/enterprise tier)",
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
            f"dead: {s.get('dead', 0):,}  never checked: {s['never_checked']:,}"
        )
        return 0

    if args.discover:
        d = discover_new(workers=args.workers)
        print(
            f"jobfitr-discover: mined {d['mined']:,} unknown boards -> "
            f"{d['added']:,} added ({d['roles']:,} roles), {d['dead']:,} refused, "
            f"{d['throttled']:,} throttled"
        )
        if d["mine_errors"]:
            # LOUD: a discovery run that mined nothing because Common Crawl refused
            # us is a completely different event from one that found nothing new.
            print(
                f"  ⚠ Common Crawl unreachable for {len(d['mine_errors'])} pattern(s): "
                f"{', '.join(d['mine_errors'])}"
            )
            print("    → no new companies were discovered this run; retry later.")

    r = resolve_batch(limit=args.limit, use_cdx=args.cdx, workers=args.workers)
    print(
        f"jobfitr-resolve: checked {r['checked']:,} companies -> "
        f"{r['resolved']:,} resolved, {r['dead']:,} dead, "
        f"{r['unresolved']:,} cached as unresolved"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
