"""Append-only record of what people asked for and what the board handed back.

WHY THIS EXISTS: every quality number in this repo is measured against 57 SYNTHETIC,
author-written profiles. That harness proves the scorer is internally consistent; it
cannot tell you whether a real person's real search returned jobs they would want,
because no real search has ever been recorded. This is the file that makes that
answerable — one line per search, reviewable weeks later.

WHAT IT DELIBERATELY DOES NOT CONTAIN. The product promises "no account, no tracking"
in three places (README, index.html, the app.js footer), and that promise is kept
literally, not loosely:

  * no IP address, no user agent, no referer, no cookie
  * no session or request id, and nothing else that could link two lines to one person
  * no free text from the chat interview

A line records the SHAPE of a search and the QUALITY of its answer. Two identical
searches an hour apart are indistinguishable from two different people, by design. That
costs a real thing — you cannot see one person's refine loop, so "did refining help?"
is not answerable from this file — and that cost is accepted rather than engineered
around, because the alternative is an identifier, and an identifier is the tracking the
front page says does not happen.

OFF UNLESS ASKED. No path in `JOBFITR_SEARCH_LOG` means no file is opened and no line is
written. A self-hoster who never sets it never logs anything, which is the correct
default for someone running this for themselves.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

# ── configuration ────────────────────────────────────────────────────────────

# Unset → disabled. Production sets it to the SHARED dir, /opt/jobfitr/data, not the
# per-slot one: a blue-green flip changes which process serves traffic, and a per-slot
# log would silently split the history in half at every deploy. The shared dir is
# already where failures.log lives, and jobfitr-web@.service needs it in ReadWritePaths.
LOG_PATH = os.environ.get("JOBFITR_SEARCH_LOG", "")

# Rotate at 32 MB into a single .1 sibling, so the log cannot fill a box that also holds
# the job store. At ~700 bytes a line that is roughly 48,000 searches per file — far more
# than this ever needs to hold, and the review window is weeks, not years.
MAX_BYTES = int(os.environ.get("JOBFITR_SEARCH_LOG_MAX_BYTES", str(32 * 1024 * 1024)))

# A single write() to a file opened O_APPEND is atomic only up to PIPE_BUF, which is
# 4096 on Linux. Both slots stay warm and both can be restarted mid-flip, so two
# processes really can hold this file open at once; keeping every line under that
# ceiling means a torn, unparseable line cannot happen. `top` is trimmed until it fits.
LINE_CAP = 4096

# How many of the delivered jobs to record. Raised from 5 to 10 on 2026-08-15 so a REAL
# search can be replayed through the judge panel the same way a synthetic profile is —
# `rank_test.py` grades a top-10, and a 5-row sample cannot fill one. This is the whole
# reason the log carries `url` and `loc` per row: the judge needs to see the listing, and
# every quality verdict this project holds was measured against 57 profiles one person
# invented. A logged real search is the first labelled data that is not imagined.
TOP_N = 10

_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════
# _percentile()
# ═══════════════════════════════════════════════════════════════
# Nearest-rank percentile over an already-sorted descending list.
# Avoids a statistics import for two numbers, and returns None on
# an empty board rather than raising — a zero-result search is a
# result worth logging, and is in fact the most interesting one.
# ═══════════════════════════════════════════════════════════════
def _percentile(desc: list[int], pct: float) -> int | None:
    if not desc:
        return None
    idx = min(len(desc) - 1, int(len(desc) * pct))
    return desc[idx]


# ═══════════════════════════════════════════════════════════════
# _title_tier()
# ═══════════════════════════════════════════════════════════════
# The title rung out of a card's `parts` receipt, which is a list
# of (label, value) pairs summing to `points`. Recorded separately
# from the total because the tier ladder is the thing under review:
# a board of 35s is a very different answer from a board of 100s,
# and the total alone cannot tell them apart once boosts are in.
# ═══════════════════════════════════════════════════════════════
def _title_tier(parts) -> int:
    for label, value in parts or []:
        if label in ("title", "related title"):
            return value
    return 0


# ═══════════════════════════════════════════════════════════════
# record()
# ═══════════════════════════════════════════════════════════════
# Append one search to the log. Never raises: a disk-full box, a
# read-only mount, or a systemd ReadWritePaths that was not updated
# must degrade to "no log line", never to a failed search. The
# whole point is observing the product, and an observer that can
# take the product down is worse than no observer.
# ═══════════════════════════════════════════════════════════════
def record(
    *,
    titles,
    related,
    boosts,
    exclude,
    location,
    remote_only,
    max_age_days,
    min_score,
    pool,
    candidates,
    kept,
    degraded,
    elapsed_ms,
    sources=None,
    overrides=None,
    probe=False,
) -> None:
    if not LOG_PATH:
        return
    try:
        scores = sorted((points for _, points, _, _ in kept), reverse=True)
        line = {
            # ET, not UTC — the box runs UTC and every other timestamp in this project
            # is Eastern. A log read weeks later must not need a zone conversion in the
            # reader's head to line up with when the site was actually busy.
            "ts": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S"),
            "titles": titles,
            "related": related,
            "boosts": boosts,
            "exclude": exclude,
            "location": location,
            "remote_only": bool(remote_only),
            "max_age_days": max_age_days,
            "min_score": min_score,
            # The funnel, which is where a bad search shows itself. `candidates` is what
            # FTS5 retrieved and `delivered` is what survived filters, dedupe, and
            # RESULT_CAP — a large gap between them is the signal that a filter is
            # eating the board, which is exactly the class of bug the panel found twice.
            "pool": pool,
            "candidates": candidates,
            "delivered": len(kept),
            "degraded": bool(degraded),
            "ms": round(elapsed_ms),
            "score_max": scores[0] if scores else None,
            "score_p50": _percentile(scores, 0.5),
            # WHICH SOURCES ANSWERED, and why one didn't. `live._fetch_all` swallows a
            # dead vendor by design (one bad source must not fail a search), so before
            # this the only symptom of an exhausted quota or a missing key was a thinner
            # board. Two Louisville searches an hour apart differed by 130 rows and
            # nothing recorded which source moved. Shape: {"adzuna": {"n": 130, "why": ""},
            # "google_jobs": {"n": 0, "why": "quota"}}. Absent when the search served a
            # fresh cache and called nobody.
            **({"sources": sources} if sources else {}),
            # WHAT THE CONTRACT HAD TO OVERRIDE in the posted answers, absent when it
            # overrode nothing — which is the overwhelming majority of searches.
            #
            # Every other field on this line is a POST-override value, and that made the
            # one thing worth counting invisible. On 2026-08-17 an interview emitted
            # `titles` and `exclude` both containing "ai engineer"; exclusions are a hard
            # filter, so the board came back entirely DevOps for an AI Engineer search.
            # The guard shipped that afternoon now cancels the exclusion — and the logged
            # line became indistinguishable from a search where the model never wrote it.
            # A defect that repairs itself invisibly still HAPPENED, and step 8's seven
            # mornings are measured from this file, so an unobservable defect reads as
            # seven clean mornings.
            #
            # Not a new privacy category: `titles`, `boosts` and the surviving `exclude`
            # terms are already model-written words from the same interview, so a
            # CANCELLED exclusion term is the same class of data as the kept ones beside
            # it. The docstring's "no free text from the chat interview" means the user's
            # prose, not the structured fields the interview produces.
            **({"overrides": overrides} if overrides else {}),
            # Only present when true. verify-slot.sh POSTs three real searches at a slot
            # before every flip, and without this flag those land in the log as genuine
            # user demand — "engineer", "nurse", "driver", three per deploy, which is
            # enough to dominate the digest this file exists to produce. The reviewer
            # drops them unless asked. Spoofable by any caller, and that is fine: there
            # is no incentive to forge one, and the cost of a forged probe is one missing
            # line in a quality log.
            **({"probe": True} if probe else {}),
            "top": [
                {
                    "t": (c.get("title") or "")[:120],
                    "co": (c.get("company") or "")[:80],
                    # Location and url are about the JOB, never the searcher — they carry
                    # no more about the person than the title already does, and without
                    # them a logged search cannot be rebuilt into a judging packet.
                    "loc": (c.get("location") or "")[:60],
                    "url": (c.get("url") or "")[:200],
                    "p": points,
                    "tier": _title_tier(parts),
                }
                for c, points, _, parts in kept[:TOP_N]
            ],
        }
        text = json.dumps(line, ensure_ascii=False, separators=(",", ":"))
        # Trim the sample rather than the search inputs: a line that has lost its top-5
        # still answers "did this search work at all", while one that has lost its
        # titles cannot be interpreted at all.
        while len(text.encode()) > LINE_CAP and line["top"]:
            line["top"].pop()
            text = json.dumps(line, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode()) > LINE_CAP:
            return

        path = Path(LOG_PATH)
        with _lock:
            # Rotate BEFORE writing, so the cap is a real ceiling rather than a ceiling
            # plus one line. Single generation on purpose — this is a review buffer, not
            # an audit trail, and a box with a 32 MB budget should not grow .1 .2 .3.
            if path.exists() and path.stat().st_size >= MAX_BYTES:
                path.replace(path.with_suffix(path.suffix + ".1"))
            with path.open("a", encoding="utf-8") as fh:
                fh.write(text + "\n")
    except Exception:  # noqa: BLE001 — see the block header: never break a search.
        return
