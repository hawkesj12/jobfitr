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


def config_from_dict(doc: dict) -> Config:
    """Build a per-user Config from the posted 5-answer JSON. Pure, no I/O."""
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
    cfg.exclude_titles = exclude
    # Likewise clear the tech-specific title penalty (research-scientist / member-of-
    # technical-staff) — meaningless for a general audience and unfair to those roles.
    cfg.title_penalty = {}

    # Location / remote.
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
    if isinstance(remote_only, bool):  # explicit flag always wins
        cfg.remote_only = remote_only

    # Freshness.
    max_age = doc.get("max_age_days")
    if isinstance(max_age, (int, float)) and not isinstance(max_age, bool):
        cfg.max_age_days = int(max_age)

    # Pickiness.
    cfg.min_score = _resolve_min_score(doc.get("min_score"))

    return cfg
