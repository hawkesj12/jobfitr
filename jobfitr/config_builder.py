"""What the user asked for → the FILTER inputs. No `Config`, no scoring.

This module used to build a whole `job_radar.Config`: boosts, rank-down penalties, fit
weights and a pickiness cutoff, all feeding a scoreboard. The scoreboard went on
2026-08-20 — a model reads the postings and judges them now — and the Config went with it,
because nothing downstream consumed one. Three call sites were reading exactly two fields
off a 211-line construction.

What survives is what still decides which rows a person may RECEIVE, and one guard that
has already prevented a live failure. Plain values in, plain values out, no I/O.
"""

from __future__ import annotations

from .match import has_term, norm_key

__all__ = ["_clean_list", "search_inputs", "filters_from_dict", "Filters"]


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



class Filters:
    """The hard constraints, as data. Everything here can DELETE a row from someone's
    results, which is why they live together and why the guard below is at this boundary
    rather than in one caller."""

    __slots__ = ("titles", "location", "remote_only", "exclude_titles", "max_age_days")

    def __init__(self, titles, location, remote_only, exclude_titles, max_age_days):
        self.titles = titles
        self.location = location
        self.remote_only = remote_only
        self.exclude_titles = exclude_titles
        self.max_age_days = max_age_days


def filters_from_dict(doc: dict, notes: list[str] | None = None) -> Filters:
    """Posted answers → the hard filters. Pure.

    Pass a list as `notes` to receive human-readable descriptions of anything this had to
    OVERRIDE. The purity matters — this is the one contract every entry path shares — so
    nothing is printed or logged here; the caller decides.
    """
    doc = doc or {}
    titles = _clean_list(doc.get("titles"))
    exclude = _clean_list(doc.get("exclude"))

    # AN EXCLUSION MAY NOT DELETE THE USER'S OWN TARGET ROLE.
    #
    # Observed live, 2026-08-17: an interview produced `titles` containing "ai engineer"
    # AND `exclude` containing "ai engineer". Exclusions are a HARD FILTER in
    # `server._eligible` — not a rank penalty — so every genuine AI Engineer posting was
    # removed before anything saw it, and the results came back entirely "DevOps Engineer".
    # Nothing said why. A ranking bug leaves the right jobs present in the wrong order;
    # this deleted them.
    #
    # The model writing both lists is the proximate cause and it will misfire again — so
    # the guard lives HERE, at the contract, where every path passes through.
    #
    # It uses `match.has_term`, the same function `_eligible` filters with, rather than a
    # substring test: the check must agree with the thing it is protecting against.
    kept = []
    for term in exclude:
        if any(has_term(term, t) or norm_key(term) == norm_key(t) for t in titles):
            if notes is not None:
                notes.append(
                    f"Ignored the exclusion {term!r} — it would have removed the role you asked for."
                )
            continue
        kept.append(term)

    loc = (doc.get("location") or "").strip()
    remote_only = doc.get("remote_only")
    low = loc.lower()
    if low in ("remote", "anywhere", "any", ""):
        location, inferred_remote = "", low == "remote"
    else:
        location, inferred_remote = loc, False
    if isinstance(remote_only, bool):
        inferred_remote = remote_only

    raw_age = doc.get("max_age_days")
    max_age = (
        int(raw_age)
        if isinstance(raw_age, (int, float)) and not isinstance(raw_age, bool)
        else None
    )
    return Filters(titles, location, inferred_remote, kept, max_age)
