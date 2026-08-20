#!/usr/bin/env python3
"""Embed the job pool into a vector store — incremental, content-hash keyed.

WHY A SEPARATE FILE FROM jobs-v3.db: the store is a cache that gets REBUILT, never
migrated (store._default_db_path). Embedding is the one thing here that cannot be
regenerated in a ten-minute harvest — measured 21.6 ms/job, so ~22 minutes for a
60,000-row pool. Keying vectors by a hash of (url|title|body) in their own file means a
schema bump on jobs-v3.db costs nothing: every unchanged row keeps its vector and only
genuinely new or edited postings are re-embedded.

WHAT GETS EMBEDDED, and why not the whole body: measured 2026-08-19 over a 5,000-row
live sample (_private/retrieval/FINDINGS-vector-granularity-2026-08-19.md). Chunking the
full body costs 13-15x more (402 vs 31 ms/job) and did not retrieve better. Embedding the
whole posting in one vector needs a long-context model that peaked at 6.99 GB RSS for 200
documents and was OOM-killed on 5,000 — it cannot run on a 2-vCPU / 8 GB box at all.
One summary vector per job is what survived: title, company, location, and the first 300
characters, which is where a posting describes the role before it turns into benefits
boilerplate.

THE EMBEDDER — and there is exactly ONE. `minishlab/potion-retrieval-32M`, a Model2Vec
static model. Every other model named below (bge, jina, arctic, MiniLM, fastembed) appears
in this file ONLY as the record of what it was measured against on 2026-08-19. None of them
runs. None is installed. If you are looking for a second embedding path, there isn't one.

(Do not confuse this with `chat.MODELS`, which is a bench of 13 LLM candidates for the
conversation with one active. Different thing entirely: that one is metered and needs a key,
this one is a lookup table that costs nothing.)

potion-retrieval-32M is a STATIC EMBEDDER. There is no neural
forward pass at all — the vectors for every vocabulary token were distilled once, offline,
from a real sentence-transformer; encoding is a table lookup plus a weighted mean. Measured
2026-08-19 on the 363-row graded set: 11 of 36 top-rated jobs in the top 25 against
bge-base-en-v1.5's 12 (indistinguishable — paired McNemar over that set returns p=1.00 for
every arm pair tested), BETTER on relevant@50 (38 vs 37), and **0.06s vs 74.2s** to build.
7,580 docs/s here, so the 65,170-row pool embeds in seconds rather than hours, which is what
removes the 2-vCPU box from the design entirely.

It also deletes a dependency: no onnxruntime (69 MB on its own), no torch, no transformers.

The obvious objection — a bag-of-token-vectors cannot see word order, negation, or
composition — was tested rather than assumed, and bge-base is NOT better at any of them.
Negation: both fail (0.75 vs 0.74 mean similarity on pairs that should be far apart; neither
separates "you will not be on call" from "you will be on call"). Word order: both correctly
treat "Senior AI Product Builder" and "AI Product Builder, Senior" as the same role.
Composition: Model2Vec is BETTER on all four pairs — it rates "Forward Deployed Engineer"
against "Deployment Engineer for forward logistics" at 0.75 where bge-base says 0.90. The
speed is not being paid for with the properties it looks like it should cost.

NO INSTRUCTION PREFIX. bge is asymmetric and wants one on the query; potion does not, and
applying bge's prefix to it costs 2 of 11 top-rated jobs (measured). jobfitr/semantic.py
holds the query side and its QUERY_PREFIX is empty for exactly this reason.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import logging
import sys

from jobfitr import store
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ONE OWNER for the model contract. These used to be declared here AND in semantic.py,
# which is a silent-failure waiting to happen: documents embedded by one model and queried
# by another return plausible nonsense, not an error. The stored model is still stamped
# into the vector file and checked at query time, but two constants that must agree should
# not be two constants.
from .semantic import DIMS, EMBED_MODEL  # noqa: E402
# HOW MUCH OF A POSTING BECOMES ITS VECTOR — and this number is now INHERITED, not justified.
#
# It was 1,800 because that is ~512 tokens, the most bge/arctic/bge-base could read in one
# pass. Model2Vec has no such limit: it averages token vectors, so length is unbounded
# (verified — cos between 2x and 60x repetitions of the same sentence is 0.9999). We are
# truncating for a constraint that no longer exists.
#
# What length is RIGHT is therefore an open question, and it is a different question from
# the old one. Not "what fits" but "how much text before the average blurs" — more body
# means the role gets averaged with benefits and boilerplate. Worth re-measuring against
# the graded bed before trusting 1,800.
#
# What IS settled: not 300. A posting's opening is not reliably the job — one real example
# spends its first 550 characters on an "Application and Interview Impersonation Notice"
# before naming the role, so a 300-char summary embeds the legal notice.
SUMMARY_CHARS = 1800

log = logging.getLogger("jobfitr.vectors")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS vectors (
    url        TEXT PRIMARY KEY,
    content_id TEXT NOT NULL,      -- sha1(url|title|body): the row's identity, not its rowid
    embedding  BLOB NOT NULL,      -- {DIMS} float32, L2-normalized on write
    built_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vectors_content ON vectors(content_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""




def content_id(row: dict) -> str:
    """Identity of the text we actually EMBED — not of the whole posting.

    Hashing the full body looked more careful and was wrong: an employer editing
    paragraph nine produces a byte-identical vector, so re-embedding it is pure waste on
    a 2-vCPU box doing this nightly. Hash exactly what goes into the model and a row
    re-embeds only when its vector would actually change. It also means the nightly
    export needs ~400 characters per row instead of the full 5 KB body.
    """
    h = hashlib.sha1()
    h.update((row.get("url") or "").encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update(summary_text(row).encode("utf-8", "replace"))
    return h.hexdigest()


def _listy(v) -> list:
    if not v:
        return []
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v)
        return out if isinstance(out, list) else []
    except Exception:
        return []



def summary_text(row: dict) -> str:
    """The embedded surface: a SEMANTIC header, then the cleaned body head.

    The header carries only fields that mean something to a probe — title, its normalized
    root, its qualifiers, company, team, category. It deliberately EXCLUDES remote, salary,
    state, employment type and posted date: those are filters, and a filter excludes
    perfectly while a word in a vector only nudges similarity. Putting "onsite" in the text
    makes an onsite job slightly less similar to a remote query; a WHERE clause removes it.

    Documents take NO instruction prefix — bge-v1.5 is asymmetric and the prefix belongs on
    the query side only (jobfitr/semantic.py).
    """
    title = (row.get("title") or "").strip()
    bits = [title] if title else []
    root = (row.get("title_root") or "").strip()
    if root and root.lower() != title.lower():
        bits.append(root)
    quals = _listy(row.get("title_qualifiers"))
    if quals:
        bits.append(", ".join(str(q) for q in quals[:4]))
    head = " · ".join(bits)
    meta = [f"Company: {row.get('company') or ''}"]
    if row.get("team"):
        meta.append(f"Team: {row['team']}")
    if row.get("category"):
        meta.append(f"Field: {row['category']}")
    if row.get("location"):
        meta.append(f"Location: {row['location']}")
    tags = _listy(row.get("tags"))
    tagline = f"\nTags: {', '.join(str(t) for t in tags[:8])}" if tags else ""
    return f"{head}\n{' · '.join(meta)}{tagline}\n{store.plain(row.get('body'))[:SUMMARY_CHARS]}"


def open_vectors(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    stored = con.execute("SELECT value FROM meta WHERE key='model'").fetchone()
    if stored and stored[0] != EMBED_MODEL:
        raise SystemExit(
            f"vector store was built with {stored[0]!r}, now asked for {EMBED_MODEL!r}.\n"
            "Documents and queries MUST share a model — a mixed store returns nonsense "
            "rather than an error. Delete the file and rebuild, or set JOBFITR_EMBED_MODEL back."
        )
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('model',?)", (EMBED_MODEL,))
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('dims',?)", (str(DIMS),))
    con.commit()
    return con


def iter_jsonl(path: str):
    """Read summaries exported from another machine's store.

    The backfill is ~40 minutes of CPU and does not belong on a 2-vCPU box that is also
    serving traffic, so it runs off-box against an export and the resulting vectors-v1.db
    ships up. Because content_id hashes only the embedded text, the export carries 300
    characters of body instead of the full 5 KB — 6.3 MB for the whole 65,170-row pool.
    """
    import gzip
    import json

    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


# EVERY column summary_text() actually reads. It used to select five, while summary_text
# built its "semantic header" from title_root, title_qualifiers, team, category and tags —
# so the header the docstring calls load-bearing was empty in every vector ever built, and
# nothing failed, because a missing key just yields an empty string. Fill rates when this
# was found (2026-08-20): title_root 7.7%, category 7.4%, title_qualifiers 2.5%, team 0.1%,
# tags 0%. Small today, and job-radar 0.9.0 raises them — a bug that grows on its own.
_JOB_COLS = ("url,title,title_root,title_qualifiers,company,team,category,"
             "location,tags,body")


def iter_jobs(store_path: str):
    con = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    for r in con.execute(f"SELECT {_JOB_COLS} FROM jobs"):
        yield dict(r)
    con.close()


def _encode(rows: list[dict]):
    """rows -> L2-normalized float32 vectors. The only place the model is invoked.

    IT REUSES semantic's MODEL, and that is not a micro-optimisation. `StaticModel.
    from_pretrained` can reach the network: measured 2026-08-20, an intake embed of FIVE
    rows took 217 SECONDS on a cold HuggingFace fetch, against 535 ms cold-from-cache and
    1 ms warm. Building a second model here would put that stall inside a request handler.
    The web process already holds exactly this model for queries — semantic.EMBED_MODEL is
    the same constant — so there is one instance, one load, and one thing that can hang,
    and it hangs at query time where it already did rather than in a new place.

    The CLI path takes the same route; on a box with no HF cache the first call still
    fetches, which is why the nightly unit is oneshot with a 20-min timeout instead of
    something that has to answer a user.
    """
    import numpy as np

    from .semantic import _model

    v = np.asarray(_model().encode([summary_text(r) for r in rows]), dtype=np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    return v


def vector_count(vectors_path: str | None = None) -> int:
    """How many vectors exist. For /api/health — `semantic.available()` only proves the
    FILE is there, which cannot tell a current store from one built in June."""
    from .semantic import VECTORS_PATH

    try:
        con = sqlite3.connect(f"file:{vectors_path or VECTORS_PATH}?mode=ro", uri=True)
        n = con.execute("SELECT count(*) FROM vectors").fetchone()[0]
        con.close()
        return int(n)
    except sqlite3.Error:
        return 0


def embed_urls(urls: list[str], vectors_path: str | None = None) -> int:
    """Embed these urls NOW, reading their rows back from the store. Returns how many.

    THE POINT OF THIS FUNCTION: a nightly pass cannot keep up, because the live lane adds
    rows all day and those are precisely the local ones. Measured 2026-08-20 before this
    existed — of 1,998 unvectorised rows, 1,929 were adzuna, and 97% of every Louisville/KY
    row in the pool had no vector at all. The semantic arm was dark exactly where the
    product claims to work, while /api/health read healthy.

    It reads back from the STORE rather than embedding the fetched records directly, so the
    vector is built from the same normalized row retrieval will return.

    Never raises. Embedding is an enhancement to search and may not be able to break it —
    the same contract semantic.candidates() keeps.
    """
    from . import store
    from .semantic import VECTORS_PATH

    urls = [u for u in (urls or []) if u]
    if not urls:
        return 0
    try:
        rows = [r for r in store.rows_by_url(urls) if r.get("url")]
        if not rows:
            return 0
        con = open_vectors(vectors_path or VECTORS_PATH)
        # Two slots share one vectors file on the box, same as the store — WAL plus a
        # busy timeout is what makes concurrent small writes safe rather than an
        # intermittent "database is locked" in a request handler.
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=8000")
        known = dict(con.execute(
            "SELECT url, content_id FROM vectors WHERE url IN (%s)"
            % ",".join("?" * len(rows)), [r["url"] for r in rows]))
        stale = [r for r in rows if known.get(r["url"]) != content_id(r)]
        if not stale:
            con.close()
            return 0
        vecs = _encode(stale)
        now = time.time()
        con.executemany(
            "INSERT OR REPLACE INTO vectors(url,content_id,embedding,built_at) "
            "VALUES(?,?,?,?)",
            [(r["url"], content_id(r), v.tobytes(), now) for r, v in zip(stale, vecs)])
        con.commit()
        con.close()
        return len(stale)
    except Exception as e:  # noqa: BLE001 — see the docstring: never break search
        log.warning("embed_urls failed (%s: %s) — rows stay unvectorised", type(e).__name__, e)
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--store", default=os.environ.get("JOBFITR_DB", "jobs.db"),
                    help="the jobs store to read (default: jobs.db / $JOBFITR_DB)")
    ap.add_argument("--from-jsonl", default=None,
                    help="read summaries from a (gzipped) jsonl export instead of --store")
    ap.add_argument("--vectors", default=os.environ.get("JOBFITR_VECTORS", "vectors-v1.db"))
    ap.add_argument("--limit", type=int, default=None, help="stop after N NEW embeddings (a pilot)")
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--dry-run", action="store_true", help="report what WOULD be embedded, embed nothing")
    a = ap.parse_args(argv)

    vec = open_vectors(a.vectors)
    known = {u: c for u, c in vec.execute("SELECT url, content_id FROM vectors")}
    jobs = list(iter_jsonl(a.from_jsonl) if a.from_jsonl else iter_jobs(a.store))
    stale = [j for j in jobs if known.get(j["url"]) != content_id(j)]
    gone = set(known) - {j["url"] for j in jobs}
    print(f"pool {len(jobs)}  vectors {len(known)}  to embed {len(stale)}  orphaned {len(gone)}")
    if a.dry_run:
        return 0
    if gone:
        # The store evicts on its own schedule; a vector for a row nobody can retrieve is
        # dead weight in a brute-force KNN, so it goes with it.
        vec.executemany("DELETE FROM vectors WHERE url=?", [(u,) for u in gone])
        vec.commit()
    if not stale:
        print("up to date")
        return 0
    if a.limit:
        stale = stale[: a.limit]
        print(f"--limit: embedding {len(stale)}")


    t0 = time.perf_counter()
    done = 0
    for i in range(0, len(stale), 500):
        chunk = stale[i : i + 500]
        vecs = _encode(chunk)
        now = time.time()
        vec.executemany(
            "INSERT OR REPLACE INTO vectors(url,content_id,embedding,built_at) VALUES(?,?,?,?)",
            [(j["url"], content_id(j), v.tobytes(), now) for j, v in zip(chunk, vecs)],
        )
        vec.commit()
        done += len(chunk)
        rate = done / (time.perf_counter() - t0)
        eta = (len(stale) - done) / rate if rate else 0
        print(f"  {done}/{len(stale)}  {rate:.1f}/s  eta {eta/60:.1f} min", flush=True)
    vec.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('built_at',?)", (str(time.time()),))
    vec.commit()
    total = vec.execute("SELECT count(*) FROM vectors").fetchone()[0]
    print(f"done: {done} embedded in {(time.perf_counter()-t0)/60:.1f} min, {total} vectors total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
