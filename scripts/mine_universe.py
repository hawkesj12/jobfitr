#!/usr/bin/env python3
"""Regenerate `deploy/board-universe.json` from Common Crawl's COLUMNAR index.

    python scripts/mine_universe.py                 # newest crawl, the live hosts
    python scripts/mine_universe.py --crawl CC-MAIN-2026-30
    python scripts/mine_universe.py --dry-run       # print the counts, write nothing
    python scripts/mine_universe.py --warc-only     # diagnostic; see SUBSETS below

RUN THIS OFF-BOX. Common Crawl's CDX query host refuses the VPS (TCP 443 rejected in
53-87ms, measured repeatedly) while answering a laptop in 0.3s. The columnar index lives on
`data.commoncrawl.org`, which DOES answer from the box — but there is no reason to move the
query there: the crawl publishes roughly monthly, so this is a monthly human step whose
output is committed and deployed like code. Production reads the file and never talks to
Common Crawl. (It also cannot check for a newer crawl: `collinfo.json` is served by the
blocked host, so age-since-generation is the only staleness signal production can compute.)

WHY THE COLUMNAR INDEX INSTEAD OF THE CDX API. The CDX API takes a row `limit` and returns
rows SURT-sorted, so a capped query is an alphabetically-truncated slice — `discover.mine`'s
own docstring says so, and it is the whole reason discovery underperformed: one crawl holds
55,626 `*.myworkdayjobs.com` rows, and a 4,000-row cap yielded 158 boards, `2020companies`
through `baxter`. The columnar index is Parquet queried with SQL: no cap, and row-group
statistics on `url_surtkey` prune whole files off their footers without downloading them.

NO NEW DEPENDENCY. This shells out to the `duckdb` CLI rather than importing a Python module,
because the CLI is already installed locally and this script never runs in production. A
monthly local script is not worth a line in `pyproject.toml`.

--------------------------------------------------------------------------------
GREENHOUSE MIGRATED, AND THE FIRST VERSION OF THIS FILE GOT THE REASON WRONG
--------------------------------------------------------------------------------
`boards.greenhouse.io` now issues a permanent 301 to `job-boards.greenhouse.io` (verified
live on two slugs), and job-radar's `_PATTERNS` still names the retired host. So the pattern
must change. But the first draft of this script justified that with "the 425 boards the old
miner returned were redirect records" — WRONG, and worth recording because the wrong reason
would have led to a wrong design. Those 425 were **valid slugs, truncated by the row cap**.
The old host carries 1,789 distinct slugs across 12,075 rows; `mine` never filtered on
status, so its 301 records were yielding perfectly good slugs.

The real justification is a containment fact, and it is stronger: **the old host's slug set
is a strict SUBSET of the new host's.** 0 old-only, +2,547 new-only, 4,336 total. Swapping
loses nothing and gains 142%.

--------------------------------------------------------------------------------
SUBSETS: why this reads ALL of them and not just `subset=warc`
--------------------------------------------------------------------------------
`subset=warc` is successful (200) fetches only; redirects and errors live in
`subset=crawldiagnostics`. Querying warc alone is tempting because it is what made the
greenhouse migration legible — the retired host collapses to a clean zero.

It is also wrong, measured: of 4,336 new-host slugs, **1,009 have no 200 record at all**, and
a 25-slug probe of that tranche found **36% live with open roles** (`wehrtyou` 69 roles,
`assuredguaranty` 4). That is ~363 real employers — more than a quarter of the entire current
resolved ledger — discarded to make a query look clean. It also violates this system's own
rule that THE PROBE IS THE VERIFICATION: a slug's HTTP status in a month-old crawl is not
evidence about whether its board is live today.

So: read warc + crawldiagnostics, and let the probe decide. `subset=robotstxt` is excluded
because it holds nothing but robots.txt fetches, which are never boards — and which produced
a real junk slug (see NON-SLUGS). The warc/total ratio per host is recorded in `meta` anyway,
because the warc collapse is exactly what made this migration visible and is the tripwire for
the next one. Use `--warc-only` to reproduce that diagnostic.

--------------------------------------------------------------------------------
LEVER IS PERMANENTLY ABSENT, AND THAT IS NOT A BUG TO FIX
--------------------------------------------------------------------------------
`https://jobs.lever.co/robots.txt` contains `User-agent: CCBot` / `Disallow: /`. Common Crawl
honors it, so Lever boards will never appear in ANY crawl — while `jobs.lever.co/matchgroup`
returns 200, i.e. the boards exist and the crawler is forbidden. jobfitr's 179 Lever
resolutions all came from name-guessing, and always will.

An earlier draft listed lever in `HOSTS` anyway, "to keep the read path ready". That was the
silent zero this whole module exists to prevent, wearing a third costume: lever would
contribute 0, `meta.counts` would simply lack the key, and `universe.for_ats("lever")` would
return `[]` cheerfully forever. Lever is therefore OUT, with the reason recorded here, and
every host that IS listed must return a nonzero count or this script FAILS (see `--dry-run`
output and the assertion in `main`).

Checked at the same time, for the record: greenhouse, ashby, smartrecruiters and workable
have no CCBot `Disallow`. Lever is the only walled host of the six in job-radar's patterns.

--------------------------------------------------------------------------------
WORKDAY IS EXCLUDED BY DEFAULT, on measurement
--------------------------------------------------------------------------------
Its yield is real — a 59-board sample projects 69k-145k US-servable rows (bootstrap 90% CI) —
but **57% of what it calls "US-servable" is unverifiable-foreign**: its location is free text
with the street address welded on (`New York, NY - 225 Liberty Street`), so `city`/`state`/
`country` all parse to None and `salary_currency` is 0%, meaning neither half of
`servable_in_us` fires. Measured on three international tenants, 189 of 333 "servable" rows
were foreign, and every one of Capita's 65 was British. Shipping it would put ~60,000 foreign
jobs into a store the README calls US-only — an order of magnitude worse than the leak the
US-only work closed. `--workday` exists so the gate can be re-tested once a location
normalizer exists; it is not a flag to turn on casually.

`job-boards.eu` and `.anz` are excluded for a smaller version of the same reason: those
boards serve mostly European and ANZ roles that `servable_in_us` would discard after we had
already paid the probe requests. (`dedup.ats_from_url` returns None for the `.anz` host
anyway.)
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from job_radar.dedup import ats_from_url  # noqa: E402
from jobfitr.universe import DEFAULT_PATH  # noqa: E402

_ET = ZoneInfo("America/New_York")
DATA = "https://data.commoncrawl.org/"
COLLINFO = "https://index.commoncrawl.org/collinfo.json"

# (ats, host, surt-low, surt-high). The SURT range is what makes this cheap: the index is
# sorted by `url_surtkey` (reversed host), so a bounded range lets Parquet skip whole files
# on their footer statistics. Query a host WITHOUT a range and you scan the crawl.
#
# LEVER IS DELIBERATELY ABSENT — CCBot is Disallowed, see the module docstring. Adding it
# back would make the nonzero assertion below permanently red, which is the honest signal.
HOSTS: list[tuple[str, str, str, str]] = [
    (
        "greenhouse",
        "job-boards.greenhouse.io",
        "io,greenhouse,job-boards)",
        "io,greenhouse,job-boards0",
    ),
    ("ashby", "jobs.ashbyhq.com", "com,ashbyhq,jobs)", "com,ashbyhq,jobs0"),
]
# Wildcard subdomain, so the range covers the registered domain rather than one host.
WORKDAY = ("workday", "*.myworkdayjobs.com", "com,myworkdayjobs", "com,myworkdayjobt")

# Subsets to read. See the module docstring — warc alone drops ~363 live greenhouse boards.
SUBSETS = ("warc", "crawldiagnostics")

# ── the junk gate ────────────────────────────────────────────────────────────

# `ats_from_url` is the ONE slug parser and stays the primary gate, but it is a URL parser,
# not a slug validator: `ats_from_url('https://jobs.lever.co/robots.txt')` returns
# `('lever', 'robots.txt')`. An earlier draft claimed "anything the one parser refuses is
# dropped here", which overstated what the parser is for. These are the shapes it accepts
# that no employer board ever has.
_PERCENT = re.compile(r"%[0-9A-Fa-f]{2}")
_NOT_A_BOARD = {
    "robots.txt",
    "sitemap.xml",
    "favicon.ico",
    "index.html",
    "404.html",
    "embed",
    "api",
    "static",
    "assets",
}
# A DOT IN A SLUG IS NORMAL — do not reject on one.
#
# An earlier draft rejected any slug whose post-dot suffix was short and alphabetic, reasoning
# "no real board slug has a dot". That cost 68 LIVE ashby boards carrying 1,291 open roles:
# `checkout.com` (180), `roadsurfer.com` (135), `rivianvw.tech` (114), `kraken.com` (80),
# `jerry.ai` (53), `far.ai` (14), `magic.dev` (10). Ashby lets an employer use its own DOMAIN
# as the board slug, and the AI/crypto tier does it constantly.
#
# It was "verified" against the 1,390 resolved slugs in the live ledger, 0 of which contain a
# dot — a check that COULD NOT FAIL, because every ashby row in that ledger arrived by
# name-guessing and `name_variants` cannot emit a dot. Validating a filter against the one
# population structurally incapable of disproving it is not validation. A sample must come
# from the MINED set, which is the input the gate actually sees.
#
# So: reject only ACTUAL file extensions. Against the mined sets that leaves the right
# residue — greenhouse drops `llms.txt` and `zubiadrobots.txt`, ashby drops `llms.txt` and
# `sync.so.md`, and `www.qogita.com` survives (a `www.` prefix is worth one probe, not a
# silent delete).
_FILE_EXT = {
    "txt", "xml", "json", "md", "html", "htm", "ico", "css", "js", "php", "yml",
    "yaml", "csv", "pdf", "png", "jpg", "svg", "map", "gz", "zip",
}


def junk_reason(slug: str) -> str | None:
    """Why this slug is not an employer board, or None if it might be.

    Percent-encoding is the real one: 63 of ashby's 2,821 slugs (2.2%) are URL-encoded
    spaces or unexpanded templates — `anthos%20capital`, `%7byour_company%7d` — and they pass
    a length check untouched. Greenhouse's set is clean (11 of 4,336, 0.3%).
    """
    if not slug or len(slug) < 2 or len(slug) > 60:
        return "length"
    if _PERCENT.search(slug) or "{" in slug or "}" in slug or " " in slug:
        return "template-or-encoded"
    if slug.lower() in _NOT_A_BOARD:
        return "not-a-board-path"
    if "." in slug and slug.rsplit(".", 1)[-1].lower() in _FILE_EXT:
        return "file-extension"
    return None


# ── the crawl, and its index files ───────────────────────────────────────────


def newest_crawl() -> str:
    with urllib.request.urlopen(
        urllib.request.Request(COLLINFO, headers={"User-Agent": "jobfitr-universe"}),
        timeout=60,
    ) as r:
        return json.load(r)[0]["id"]


def index_files(crawl: str, subsets: tuple[str, ...]) -> dict[str, list[str]]:
    """The Parquet parts for one crawl, grouped by subset."""
    url = f"{DATA}crawl-data/{crawl}/cc-index-table.paths.gz"
    with urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "jobfitr-universe"}),
        timeout=120,
    ) as r:
        paths = gzip.decompress(r.read()).decode().splitlines()
    out: dict[str, list[str]] = {s: [] for s in subsets}
    for p in (x.strip() for x in paths):
        for s in subsets:
            if f"subset={s}/" in p:
                out[s].append(p)
    return out


# ── the query ────────────────────────────────────────────────────────────────


def run_query(files: list[str], hosts: list[tuple[str, str, str, str]]) -> list[str]:
    """One representative URL per (host, first-path-segment), via the duckdb CLI.

    Returning one real URL per slug rather than the slug itself is deliberate: the URL then
    goes through `dedup.ats_from_url`, the ONE slug parser in this system. job-radar's
    `discover.py` carries a comment about exactly this — it used to hold its own narrower
    copies of these regexes and `seed.py` a third set with an `&` bug, three parsers
    disagreeing on one URL. Extracting slugs in SQL here would make a fourth.
    """
    if not files:
        return []
    refs = ",\n  ".join(f"'{DATA}{p}'" for p in files)
    where = "\n   OR ".join(
        f"(url_surtkey >= '{lo}' AND url_surtkey < '{hi}')" for _, _, lo, hi in hosts
    )
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "boards.csv"
        sql = f"""INSTALL httpfs; LOAD httpfs;
SET http_keep_alive=true;
COPY (
  SELECT any_value(url) AS url
  FROM read_parquet([
  {refs}
  ])
  WHERE {where}
  GROUP BY url_host_name, split_part(ltrim(url_path, '/'), '/', 1)
) TO '{out}' (HEADER, DELIMITER ',');
"""
        script = Path(td) / "q.sql"
        script.write_text(sql)
        proc = subprocess.run(
            ["duckdb"],
            stdin=script.open(),
            capture_output=True,
            text=True,
            timeout=5400,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"duckdb failed:\n{proc.stderr[-2000:]}")
        rows = out.read_text(encoding="utf-8").splitlines()[1:]  # drop the header
    return [r.strip().strip('"') for r in rows if r.strip()]


# ── assembly ─────────────────────────────────────────────────────────────────


def build(urls: list[str]) -> tuple[list[dict], Counter]:
    """URLs → deduped board entries, gated by `ats_from_url` then `junk_reason`.

    Junk cannot cause a FALSE resolution — this lane binds no company NAME (the name arrives
    with the jobs), so the `Capital One` -> `capital` class of error is structurally
    impossible in it. What junk causes is a PERMANENT wasted request: `discover_new` writes
    a ledger row only for verified and refused boards, so a 404 is never cached and gets
    re-probed every night forever. That is why the filter is worth having.
    """
    seen, boards, rejected = set(), [], Counter()
    for u in urls:
        got = ats_from_url(u)
        if not got:
            rejected["unparseable"] += 1
            continue
        ats, slug = got
        why = junk_reason(slug)
        if why:
            rejected[why] += 1
            continue
        key = (ats, slug.lower())
        if key in seen:
            rejected["duplicate"] += 1
            continue
        seen.add(key)
        boards.append({"ats": ats, "slug": slug})
    boards.sort(key=lambda b: (b["ats"], b["slug"].lower()))
    return boards, rejected


def _bar(n: int, top: int) -> str:
    filled = round(n / top * 5) if top else 0
    return "█" * filled + "░" * (5 - filled)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mine_universe")
    ap.add_argument("--crawl", default=None, help="default: the newest crawl")
    ap.add_argument("--out", default=str(DEFAULT_PATH))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--warc-only",
        action="store_true",
        help="DIAGNOSTIC ONLY — successful fetches only. Drops ~363 live greenhouse "
        "boards; see the module docstring. Use it to compare against the full read and "
        "spot the next host migration.",
    )
    ap.add_argument(
        "--workday",
        action="store_true",
        help="include Workday — DISQUALIFIED today (57%% of its 'US-servable' rows are "
        "unverifiable-foreign); read the module docstring before using this",
    )
    args = ap.parse_args(argv)

    crawl = args.crawl or newest_crawl()
    hosts = HOSTS + ([WORKDAY] if args.workday else [])
    subsets = ("warc",) if args.warc_only else SUBSETS
    print(f"crawl {crawl}")
    print(f"hosts   : {', '.join(h[1] for h in hosts)}")
    print(
        f"subsets : {', '.join(subsets)}"
        + ("   [DIAGNOSTIC]" if args.warc_only else "")
    )

    grouped = index_files(crawl, subsets)
    per_subset: dict[str, int] = {}
    urls: list[str] = []
    for subset, files in grouped.items():
        print(f"\n{subset}: {len(files)} parquet parts — querying…")
        got = run_query(files, hosts)
        per_subset[subset] = len(got)
        urls.extend(got)
        print(f"   {len(got):,} distinct (host, slug) URLs")

    boards, rejected = build(urls)
    counts = Counter(b["ats"] for b in boards)
    top = max(counts.values()) if counts else 0
    print(f"\n{len(boards):,} boards after ats_from_url + junk_reason")
    for ats, n in sorted(counts.items()):
        pct = n / len(boards)
        print(f"   {ats:12} {n:6,}  {pct:5.0%} {_bar(n, top)}")
    if rejected:
        print(f"   rejected: {dict(rejected)}")

    # THE NONZERO ASSERTION. A requested host that yields nothing is the silent zero this
    # module exists to end — lever's CCBot Disallow is exactly that shape, which is why
    # lever is not in HOSTS. Fail loudly rather than committing a file with a missing key.
    missing = [ats for ats, _, _, _ in hosts if counts.get(ats, 0) == 0]
    if missing:
        print(
            f"\nFAILED: requested host(s) yielded ZERO boards: {', '.join(missing)}.\n"
            f"  Either the host migrated (check its live URL for a 301, as greenhouse did) "
            f"or it now Disallows CCBot in robots.txt (as jobs.lever.co does).\n"
            f"  A universe file with a silently missing ATS is the bug this script exists "
            f"to prevent — nothing was written.",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0
    if args.warc_only:
        print("\n--warc-only is a diagnostic; refusing to write a lossy universe")
        return 0

    doc = {
        "meta": {
            "crawl": crawl,
            "generated_at": datetime.now(_ET).isoformat(timespec="seconds"),
            "hosts": [h[1] for h in hosts],
            "subsets": list(subsets),
            # The migration tripwire. `warc` is 200-only; a host whose warc share collapses
            # toward zero between refreshes has moved, which is exactly how greenhouse's
            # move was spotted. Keep it even though the universe no longer filters on it.
            "urls_per_subset": per_subset,
            "counts": dict(sorted(counts.items())),
            "rejected": dict(sorted(rejected.items())),
            "source": "Common Crawl columnar index",
        },
        "boards": boards,
    }
    out = Path(args.out)
    out.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    print(f"\nwrote {out} ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
