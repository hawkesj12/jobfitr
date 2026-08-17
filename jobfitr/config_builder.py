"""Turn the front end's 5-answer JSON into a job_radar Config — the per-user
"narrow lens" applied to the cached snapshot at request time.

job_radar has no `from_dict`, and its Config defaults are tuned for a generic
software search. jobfitr is a general-audience tool (a zookeeper is as valid a
user as an ML engineer), so we do NOT merge over those tech defaults — we
*replace* the profile/scoring fields with the user's own titles and boosts.
Everything the user doesn't specify falls back to a plain Config() default.

The posted JSON contract (every key optional):

    {
      "titles":       ["zookeeper", "animal keeper"],   # Q1: the roles wanted
      "boosts":       ["reptiles", "biology degree"],   # Q2: rank-higher signals
      "exclude":      ["intern", "volunteer"],          # Q3a: never-show titles
      "rank_down":    ["staffing", "agency"],           # Q3b: sink-these signals
      "location":     "Louisville, KY",                 # Q4: place / "remote" / "anywhere"
      "remote_only":  false,                            # Q4 (optional; inferred otherwise)
      "max_age_days": 60,                               # Q4
      "min_score":    "balanced"                        # Q5: int or plenty|balanced|strong
    }
"""

from __future__ import annotations

from .match import has_term, norm_key
from job_radar.config import Config

# How heavily each kind of user signal weighs in the fit score. A title match is
# also double-counted inside job_radar.scoring.score() (title + body), so titles
# land a bit lighter than an explicit strength here.
_TITLE_WEIGHT = 3
_BOOST_WEIGHT = 5
_RANK_DOWN_PENALTY = 8

# "How picky" → a min_score cutoff on the same scale as the weights above.
# Tunable; calibrated so a couple of real matches clear "balanced".
_PICKINESS = {"plenty": 5, "balanced": 12, "strong": 20}
_DEFAULT_PICKINESS = _PICKINESS["balanced"]


def _clean_list(value) -> list[str]:
    """Coerce a value into a de-duped list of non-empty, lowercased tokens.

    Accepts a list, or a comma/newline-separated string (voice-to-text friendly).
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        parts = value
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        s = str(p).strip().lower()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def search_inputs(doc: dict) -> tuple[list[str], str]:
    """The SEARCH half of the config — what to send to the live APIs.

    titles + location define what jobs to fetch; the RANKING half (boosts, exclude,
    rank_down, min_score) is applied later at scoring. Splitting them lets the fetch
    start from just the first answers.
    """
    doc = doc or {}
    titles = _clean_list(doc.get("titles"))
    loc = doc.get("location")
    return titles, (loc.strip() if isinstance(loc, str) else "")


def _resolve_min_score(value, default: int = _DEFAULT_PICKINESS) -> int:
    """A pickiness keyword or an explicit integer → a min_score int."""
    if value is None:
        return default
    if isinstance(value, bool):  # guard: bool is an int subclass
        return default
    if isinstance(value, (int, float)):
        return int(value)
    key = str(value).strip().lower()
    return _PICKINESS.get(key, default)


def config_from_dict(doc: dict, notes: list[str] | None = None) -> Config:
    """Build a per-user Config from the posted 5-answer JSON. Pure, no I/O.

    Pass a list as `notes` to receive human-readable descriptions of anything this had to
    OVERRIDE in the posted answers. The purity matters — this is the one contract every
    entry path shares — so nothing is printed or logged here; the caller decides.
    """
    doc = doc or {}
    cfg = Config()

    titles = _clean_list(doc.get("titles"))
    boosts = _clean_list(doc.get("boosts"))
    exclude = _clean_list(doc.get("exclude"))
    rank_down = _clean_list(doc.get("rank_down"))

    # Profile: what the user is actually looking for drives the search queries.
    if titles:
        cfg.title_queries = titles

    # Relevance gate: a posting is relevant only if its title hits one of these.
    # Seed from titles + boosts so we don't over-filter; if the user gave neither,
    # keep the generic default rather than an empty list (which drops everything).
    signal = list(dict.fromkeys(titles + boosts))
    if signal:
        cfg.title_signal = signal

    # Fit weights: replace the generic-tech defaults with the user's own signals.
    if titles or boosts:
        weights: dict[str, int] = {}
        for kw in titles:
            weights[kw] = _TITLE_WEIGHT
        for kw in boosts:  # a boost that's also a title takes the higher weight
            weights[kw] = _BOOST_WEIGHT
        cfg.fit_weights = weights

    # Rank-down signals (staffing/agency terms) subtract from the score.
    if rank_down:
        cfg.agency_penalty = {kw: _RANK_DOWN_PENALTY for kw in rank_down}

    # Hard exclusions: ONLY the user's own (empty if they named none). We must NOT
    # inherit job_radar's tech-recruiting default exclude list — it contains
    # "sales", "marketing", "customer success", "accountant", "recruiter", etc.,
    # which would silently hide those non-tech roles from jobfitr's general audience.
    #
    # AN EXCLUSION MAY NOT DELETE THE USER'S OWN TARGET ROLE.
    #
    # Observed live, 2026-08-17: an interview produced `titles` containing "ai engineer"
    # AND `exclude` containing "ai engineer". Exclusions are a HARD FILTER in
    # `server._eligible` — not a rank penalty — so every genuine AI Engineer posting was
    # removed before scoring, and the board came back entirely "DevOps Engineer" ranking on
    # `docker x3`, for a search whose words were "AI operations and automation engineering".
    # Nothing on the board said why. A rank bug leaves the right jobs present in the wrong
    # order; this deleted them.
    #
    # The assistant writing both lists is the proximate cause and it will misfire again —
    # so the guard lives HERE, at the contract, where every path (chat, the form fallback,
    # a hand-posted body, a saved config replayed tomorrow) passes through.
    #
    # THE TEST USES `match.has_term`, the same function `_eligible` filters with, rather
    # than a string comparison. That is the point: a guard that decided membership
    # differently from the filter it protects against could disagree with it, and then
    # "engineer" would still silently delete "AI Engineer" — whole-word matching means a
    # single shared word is enough to cancel the whole target role.
    cancelled: list[str] = []
    kept: list[str] = []
    for term in exclude:
        # `norm_key(title)`, not the raw title — and this was a real hole, found in review.
        # `norm_key` collapses punctuation; `has_term`'s pattern joins words with `[\s-]+`,
        # which accepts a space or a hyphen but NOT a slash or a comma. So for
        # `titles=["AI/ML Engineer"]` and `exclude=["ai ml engineer"]` the guard saw no match
        # and kept the term, while a job titled "AI ML Engineer" scored a **tier-100 exact
        # match** (tier 100 compares on `norm_key`, which erases the slash) and was then
        # deleted by `_eligible`. Verified: score (100, False), `has_term` against the raw
        # title False, against the normalised title True.
        # Normalising here makes the guard read the title the way the SCORER does, which is
        # the surface that decides whether a job is the user's role.
        if any(has_term(term, norm_key(title)) for title in titles):
            cancelled.append(term)
        else:
            kept.append(term)
    cfg.exclude_titles = kept
    if cancelled and notes is not None:
        notes.append(
            "dropped exclusion(s) that would have deleted your own target role: "
            + ", ".join(cancelled)
        )
    # Likewise clear the tech-specific title penalty (research-scientist / member-of-
    # technical-staff) — meaningless for a general audience and unfair to those roles.
    cfg.title_penalty = {}

    # Location / remote. Default to SHOW ALL (not job_radar's remote_only=True) — a
    # general-audience user who names no location wants every job, not remote-only.
    cfg.remote_only = False
    location = doc.get("location")
    remote_only = doc.get("remote_only")
    if isinstance(location, str) and location.strip():
        loc = location.strip()
        low = loc.lower()
        if low in ("remote", "remote only", "remote-only"):
            cfg.location = "remote"
            cfg.remote_only = True
        elif low in ("anywhere", "any", "everywhere"):
            cfg.location = "remote"
            cfg.remote_only = False
        else:  # a real place
            cfg.location = loc
            cfg.remote_only = False
    # KNOWN DEFECT, DELIBERATELY LEFT OPEN — do not "fix" this the obvious way.
    #
    # `remote_only` is a HARD FILTER in `server._eligible`, so a real place plus
    # `remote_only=true` shows only remote jobs and, for local non-tech verticals, an empty
    # board: measured `[local dev db]` Warehouse Supervisor / Louisville 0, Industrial
    # Electrician / Louisville 0, Occupational Therapist / Austin 0, HS Teacher / Denver 0.
    # (It does NOT "empty the board" generally — `location` filters nothing, so a tech search
    # just fills with nationwide remote jobs: Software Engineer / Louisville stays at 200.)
    #
    # A guard here that made the PLACE win was written, reviewed and REVERTED on 2026-08-17,
    # for three reasons that any future attempt has to answer:
    #
    #   1. THE SHAPE HAS NEVER OCCURRED. 0 of 32 real searches carried a real place with
    #      `remote_only=true`. All 24 remote searches said "remote" or "anywhere"; all 8 place
    #      searches carried no flag. The guard would have protected against a hypothetical.
    #   2. ITS JUSTIFICATION WAS FALSE. The commit claimed the extra onsite jobs were
    #      "removable with one chip". They are not: `web/app.js`'s `passesFacets` exempts
    #      unlabelled rows ON PURPOSE (57-79% of such a board is NULL-`remote`), `buildFacets`
    #      suppresses a one-value group so the control often does not render at all, and
    #      `RESULT_CAP` truncation is server-side — remote listings displaced out of the top
    #      200 never reach the browser to be filtered back in. Measured over 19 remote-only
    #      profiles given a home city: 186 rows all-confirmed-remote became 200 rows with 62
    #      confirmed remote.
    #   3. `city + remote_only=true` IS COHERENT. "I live in Louisville, show me remote work"
    #      is a real request, and `chat.py`'s location chips lead with Remote/Hybrid/On-site,
    #      so a user produces this pair on purpose. Unlike `exclude` vs `titles` — which is
    #      NEVER coherent, which is why that guard is right — the payload cannot tell which
    #      reading is meant, so the contract would be guessing at intent it does not have.
    #
    # The fix that would dominate: keep both fields and stop making the flag a FILTER when a
    # place is also named — pass the remote preference into `_rank` as a strong boost so
    # confirmed-remote rows sort to the top of the 200 instead of displacing them. That needs
    # its own before/after over those 19 profiles, and it is a `_rank` change, not this one.
    # Also required first, whatever the direction: `searchlog.record` takes the POST-override
    # `remote_only` and has no `overrides` field, so today the only real-user signal cannot
    # observe this firing at all.
    if isinstance(remote_only, bool):  # explicit flag always wins
        cfg.remote_only = remote_only

    # Freshness.
    max_age = doc.get("max_age_days")
    if isinstance(max_age, (int, float)) and not isinstance(max_age, bool):
        cfg.max_age_days = int(max_age)

    # Pickiness.
    cfg.min_score = _resolve_min_score(doc.get("min_score"))

    return cfg
