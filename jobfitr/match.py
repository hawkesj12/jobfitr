"""Term matching and title tiering — the foundation the whole scoreboard stands on.

Pure stdlib, no project imports, so it is unit-testable without a server and so the
scoring rewrite that consumes it stays a separate, reviewable change.

WHY THIS MODULE EXISTS. The matcher it replaces was a plain substring test
(`if term in title`). Measured against the 39,597-row corpus, a user boosting "rag" —
retrieval-augmented generation, the thing Atlas actually is — matched 6,958 listings of
which 171 were real. 97.3% false, almost all of them the word "leverage", plus "storage"
and "coverage". Every AI job description says "leverage". The boost was doing nothing but
adding noise, and every number computed downstream of it inherited that noise.

WHY WHOLE-WORD + PLURAL, AND NOT PORTER STEMMING. Four strategies were measured on the
same corpus. Substring is catastrophic. Whole-word alone is too strict — it rejects
"stakeholders" and loses 83% of genuine `stakeholder` matches. Porter stemming and
whole-word+plural tie on everything that matters:

    term          substring   whole-word   word+plural   Porter
    rag               6,958          171           171      171
    stakeholder       3,269          559         3,259    3,264
    warehouse           719          539           719      724
    startup           2,378        1,083         2,377    2,377
    icu               1,443           15            15       15
    excel             4,624          498           646    4,596   <-- the tiebreaker
    agent             4,324        1,167         3,263    4,292

`excel` decides it: Porter stems "excellent" to "excel", so a user boosting the
spreadsheet would match 4,596 rows instead of 646 — reintroducing the exact 89%-false
failure being removed here. (`excel` is a live boost in the test corpus.) The only thing
Porter wins is `agent`, where it also catches "agentic"; that miss is accepted, and a
user who wants agentic systems can type "agentic".

The result needs no stemmer dependency and no exception list.
"""

from __future__ import annotations

import re

__all__ = [
    "SENIORITY_PREFIXES",
    "has_term",
    "norm_key",
    "term_hits",
    "term_pattern",
    "title_points",
    "title_score",
]

# A term shorter than this gets no plural suffix. "icu" + "s" would match "icus", which is
# harmless, but the guard keeps very short terms from growing surprising surface area.
_MIN_PLURAL_LEN = 4

# Leading words stripped from BOTH sides before the core-role comparison, so that
# "Senior AI Product Builder" and "Staff AI Product Builder" are recognised as the same
# role at a different level rather than as unrelated titles.
SENIORITY_PREFIXES = (
    "head of",
    "director of",
    "senior",
    "staff",
    "principal",
    "junior",
    "lead",
    "sr",
    "jr",
)

# Doubled from 50/40/30/15 on 2026-08-02. The original values let a listing with NO
# title match at all out-score a perfect one: a "Consultant" whose body repeated all nine
# of a user's boost terms scored 64 against an exact-title $235-315k role's 40. Boosts are
# meant to be a bonus ON TOP of wanting the role, not a substitute for it, and at the old
# values only 7 single-occurrence boosts were needed to overwhelm an exact title.
#
# Doubling makes the title the anchor it is supposed to be — an exact match now needs 13
# such boosts to be overturned, which covers 39 of the 57 test users outright. It does NOT
# fully close the gap for the 18 users who give 9+ boosts, and that residual is deliberate:
# whether a heavily-matching listing with an unrecognisable title is a keyword-stuffed
# impostor or a genuinely good job under an odd name is not something the arithmetic can
# see. That is what the judged comparison is for.
_TIER_EXACT = 100
_TIER_ALL_WORDS = 80
_TIER_CORE = 60
_TIER_RELATED = 30
_TIER_NONE = 0

# WHAT THE RELATED TIER IS FOR. It was always specified as "AI-generated titles" — the
# roles the model suggests once the user's own list is final. Nothing generated any, so
# the slot was occupied by a stand-in: a job title counted as related if it shared half
# its words with something the user typed.
#
# That heuristic was right by accident. Measured across the 57 test users it carried 19
# listings, and it caught "Warehouse Lead" for a wanted "Warehouse Supervisor" — and
# also "Senior Full-Stack Developer" for a wanted "Instructional Designer", on the
# strength of sharing "Design". A model asked for related roles suggests the first and
# would never suggest the second.
#
# So the fraction rule is gone and the tier holds what it was named for. A related match
# is a FLAT 30 — no weight, no fraction of a primary tier. It is worth less than any
# match against a title the user actually named, and more than nothing.

_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


# ═══════════════════════════════════════════════════════════════
# norm_key()
# ═══════════════════════════════════════════════════════════════
# Lowercase + collapse whitespace/punctuation, for identity comparison.
# Moved here from server.py so the matcher and the deduper share one
# normaliser instead of drifting apart.
# ═══════════════════════════════════════════════════════════════
def norm_key(value) -> str:
    return " ".join(
        "".join(ch if ch.isalnum() else " " for ch in str(value or "")).split()
    ).lower()


# ═══════════════════════════════════════════════════════════════
# term_pattern()
# ═══════════════════════════════════════════════════════════════
# Compile a search term into a whole-word regex that also accepts a
# plural. Multi-word terms match as a PHRASE — the words joined by
# whitespace, with the suffix applied to the last word only. Matching
# them independently would count "warehouse ... automation" scattered
# across a paragraph as a hit and inflate every downstream number.
# Cached, because the same handful of terms is tested against tens of
# thousands of listings per request.
# ═══════════════════════════════════════════════════════════════
def term_pattern(term: str) -> re.Pattern[str] | None:
    key = norm_key(term)
    if not key:
        return None
    cached = _PATTERN_CACHE.get(key)
    if cached is not None:
        return cached
    words = key.split()
    body = r"\s+".join(re.escape(w) for w in words[:-1] + [words[-1]])
    if len(words[-1]) >= _MIN_PLURAL_LEN:
        body += r"(?:s|es)?"
    pattern = re.compile(rf"\b{body}\b", re.IGNORECASE)
    _PATTERN_CACHE[key] = pattern
    return pattern


# ═══════════════════════════════════════════════════════════════
# term_hits()
# ═══════════════════════════════════════════════════════════════
# How many times the term occurs in the text. Occurrences are what the
# scoreboard's 8/6/4/2 decay reads: a listing that names RAG twenty
# times is more about RAG than one that mentions it once, and that is
# real signal rather than length noise.
# Non-overlapping (re.finditer); only the first four occurrences carry
# any points, so overlap semantics are not load-bearing.
# ═══════════════════════════════════════════════════════════════
def term_hits(term: str, text: str) -> int:
    pattern = term_pattern(term)
    if pattern is None or not text:
        return 0
    return sum(1 for _ in pattern.finditer(text))


# ═══════════════════════════════════════════════════════════════
# has_term()
# ═══════════════════════════════════════════════════════════════
# Presence test. Short-circuits on the first hit rather than counting
# them all — used by the penalty path, which only cares whether the
# term is there.
# ═══════════════════════════════════════════════════════════════
def has_term(term: str, text: str) -> bool:
    pattern = term_pattern(term)
    if pattern is None or not text:
        return False
    return pattern.search(text) is not None


def _strip_seniority(normalised: str) -> str:
    """Drop one leading seniority marker. Applied to both sides of a comparison."""
    for prefix in SENIORITY_PREFIXES:
        if normalised.startswith(prefix + " "):
            return normalised[len(prefix) + 1 :]
    return normalised


# ═══════════════════════════════════════════════════════════════
# title_points()
# ═══════════════════════════════════════════════════════════════
# Score one job title against ONE title, on the three tiers that mean
# "the user asked for this". Returns the BEST single tier — tiers do
# not add, so a listing cannot collect 100 and 80 for the same title.
# The related tier is not here on purpose: it is not a property of a
# title pair, it is a statement about WHOSE list the title came from.
# title_score() below is what applies it.
#
# Mechanical by design: no similarity model, no embedding, no judgment
# call. Deciding what counts as "close enough" is not the ranker's job
# — the interview asks the model for it explicitly, and the answer
# arrives as `related_titles`.
# ═══════════════════════════════════════════════════════════════
def title_points(user_title: str, job_title: str) -> int:
    want = norm_key(user_title)
    got = norm_key(job_title)
    if not want or not got:
        return _TIER_NONE

    if want == got:
        return _TIER_EXACT

    if all(w in set(got.split()) for w in want.split()):
        return _TIER_ALL_WORDS

    if _strip_seniority(want) == _strip_seniority(got):
        return _TIER_CORE

    return _TIER_NONE


# ═══════════════════════════════════════════════════════════════
# title_score()
# ═══════════════════════════════════════════════════════════════
# The whole ladder, in one call: the best tier across every title
# the user named, and only if NONE of them land, a flat 30 for
# matching one of the model's suggestions.
#
# The precedence is the point. A related title can never outrank a
# real one — not even an exact match on a suggestion beats a
# seniority-shifted match on a role the user actually asked for.
# The user's own words always win.
# ═══════════════════════════════════════════════════════════════
def title_score(titles, related_titles, job_title: str) -> tuple[int, bool]:
    """(points, is_related). `is_related` labels the card's receipt."""
    best = max((title_points(t, job_title) for t in titles or [] if t), default=0)
    if best:
        return best, False
    for t in related_titles or []:
        if t and title_points(t, job_title):
            return _TIER_RELATED, True
    return _TIER_NONE, False
