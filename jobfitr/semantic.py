"""The semantic retrieval arm — probes in, ranked urls out.

Second way into the pool, beside `store.bm25_candidates`. That one queries the TITLE
column only (`store._fts_query` builds `title: NEAR(...)`), so a posting whose title does
not carry the user's words is unreachable no matter how well its text describes them.
`Forward Deployment Engineer` is invisible to a search for `Forward Deployed Engineer` —
verified, but NOT for the reason first written here. The index is `tokenize='porter
unicode61'` (store.py:751), so stemming IS on. Porter resolves inflection (engineer/
engineering, nurse/nursing, automate/automated) and refuses derivational nominalisation:
{deployed, deploy, deploying} and {deployment, deployments} are two separate stem clusters
because -ment survives on a short stem. Measured 2026-08-19 on the graded set, switching
the harness to porter moved rated-3@25 from 8 to 7 — slightly NEGATIVE. So the gap is a
narrow, enumerable class of word-formation that the lexical arm cannot cross, and no
tokenizer setting closes it.

WHY IT IS A SEPARATE ARM AND NOT A RERANK: a rerank can only reorder rows the title query
already returned, so it adds zero recall by construction. Measured 2026-08-19 over 5,000
RANDOM live rows, judged blind by a cross-family model against a real profile: the title
arm found 13 good jobs in its top 25, this arm found 9, and **they overlapped on one**.
Union 21 — a 62% lift over the title arm alone. The two arms do not compete; they barely
intersect.

HOW THE ARMS COMBINE — RRF, and an earlier claim here was WRONG. This docstring used to
say the caller should UNION rather than fuse, citing "union 21 vs RRF 11". That comparison
compared a 50-row union against a 25-row RRF list: it confounded the fusion rule with the
candidate count, which is the one effect already known to dominate. Re-measured at MATCHED
budget on two unrelated dense arms, RRF is never dominated at N>=50:

    N=            25    50   100   200
    lexical only   8    10    22    35
    dense only     9    14    21    28
    RRF(both)     10    14    23    33
    union         12    14    18    32

So RRF k=60 fuses the arms, the same rule this module already uses to collapse the eight
probes. Do not reinstate the union.

The honest caveat on ALL of the above: `gold` is one scalar per row, so every one of these
numbers is a SINGLE query. Paired McNemar over the 36 top-rated items returns p=1.00 for
every arm pair tested — the arms are not separated by this evidence. What IS significant
(p=0.031, 6-0 discordant) is the two-arm hybrid beating title-only lexical 16-10 at K=50.
Two arms is supported; the choice between them is not.

The store is a cache rebuilt nightly; vectors live in their own file keyed by a content
hash (scripts/build_vectors.py) precisely so a schema bump does not cost 40 minutes of
re-embedding.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("jobfitr.semantic")

# bge-v1.5 is ASYMMETRIC: the query carries an instruction, documents carry none.
# build_vectors.py embeds documents raw; this is the other half of that contract and the
# two must never drift. Omitting the prefix is the most common embedding mistake there is
# — nothing errors, results just quietly get worse.
# EMPTY ON PURPOSE. bge-v1.5 is asymmetric and wants an instruction on the query side;
# potion-retrieval-32M does not, and applying bge's prefix to it cost 2 of 11 top-rated
# jobs on the graded set (9 vs 11) — a bigger effect than most differences between models.
# The wrong prefix is as damaging as no prefix; it is a per-model fact, not a default.
QUERY_PREFIX = ""
MODEL = os.environ.get("JOBFITR_EMBED_MODEL", "minishlab/potion-retrieval-32M")
DIMS = 512
VECTORS_PATH = os.environ.get("JOBFITR_VECTORS", "vectors-v1.db")

RRF_K = 60
PROBE_DEPTH = 200  # per-probe KNN depth before fusion
# One employer's postings share an intro paragraph, so they embed near each other and
# arrive as a block: in the 2026-08-19 run a single company took 5 of 25 rows, four of
# them onsite roles the user had already excluded. A candidate set is a budget, and one
# employer may not spend a fifth of it.
MAX_PER_COMPANY = int(os.environ.get("JOBFITR_SEMANTIC_MAX_PER_COMPANY", "3"))

_MODEL = None
_MATRIX = None  # (urls, np.ndarray) cached per process
_LOAD_FAILED = False


def available() -> bool:
    """Is the arm usable at all? Cheap — never imports the heavy stack."""
    if _LOAD_FAILED:
        return False
    import importlib.util

    if importlib.util.find_spec("model2vec") is None or importlib.util.find_spec("numpy") is None:
        return False
    return Path(VECTORS_PATH).exists()


def _model():
    global _MODEL, _LOAD_FAILED
    if _MODEL is None:
        from model2vec import StaticModel

        stored = _stored_model()
        if stored and stored != MODEL:
            # A store embedded with one model and queried with another returns plausible
            # nonsense rather than an error, which is the worst failure shape available.
            _LOAD_FAILED = True
            raise RuntimeError(f"vector store built with {stored!r}, query model is {MODEL!r}")
        _MODEL = StaticModel.from_pretrained(MODEL)
    return _MODEL


def _stored_model() -> str | None:
    try:
        con = sqlite3.connect(f"file:{VECTORS_PATH}?mode=ro", uri=True)
        row = con.execute("SELECT value FROM meta WHERE key='model'").fetchone()
        con.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _matrix():
    """All vectors as one L2-normalized matrix. Measured: brute-force numpy KNN over
    60,000 x 384 with 8 probes is 6.6 ms, so sqlite-vec buys nothing at this scale and
    costs a loadable-extension dependency the box may not have."""
    global _MATRIX
    if _MATRIX is None:
        import numpy as np

        con = sqlite3.connect(f"file:{VECTORS_PATH}?mode=ro", uri=True)
        rows = con.execute("SELECT url, embedding FROM vectors").fetchall()
        con.close()
        urls = [r[0] for r in rows]
        mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), DIMS)
        _MATRIX = (urls, mat)
        log.info("semantic: loaded %d vectors (%.0f MB)", len(urls), mat.nbytes / 1e6)
    return _MATRIX


def reset_cache() -> None:
    """Drop the in-process matrix so a rebuilt vector file is picked up."""
    global _MATRIX
    _MATRIX = None


def candidates(probes: list[str], k: int = 50) -> list[str]:
    """Rank the pool against several probes; return up to `k` urls, best first.

    Returns [] on ANY failure — a missing store, a model that will not load, a bad
    matrix. The lexical arm is the floor and this one may never be able to take search
    down with it.
    """
    probes = [p.strip() for p in (probes or []) if p and p.strip()]
    if not probes or not available():
        return []
    try:
        import numpy as np

        t0 = time.perf_counter()
        model = _model()
        urls, mat = _matrix()
        q = np.asarray(model.encode([QUERY_PREFIX + p for p in probes]), dtype=np.float32)
        q /= np.linalg.norm(q, axis=1, keepdims=True)

        # RRF across probes — the right tool HERE (many rankings, one corpus, no score
        # calibration between them). Not across arms; see the module docstring.
        scores: dict[int, float] = {}
        sims = mat @ q.T
        depth = min(PROBE_DEPTH, len(urls))
        for pi in range(sims.shape[1]):
            top = np.argpartition(-sims[:, pi], depth - 1)[:depth]
            for rank, j in enumerate(top[np.argsort(-sims[top, pi])]):
                scores[int(j)] = scores.get(int(j), 0.0) + 1.0 / (RRF_K + rank)
        ordered = sorted(scores, key=lambda i: (-scores[i], i))
        log.info("semantic: %d probes over %d vectors in %.1f ms",
                 len(probes), len(urls), (time.perf_counter() - t0) * 1000)
        return [urls[i] for i in ordered[:k]]
    except Exception as e:  # noqa: BLE001 — the arm degrades, it never raises upward
        log.warning("semantic: degraded to lexical-only (%s: %s)", type(e).__name__, e)
        return []


def cap_per_company(rows: list[dict], limit: int = MAX_PER_COMPANY) -> list[dict]:
    """Keep at most `limit` rows per company, preserving order. See MAX_PER_COMPANY."""
    seen: dict[str, int] = {}
    out = []
    for r in rows:
        key = (r.get("company") or "").strip().lower()
        if seen.get(key, 0) >= limit:
            continue
        seen[key] = seen.get(key, 0) + 1
        out.append(r)
    return out


def hybrid(lexical_urls: list[str], probes: list[str], k: int = 50,
           per_company: dict[str, str] | None = None) -> list[str]:
    """The two arms, fused by RRF, capped, truncated to k. THE entry point for the AI path.

    ORDER: the caller pre-filters BEFORE either arm runs. Filtering afterwards eats the
    budget — a dense KNN returns a fixed top-k, so dropping stated-onsite rows from the
    result hands the model 18 candidates where it asked for 50. Measured over the graded
    set, pre-filtering on stated dealbreakers keeps 91% of rows and 97% of relevant ones.

    RRF, NOT UNION, and an earlier version of this module said the opposite. That claim
    came from comparing a 50-row union against a 25-row RRF list — the fusion rule
    confounded with the candidate count, which is the effect already known to dominate.
    Re-measured at MATCHED budget on two unrelated dense arms, RRF is never dominated at
    N>=50. Weighted RRF was also built and tested: it works on synthetic probes and dies
    on real ones, because a bad probe ("healthcare software") matches its wrong answers
    with HIGH confidence, so a confidence weight measures certainty, not relevance.

    Returns lexical order unchanged when the dense arm is unavailable — the lexical arm is
    the floor and this one may never take search down with it.
    """
    dense_urls = candidates(probes, k=max(k * 4, 200))
    if not dense_urls:
        out = lexical_urls[:]
    else:
        scores: dict[str, float] = {}
        for ranked in (lexical_urls, dense_urls):
            for rank, url in enumerate(ranked):
                scores[url] = scores.get(url, 0.0) + 1.0 / (RRF_K + rank)
        out = sorted(scores, key=lambda u: (-scores[u], u))
    if per_company:
        out = [r["url"] for r in cap_per_company(
            [{"url": u, "company": per_company.get(u, "")} for u in out])]
    return out[:k]
