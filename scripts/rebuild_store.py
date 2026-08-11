#!/usr/bin/env python3
"""Rebuild jobs.db from scratch on the current schema, and report what landed.

The store is a CACHE, not a system of record: rows evict at 14 days unseen / 60
days posted, and a harvest refills the whole thing in ~4 minutes. So a schema
change is a rebuild, not a migration — no ALTER TABLE, no backfill.

That is true of `jobs`. It is NOT true of everything in the file, which is the one
thing this script exists to get right: the `companies` resolution ledger is also in
here, and it is the opposite of disposable (see _CARRY_TABLES). The old file is
renamed rather than removed both to carry that forward and so you can still look at
yesterday's store when today's looks wrong.

    python scripts/rebuild_store.py                 # the real store
    python scripts/rebuild_store.py --db /tmp/x.db  # a scratch copy first

It answers the only question that decides whether the rebuild worked: **did the
new columns actually get filled?** A column reading 0% is a mapping bug — a key
that the engine renamed, or a normalize_job entry pointing at a name nothing
sends — and it is invisible in a row count, which looks identical either way.

The comparison figures to hold it against are in
_private/raw/harvest-2026-08-08T0926/COVERAGE.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
_ET = ZoneInfo("America/New_York")


def _fmt(n) -> str:
    return f"{n:,}"


def _bar(pct: float) -> str:
    filled = round(pct / 100 * 5)
    return "█" * filled + "░" * (5 - filled)


def _sidecars(db: Path) -> list[Path]:
    """SQLite's WAL companions. Leaving a -wal behind next to a freshly created
    database is how a 'rebuilt' store comes back holding the old one's pages."""
    return [db.with_name(db.name + suffix) for suffix in ("-wal", "-shm")]


def _prune_backups(db: Path, keep: int) -> list[Path]:
    backups = sorted(db.parent.glob(db.name + ".bak-*"), reverse=True)
    dropped = backups[keep:]
    for p in dropped:
        p.unlink()
    return dropped


# `jobs` is disposable — a harvest refills it in ~4 minutes. These two are NOT, and
# the first draft of this script threw both away:
#
#   companies  the ATS resolution ledger. Its VALUE IS THE CACHED NEGATIVES — "probed,
#              nothing found" for ~3k federal agencies, hospitals and staffing firms.
#              Rebuilding it means re-probing every one of them, ~18k HTTP requests
#              (scripts/dry_run.py), and until it finishes the harvest falls back to
#              the watchlist: measured on this machine, 1,776 jobs instead of ~31,000.
#              A cache whose whole point is the expensive answer is not disposable.
#   meta       specifically `live_fetch_usage`, the persisted daily API ceiling. It is
#              stored on disk precisely so a restart cannot silently zero it; wiping it
#              on rebuild is that same bug wearing a different hat.
#
# Deliberately NOT carried: `searches` (the per-(title,location) freshness clock — the
# jobs it vouched for are gone, so a re-fetch is CORRECT), and the snapshot mtime, so
# init() re-imports jobs.json instead of thinking it already has it.
# The columns a 0.7.0 harvest fills on EVERY row, so a zero here means the snapshot
# predates the engine rather than that the sources were thin. That distinction is the
# whole job of the gate below: an earlier cut failed on ANY completely-empty column and
# tripped on `salary_estimated_min/max` — Adzuna's model guesses, ~3% fill in a big
# harvest and legitimately zero in a small one. A gate that cries wolf on a healthy
# rebuild teaches you to pass --allow-empty-columns reflexively, which is worse than no
# gate at all.
_ALWAYS_FILLED = ("title_root", "direct_apply")

_CARRY_TABLES = ("companies",)
_CARRY_META_KEYS = ("live_fetch_usage",)


def _carry_forward(backup: Path, db: Path) -> dict[str, int]:
    carried: dict[str, int] = {}
    with sqlite3.connect(db) as c:
        c.execute("ATTACH DATABASE ? AS old", (str(backup),))
        have = {r[0] for r in c.execute("SELECT name FROM old.sqlite_master")}
        for t in _CARRY_TABLES:
            if t not in have:
                continue
            # NAME THE COLUMNS. `SELECT *` binds positionally, and this is the script
            # that exists FOR schema changes — the one schema it could not survive a
            # change to was the very table it is preserving. Taking the intersection of
            # old and new also means a column added to `companies` does not break the
            # carry-forward; it just arrives empty, which is correct for a new column.
            new_cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
            old_cols = {r[1] for r in c.execute(f"PRAGMA old.table_info({t})")}
            shared = [col for col in new_cols if col in old_cols]
            if not shared:
                continue
            cols = ",".join(f'"{col}"' for col in shared)
            n = c.execute(
                f"INSERT OR REPLACE INTO {t}({cols}) SELECT {cols} FROM old.{t}"
            ).rowcount
            carried[t] = n
            if len(shared) != len(new_cols):
                missing = sorted(set(new_cols) - old_cols)
                print(f"  note       {t}: {', '.join(missing)} not in the old schema")
        if "meta" in have:
            keys = ",".join("?" * len(_CARRY_META_KEYS))
            n = c.execute(
                f"INSERT OR REPLACE INTO meta SELECT * FROM old.meta WHERE key IN ({keys})",
                _CARRY_META_KEYS,
            ).rowcount
            if n:
                carried["meta"] = n
        # DETACH cannot run inside a transaction, and python's sqlite3 has an open
        # one by now because of the INSERTs above.
        c.commit()
        c.execute("DETACH DATABASE old")
    return carried


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rebuild_store", description=__doc__)
    ap.add_argument("--db", help="store to rebuild (default: the configured DB_PATH)")
    ap.add_argument("--jobs", help="snapshot to import (default: the configured path)")
    ap.add_argument(
        "--keep-backups", type=int, default=3, help="older backups to prune (default 3)"
    )
    ap.add_argument(
        "--allow-empty-columns",
        action="store_true",
        help="exit 0 even if a column is completely unfilled (stale-snapshot rebuilds)",
    )
    ap.add_argument(
        "--no-backup",
        action="store_true",
        help="discard the old store once carried forward (scratch DBs only)",
    )
    args = ap.parse_args(argv)

    if args.db:
        os.environ["JOBFITR_DB_PATH"] = args.db
    if args.jobs:
        os.environ["JOBFITR_JOBS_PATH"] = args.jobs

    from jobfitr import store

    db = Path(store.DB_PATH)
    jobs_json = Path(store.JOBS_JSON_PATH)

    # Refuse rather than produce an empty store. A rebuild that silently yields
    # zero rows looks like a success in every log line and takes the board down.
    if not jobs_json.exists():
        print(
            f"error: {jobs_json} does not exist — nothing to rebuild FROM.\n"
            f"       Run a harvest first (jobfitr-snapshot), or pass --jobs.",
            file=sys.stderr,
        )
        return 2
    try:
        snap = json.loads(jobs_json.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"error: {jobs_json} is unreadable ({e})", file=sys.stderr)
        return 2
    # `sync_snapshot` only ever reads the dict form, so a bare list would pass this
    # precheck and then import zero rows. Use the same rule it does.
    rows_in = len(snap.get("jobs", [])) if isinstance(snap, dict) else 0
    if not rows_in:
        print(f"error: {jobs_json} holds no jobs", file=sys.stderr)
        return 2

    print(f"\n── rebuild {db} ──────────────────────────────")
    print(f"  snapshot   {jobs_json} — {_fmt(rows_in)} rows")

    backup: Path | None = None
    if db.exists():
        # Fold the WAL back into the main file FIRST. The store runs in WAL mode, so
        # recent writes — including the ledger this rebuild exists to preserve — may
        # live only in jobs.db-wal. Renaming the main file and then deleting the
        # sidecars, which is the obvious order, silently discards them.
        with sqlite3.connect(db) as c:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        stamp = datetime.now(_ET).strftime("%Y-%m-%dT%H%M")
        backup = db.with_name(f"{db.name}.bak-{stamp}")
        # Renamed, never deleted outright: it is both the rollback artifact and the
        # source the ledger is carried forward from a few lines below.
        db.replace(backup)
        print(f"  backup     {backup.name}")
        dropped = _prune_backups(db, args.keep_backups)
        if dropped:
            print(f"  pruned     {len(dropped)} older backup(s)")
    for p in _sidecars(db):
        p.unlink(missing_ok=True)

    store.init()
    if backup:
        for table, n in _carry_forward(backup, db).items():
            print(f"  carried    {_fmt(n)} {table} rows forward from the backup")
        if args.no_backup:
            backup.unlink()
            for p in _sidecars(backup):
                p.unlink(missing_ok=True)

    stored = store.pool_size()
    print(f"\n  stored     {_fmt(stored)} rows", end="")
    if rows_in - stored:
        # PRIMARY KEY(url) collapsing a few dozen is normal — the same posting is
        # reachable from more than one board. A large number is not.
        print(f"  ({_fmt(rows_in - stored)} duplicate URLs collapsed)")
    else:
        print()

    with store._conn() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(jobs)")]
        fills = {
            col: c.execute(
                f'SELECT count(*) FROM jobs WHERE "{col}" IS NOT NULL AND "{col}" <> ""'
            ).fetchone()[0]
            for col in cols
        }
        fts = c.execute("SELECT count(*) FROM jobs_fts").fetchone()[0]

    print(f"\n── column fill ({len(cols)} columns) ──────────────────")
    empty = []
    for col in cols:
        pct = 100 * fills[col] / stored if stored else 0
        print(f"  {col:24} {pct:5.1f}% {_bar(pct)}  {_fmt(fills[col])}")
        if not fills[col]:
            empty.append(col)

    print(f"\n  fts index  {_fmt(fts)} rows", end="")
    if fts != stored:
        print("   ⚠ DOES NOT MATCH jobs — the triggers are wrong")
    else:
        print()

    # A COMPLETELY EMPTY COLUMN FAILS THE REBUILD. This printed a warning and then
    # exited 0 saying "rebuild OK", which is how a slot rebuilt from a stale snapshot
    # sails through every automated gate with the release's headline features at 0%
    # fill: `verify-slot.sh` does not look at column fill either, so nothing between
    # the operator and production measures the thing that actually broke.
    #
    # The likely cause is always the same and is worth naming in the message: the
    # snapshot was written by an older engine, so re-harvest before rebuilding.
    if empty:
        print(f"\n  ⚠ {len(empty)} column(s) completely empty: {', '.join(empty)}")

    # Only the always-filled columns FAIL the rebuild. Everything else being empty is
    # information, not an error — a thin harvest genuinely has no Adzuna salary
    # estimates, and treating that as a failure would be a false alarm on a good build.
    stale = [c for c in _ALWAYS_FILLED if c in empty]
    if stale:
        print(
            f"\n  ✗ {', '.join(stale)} is empty — a 0.7.0 harvest fills it on EVERY row,"
            "\n    so jobs.json was written by an OLDER job-radar. Re-run the harvest"
            "\n    from THIS slot's binary, then rebuild. --allow-empty-columns overrides."
        )

    ok = fts == stored and stored > 0 and not (stale and not args.allow_empty_columns)
    print("\n  " + ("rebuild OK" if ok else "REBUILD FAILED ITS OWN CHECKS"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
