"""The set of ATS boards known to EXIST — mined offline, committed, deployed like code.

WHY THIS FILE EXISTS AT ALL. Board discovery used to mine Common Crawl live, on the box,
every night. That path was dead for three separate reasons at once, and each one alone
would have justified moving it:

  1. THE BOX IS BLOCKED. `index.commoncrawl.org` refuses the VPS's datacenter IP — TCP 443
     rejected in 53-87ms, measured repeatedly, while `data.commoncrawl.org`,
     `boards-api.greenhouse.io` and `api.ashbyhq.com` all answer 200 from the same host and
     egress is `policy ACCEPT`. From a laptop the same host answers in 0.3s. So the nightly
     logged `mined 0 unknown boards` + `⚠ Common Crawl unreachable for 4 pattern(s)`, which
     is why `host`/`site` were NULL on all 7,940 ledger rows and Workday had never resolved
     once.
  2. THE MINE WAS ALPHABETICALLY TRUNCATED. `discover.mine`'s `limit` caps CDX **rows, not
     boards**, and CDX returns them SURT-sorted — its own docstring says so. One crawl holds
     55,626 `*.myworkdayjobs.com` rows; a 4,000-row cap returned 158 boards, tenants
     `2020companies` through `baxter`. Every yield rate this project ever computed from a
     mine was a rate over the A-B slice.
  3. IT WAS ALWAYS WASTED WORK. Common Crawl publishes roughly monthly. Mining nightly
     re-derived the same answer ~30 times per new fact, over a network dependency that could
     fail silently.

So the universe is now generated OFF-box by `scripts/mine_universe.py` (SQL over Common
Crawl's columnar Parquet index, no row cap) and committed. The nightly does not touch
Common Crawl at all: it reads this file and probes. Probing was never the broken part —
`boards-api.greenhouse.io` answers fine from the box.

WHAT THIS COSTS, STATED PLAINLY. A board that appears between refreshes is invisible until
someone regenerates the file. That is a real regression against a working live mine — but
the live mine was not working, and a monthly file beats a nightly zero. The cost is paid
back by `age_days()`: staleness is REPORTED, not silent.

THE SILENT-ZERO RULE. This project's signature bug is a failure that looks like an empty
success — `_s(job.get(...))` returning `''` for a dropped schema field, `record()` swallowing
an EROFS, `mined 0 unknown boards` reading as "nothing new" when it meant "we were refused".
So a MISSING universe file RAISES; it does not return an empty list. An empty list here would
reproduce exactly the failure this module was written to end.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# Committed beside the watchlist it sits next to conceptually: both are static inputs the
# harvest reads, versioned with the code and reviewable in a diff. NOT the ledger — the
# ledger records what we PROBED and RESOLVED; this is upstream input, and mixing the two is
# what produced the `board:`-key vs name-key duplication (377 companies simultaneously
# resolved and unresolved).
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "deploy" / "board-universe.json"

# Common Crawl lands a new crawl roughly monthly. 45 days means a refresh was MISSED, not
# merely that one is due — the extra fortnight keeps a normal late crawl from crying wolf.
STALE_AFTER_DAYS = 45

# Why a given ATS is absent, when it is absent BY DESIGN. Anything not listed here is absent
# for an unknown reason, which is a different and more alarming thing — so it gets the generic
# message rather than borrowing someone else's excuse.
_WHY_ABSENT = {
    "lever": (
        "lever can NEVER be mined: jobs.lever.co/robots.txt sets `User-agent: CCBot` / "
        "`Disallow: /`, and Common Crawl honors it. The boards are live (/matchgroup returns "
        "200); the crawler is forbidden. Lever resolutions come from name-guessing only."
    ),
    "workday": (
        "workday is withheld pending a location normalizer — 57% of its 'US-servable' rows "
        "are unverifiable-foreign, because its location is free text with the street address "
        "attached, so country/city/state all parse to None"
    ),
}

# The ATSs whose absence is EXPECTED. `discover_new` and `resolve_batch` report these as one
# quiet informational line; anything else absent, or a missing file, keeps the ⚠.
EXPECTED_ABSENT = frozenset(_WHY_ABSENT)


class UniverseUnavailable(RuntimeError):
    """The universe file is missing or unreadable.

    Raised rather than returning [] on purpose — see the module docstring's silent-zero
    rule. Carries the regeneration command, because the whole point is that the human step
    is visible when it is skipped.
    """


class UniverseNotQueried(UniverseUnavailable):
    """The file is fine, but this ATS was never mined into it.

    A SEPARATE case from "the file is missing", and it exists because the first draft of
    this module shipped the exact bug it was written to prevent. `CDX_ATS` asks for four
    ATSs; the generator produces two. Lever can never be produced at all —
    `jobs.lever.co/robots.txt` sets `User-agent: CCBot` / `Disallow: /`, so Common Crawl is
    forbidden from ever seeing a Lever board — and Workday is deliberately withheld until a
    location normalizer exists. Both of those are correct, and both were INVISIBLE: the key
    was simply absent from `meta.counts` and `for_ats` returned [] cheerfully forever.

    So an ATS the generator never looked for raises, the caller reports it, and the nightly
    log carries one line per dark lane. A deliberate omission that nobody can see is
    indistinguishable from a broken one.
    """


# ── loading ──────────────────────────────────────────────────────────────────


def _path(path: str | None = None) -> Path:
    return Path(path or os.environ.get("JOBFITR_BOARD_UNIVERSE") or DEFAULT_PATH)


def load(path: str | None = None) -> dict:
    """The whole file: `{"meta": {...}, "boards": [...]}`.

    Raises UniverseUnavailable if it is missing or malformed.
    """
    p = _path(path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise UniverseUnavailable(
            f"no board universe at {p} — regenerate with "
            f"`python scripts/mine_universe.py` (runs OFF-box; Common Crawl's CDX host "
            f"refuses the VPS)"
        ) from e
    except (OSError, json.JSONDecodeError) as e:
        raise UniverseUnavailable(f"board universe at {p} is unreadable: {e}") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("boards"), list):
        raise UniverseUnavailable(f"board universe at {p} has no 'boards' list")
    return doc


def for_ats(ats: str, path: str | None = None) -> list[dict]:
    """Every known board for one ATS, shaped exactly like `discover.mine` returns.

    A drop-in for `discover.mine(ats, ...)`: `{"ats", "slug"}` plus `host`/`site` for
    Workday, whose three-part key is unguessable and therefore only ever comes from a
    crawled URL.

    Raises UniverseNotQueried if the generator never mined this ATS — see that exception.
    """
    doc = load(path)
    counts = doc.get("meta", {}).get("counts")
    # `counts` absent entirely = a hand-made or pre-`counts` file; do not invent a failure
    # for it. But a `counts` map that omits this ATS is a stated fact: it was not mined.
    if isinstance(counts, dict) and ats not in counts:
        # The explanation is looked up, not hardcoded. An earlier version appended the
        # lever/workday reasons to EVERY absent ATS, so if greenhouse ever went missing the
        # error would have explained Lever's robots.txt.
        why = _WHY_ABSENT.get(ats, "not mined by scripts/mine_universe.py")
        raise UniverseNotQueried(
            f"'{ats}' is not in the board universe "
            f"(mined: {', '.join(sorted(counts)) or 'nothing'}) — {why}"
        )
    out = []
    for b in doc["boards"]:
        if b.get("ats") != ats or not b.get("slug"):
            continue
        e = {"ats": b["ats"], "slug": b["slug"]}
        for k in ("host", "site"):
            if b.get(k):
                e[k] = b[k]
        out.append(e)
    return out


def age_days(path: str | None = None) -> float | None:
    """How long since the file was generated, or None if it does not say.

    Read by `jobfitr-resolve` so a forgotten refresh is announced in the nightly log
    instead of quietly shrinking discovery to whatever was true last quarter.
    """
    stamp = load(path).get("meta", {}).get("generated_at", "")
    try:
        then = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=_ET)
    return (datetime.now(_ET) - then).total_seconds() / 86400


def describe(path: str | None = None) -> str:
    """One line for the nightly log: where it came from, how old, how big."""
    doc = load(path)
    meta = doc.get("meta", {})
    counts = meta.get("counts") or {}
    age = age_days(path)
    parts = ", ".join(f"{k} {v:,}" for k, v in sorted(counts.items()))
    line = (
        f"universe: {len(doc['boards']):,} boards ({parts}) "
        f"from {meta.get('crawl', 'an unrecorded crawl')}"
    )
    if age is None:
        return line + " — generated-at MISSING, cannot tell if it is stale"
    line += f", generated {age:.0f}d ago"
    if age > STALE_AFTER_DAYS:
        line += (
            f"  ⚠ STALE (>{STALE_AFTER_DAYS}d) — a new crawl has almost certainly landed; "
            f"regenerate OFF-box with `python scripts/mine_universe.py`"
        )
    return line
