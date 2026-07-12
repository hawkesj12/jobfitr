"""The web API. Serves the cached snapshot; scores it against each user's config.

The one hard invariant: a request NEVER calls an external job API. It reads the
pre-harvested jobs.json and applies the user's lens with job_radar's pure scoring
functions. That's what decouples user count from job-API traffic (no IP bans, no
shared-key quota burn) — and it's asserted by test_zero_network_on_request.

Scoring functions are passed an explicit `cfg` per request; we never call
config.set_active() here, because the server is concurrent and that global is
shared state (a race). Only the single-threaded snapshot builder uses set_active.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import Body, FastAPI
from fastapi.staticfiles import StaticFiles
from job_radar.scoring import is_remote, relevant, score, top_signals
from job_radar.util import age_int

from .config_builder import config_from_dict
from .snapshot import DEFAULT_JOBS_PATH, load_snapshot

# Where the harvest cache lives. Overridable for tests / deployment.
JOBS_PATH = os.environ.get("JOBFITR_JOBS_PATH", DEFAULT_JOBS_PATH)

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
SNIPPET_CHARS = 240

app = FastAPI(title="jobfitr", version="0.1.0")


def _snippet(text) -> str:
    if not isinstance(text, str):
        return ""
    s = " ".join(text.split())
    return s[:SNIPPET_CHARS]


def _shape(job: dict, fit_score: int, why: str) -> dict:
    """The lean per-card payload the front end renders (no full JD body)."""
    return {
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "posted": job.get("posted", ""),
        "source": job.get("source", "") or (job.get("sources") or [None])[0],
        "salary": job.get("salary", ""),
        "fit_score": fit_score,
        "why": why,  # the top fit-signal keywords that matched
        "snippet": _snippet(job.get("text")),
    }


@app.post("/api/score")
def score_jobs(payload: dict = Body(...)) -> dict:
    """Rank the cached job universe against the posted 5-answer config."""
    cfg = config_from_dict(payload)
    limit = payload.get("limit")
    limit = (
        DEFAULT_LIMIT
        if not isinstance(limit, int) or limit <= 0
        else min(limit, MAX_LIMIT)
    )

    snap = load_snapshot(JOBS_PATH)
    results = []
    for job in snap.get("jobs", []):
        title = job.get("title", "")
        if not relevant(title, cfg):
            continue
        if not is_remote(job, cfg):
            continue
        age = age_int(job.get("posted", ""))
        if age is not None and age > cfg.max_age_days:
            continue
        s = score(job, cfg)
        if s < cfg.min_score:
            continue
        results.append(_shape(job, s, top_signals(job, cfg=cfg)))

    results.sort(key=lambda r: r["fit_score"], reverse=True)
    results = results[:limit]
    return {"count": len(results), "meta": snap.get("meta", {}), "jobs": results}


@app.get("/api/meta")
def meta() -> dict:
    """Harvest freshness for the UI (when the cache was built, how many jobs)."""
    return load_snapshot(JOBS_PATH).get("meta", {})


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


def _web_dir() -> Path:
    """The static front end lives in ./web at the repo root; overridable for deploy."""
    override = os.environ.get("JOBFITR_WEB_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "web"


# Serve the front end at / — mounted LAST so the /api/* routes above still win.
# Guarded so the API still boots headless (e.g. before the front end exists).
_WEB = _web_dir()
if _WEB.is_dir():
    app.mount("/", StaticFiles(directory=str(_WEB), html=True), name="web")


def main(argv=None) -> int:  # pragma: no cover — exercised via jobfitr-serve
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(
        prog="jobfitr-serve", description="Run the jobfitr API locally."
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args(argv)
    uvicorn.run(
        "jobfitr.server:app", host=args.host, port=args.port, reload=args.reload
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
