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

MODEL: minishlab/potion-retrieval-32M, a Model2Vec STATIC EMBEDDER. There is no neural
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
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DIMS = 512
MODEL = os.environ.get("JOBFITR_EMBED_MODEL", "minishlab/potion-retrieval-32M")
SUMMARY_CHARS = 1800   # ~512 tokens: the most bge/arctic/bge-base can read in one pass.
# NOT 300. Measured 2026-08-19: a posting's opening is not reliably the job — one real example
# spends its first 550 characters on an "Application and Interview Impersonation Notice" before
# naming the role. A 300-char summary embeds the legal notice.

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


_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</(li|p|div|h[1-6]|tr|ul|ol)\s*>", re.I)
_ENTITIES = (("&nbsp;", " "), ("&amp;", "&"), ("&#39;", "'"), ("&rsquo;", "'"),
             ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">"), ("&ndash;", "–"), ("&mdash;", "—"))


def plain(html: str) -> str:
    """Markup out, text in — measured over 400 live postings on 2026-08-19.

    55% of bodies carry raw HTML and markup is 12.9% of their characters (worst case 79%).
    Embedding that spends budget on `<span style="font-family: helvetica, arial, sans-serif">`
    and on Notion discussion GUIDs, and it does something worse than waste: every posting from
    the same ATS shares the same boilerplate markup, so it pulls unrelated jobs TOWARD each
    other in vector space.

    The tags are formatting, not data — 1,190 distinct <h*>/<strong> strings over 1,683
    occurrences, 82% of them appearing exactly once, top-30 covering 15%. There is no shared
    section vocabulary to parse into fields, so this is a cleanup, not a parser.

    </li> and friends become NEWLINES rather than spaces: 221 of 400 postings are bullet lists
    with a median of 22 bullets, and those bullets are the responsibilities and requirements.
    Collapsing them into one run of prose destroys the only sentence boundaries left.

    Markdown is not a factor: 6 of 400 bodies use `**bold**` and none use markdown headings,
    bullets, links, or code. The two `re.sub`s for it are belt-and-braces.

    THE REAL HOME FOR THIS IS job-radar's extraction, not here — this is jobfitr cleaning up
    after its dependency. Keep the function self-contained so it can move upstream unchanged.
    """
    if not html:
        return ""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = _BLOCK.sub("\n", t)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = _TAG.sub(" ", t)
    for a, b in _ENTITIES:
        t = t.replace(a, b)
    t = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", t)
    t = re.sub(r"(?m)^#{1,4}\s*", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


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


def _signal(row: dict) -> str:
    """Responsibilities + requirements when the posting exposes them, else the cleaned head.

    Measured 2026-08-19 over the whole pool: 51.8% of bodies carry a responsibilities
    section and 51.7% a requirements section, and for those the two together are only 40%
    of the cleaned body — the other 60% is benefits, compensation, company marketing, EEO
    and recruiting-fraud boilerplate, which is near-identical across thousands of postings
    and pulls unrelated jobs toward each other. Dropping it moved top-rated jobs reaching
    the top 25 from 5 to 8 on the graded set, at no extra embedding cost.

    Greenhouse-only in practice (100% of greenhouse bodies carry HTML, ~0% of every other
    adapter), so this is a no-op for 35% of the pool and it falls back rather than failing.
    Once job-radar ships the `sections` field this reads that instead of re-parsing.
    """
    from jobfitr.sections import KEEP, split_sections

    parts = split_sections(row.get("body") or "")
    keep = [t for kind, _h, t in parts if kind in KEEP and t]
    if not keep:
        return plain(row.get("body"))
    intro = next((t for kind, h, t in parts if kind is None and not h and t), "")
    return " ".join([intro[:600]] + keep).strip()


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
    return f"{head}\n{' · '.join(meta)}{tagline}\n{_signal(row)[:SUMMARY_CHARS]}"


def open_vectors(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    stored = con.execute("SELECT value FROM meta WHERE key='model'").fetchone()
    if stored and stored[0] != MODEL:
        raise SystemExit(
            f"vector store was built with {stored[0]!r}, now asked for {MODEL!r}.\n"
            "Documents and queries MUST share a model — a mixed store returns nonsense "
            "rather than an error. Delete the file and rebuild, or set JOBFITR_EMBED_MODEL back."
        )
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('model',?)", (MODEL,))
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


def iter_jobs(store_path: str):
    con = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    for r in con.execute("SELECT url,title,company,location,body FROM jobs"):
        yield dict(r)
    con.close()


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

    import numpy as np
    from model2vec import StaticModel

    model = StaticModel.from_pretrained(MODEL)
    t0 = time.perf_counter()
    done = 0
    for i in range(0, len(stale), 500):
        chunk = stale[i : i + 500]
        vecs = np.asarray(model.encode([summary_text(j) for j in chunk]), dtype=np.float32)
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
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
