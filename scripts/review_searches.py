#!/usr/bin/env python3
"""Read the search log and say whether the board is actually working for people.

    python scripts/review_searches.py /opt/jobfitr/data/searches.jsonl
    python scripts/review_searches.py searches.jsonl --days 7 --worst 15

The log answers a question the 57-profile harness structurally cannot: those profiles
are synthetic and author-written, so every quality number in this repo is conditional on
a mix one person invented. This reads what real people actually asked for.

Lead with the failures. A digest that opens with a healthy average is how a broken
search stays invisible for a month — the interesting lines are the ones that returned
nothing, returned a board of weak matches, or ran slow.

Deliberately stdlib-only so it runs on the box with no install. For ad-hoc slicing,
duckdb reads the file directly and is the better tool:

    duckdb -c "SELECT titles, delivered, score_max FROM read_json_auto('searches.jsonl')
               WHERE delivered = 0 ORDER BY ts DESC"
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

# A board whose BEST match is under this is a search that technically returned rows and
# practically returned nothing useful: under the ladder, 35 is "shares the head noun"
# and 15 is "some words overlap". Nobody wants a page of those.
WEAK_TOP = 55


def _bar(pct: float) -> str:
    filled = round(pct * 5)
    return "█" * filled + "░" * (5 - filled)


def _load(path: Path, days: int | None):
    cutoff = datetime.now() - timedelta(days=days) if days else None
    rows, bad = [], 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            if cutoff and datetime.fromisoformat(d["ts"]) < cutoff:
                continue
            rows.append(d)
        except Exception:  # noqa: BLE001 — a torn line must not stop the review
            bad += 1
    return rows, bad


def _pct(n, d):
    return f"{n / d:6.1%} {_bar(n / d)}" if d else "     — "


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", type=Path, nargs="?", default=Path("searches.jsonl"))
    ap.add_argument("--days", type=int, default=None, help="only the last N days")
    ap.add_argument(
        "--worst", type=int, default=10, help="how many bad searches to show"
    )
    ap.add_argument(
        "--include-probes",
        action="store_true",
        help="keep verify-slot.sh's pre-flip searches. Excluded by default: three "
        "synthetic searches per deploy would otherwise read as real demand, and "
        "'engineer'/'nurse'/'driver' would top the list of what people asked for.",
    )
    a = ap.parse_args()

    if not a.path.exists():
        print(f"no log at {a.path}", file=sys.stderr)
        return 1
    rows, bad = _load(a.path, a.days)
    probes = sum(1 for r in rows if r.get("probe"))
    if not a.include_probes:
        rows = [r for r in rows if not r.get("probe")]
    if not rows:
        print(f"{a.path}: no searches in range")
        return 0

    n = len(rows)
    empty = [r for r in rows if r["delivered"] == 0]
    weak = [r for r in rows if r["delivered"] and (r["score_max"] or 0) < WEAK_TOP]
    degraded = [r for r in rows if r.get("degraded")]
    ms = sorted(r["ms"] for r in rows)

    print(
        f"\n{a.path}  ·  {n:,} searches"
        + (f" (last {a.days}d)" if a.days else "")
        + (
            f"  ·  {probes} deploy probe(s) excluded"
            if probes and not a.include_probes
            else ""
        )
        + (f"  ·  {bad} unreadable line(s)" if bad else "")
    )
    print(f"  {rows[0]['ts']}  →  {rows[-1]['ts']}\n")

    # ── the failures, first ──────────────────────────────────────────────────
    print("THE FAILURES")
    print(f"  returned NOTHING        {len(empty):5,}  {_pct(len(empty), n)}")
    print(
        f"  best match was weak     {len(weak):5,}  {_pct(len(weak), n)}   (top < {WEAK_TOP})"
    )
    print(f"  served degraded         {len(degraded):5,}  {_pct(len(degraded), n)}")

    # ── how good the boards were ─────────────────────────────────────────────
    tops = [r["score_max"] for r in rows if r["score_max"] is not None]
    tiers = collections.Counter(
        j["tier"] for r in rows for j in r["top"][:1] if r["top"]
    )
    print("\nTHE BOARDS")
    if tops:
        print(
            f"  best-match score        median {statistics.median(tops):.0f}"
            f"   p10 {sorted(tops)[len(tops) // 10]:.0f}"
            f"   max {max(tops):.0f}"
        )
    print("  title rung of the #1 result:")
    for tier, count in sorted(tiers.items(), reverse=True):
        print(f"    {tier:>4}  {count:5,}  {_pct(count, sum(tiers.values()))}")

    print("\nTHE FUNNEL")
    cands = sorted(r["candidates"] for r in rows)
    deliv = sorted(r["delivered"] for r in rows)
    print(f"  candidates retrieved    median {statistics.median(cands):,.0f}")
    print(f"  delivered               median {statistics.median(deliv):,.0f}")
    print(
        f"  latency                 median {statistics.median(ms):,.0f} ms"
        f"   p95 {ms[int(len(ms) * 0.95)]:,.0f} ms   max {max(ms):,.0f} ms"
    )

    # ── what people actually want ────────────────────────────────────────────
    print("\nWHAT PEOPLE ASKED FOR")
    for label, key in (("titles", "titles"), ("boosts", "boosts")):
        c = collections.Counter(t for r in rows for t in (r.get(key) or []))
        top = "  ".join(f"{t}×{k}" for t, k in c.most_common(8))
        print(f"  {label:8s} {len(c):4,} distinct   {top}")
    locs = collections.Counter((r.get("location") or "(none)") for r in rows)
    print(
        f"  {'where':8s} {len(locs):4,} distinct   "
        + "  ".join(f"{t}×{k}" for t, k in locs.most_common(6))
    )

    # ── the specific searches to go look at ──────────────────────────────────
    losers = sorted(empty + weak, key=lambda r: (r["delivered"], r["score_max"] or 0))
    if losers:
        print(
            f"\nGO LOOK AT THESE  (worst {min(a.worst, len(losers))} of {len(losers)})"
        )
        for r in losers[: a.worst]:
            got = (
                f"{r['delivered']} jobs, best {r['score_max']}"
                if r["delivered"]
                else "NOTHING"
            )
            print(
                f"  {r['ts']}  {'+'.join(r['titles']) or '(no title)'!r:52.52} "
                f"@ {(r.get('location') or '-')!r:14.14} → {got}"
                f"   [{r['candidates']:,} retrieved]"
            )
            if r["top"]:
                print(
                    f"      top: {r['top'][0]['t']!r} @ {r['top'][0]['co']!r} "
                    f"({r['top'][0]['p']} pts, rung {r['top'][0]['tier']})"
                )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
