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
from functools import lru_cache

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
    # Words are joined by whitespace OR a hyphen, because English writes the same
    # compound both ways and a job description picks whichever its author preferred.
    #
    # This was `\s+`, and the 1.5 audit caught it twice, independently, in unrelated
    # domains: one reader saw a listing spelling it both "forward deployed" and
    # "forward-deployed" and counted two occurrences where the code counted one; another
    # saw "month-end close" — the standard accounting spelling — twice in a body the
    # matcher scored as zero. Measured across the 39,597-row corpus, the spaced-only
    # pattern missed the MAJORITY of real occurrences for hyphen-conventional terms:
    #
    #     term               matched   missed
    #     end to end             773    3,433
    #     data driven             60    1,472
    #     problem solving        242      718
    #     full stack             375      537
    #
    # And it silently zeroed terms a user actually typed with a hyphen: norm_key strips
    # the hyphen from the TERM, so "multi-agent orchestration" became the spaced pattern
    # and then could not match the hyphenated text everyone writes — 0 matches in 39,597
    # listings, alongside "customer-embedded". Two of one test user's fourteen boosts had
    # never contributed a single point.
    body = r"[\s-]+".join(re.escape(w) for w in words[:-1] + [words[-1]])
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
#
# THE COUNT, SAID PRECISELY, because the decay depends on it and the
# 1.5 audit showed readers arriving at different numbers:
#
#   - Matches are NON-OVERLAPPING (re.finditer), scanning left to right.
#   - A term of 4+ characters also matches a trailing "s" or "es", so
#     "warehouse" counts "warehouses". Shorter terms do not, so "icu"
#     does not count "icus".
#   - A multi-word term counts only as a PHRASE — the words adjacent
#     and in order, separated by whitespace or a hyphen. "warehouse"
#     and "automation" scattered across a paragraph is not a hit.
#   - Only the first FOUR occurrences carry points (8/6/4/2, 20 total).
#     The fifth and every later one add exactly nothing, so counting
#     past four never changes a score — which is why overlap semantics
#     are not load-bearing.
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
# THE RULES, WRITTEN SO A STRANGER CAN FOLLOW THEM. This wording is
# not decoration — it is the spec five independent readers scored 250
# listings against in the 1.5 audit, and one of them flagged six cases
# as ambiguous because the old phrasing did not say this out loud:
#
#   Every comparison is WHOLE WORD, after normalising (lowercase,
#   punctuation to spaces, collapse runs of space). "engineering" does
#   NOT satisfy a want for "engineer" — they are different words, and
#   no stemming or prefix matching happens anywhere in this file. The
#   reader who flagged it guessed strict whole-word and matched the
#   code on all six, so the RULE was right; the sentence describing it
#   was not.
#
#   100  exact      the two normalise to the same string
#    80  all words  every word of the USER's title appears somewhere in
#                   the JOB's title, any order. The job title may carry
#                   extra words; the user's may not go unmatched. So
#                   "Senior Data Engineer" satisfies a want for "Data
#                   Engineer", but not the reverse.
#    60  core       equal after removing ONE leading seniority word from
#                   each side (see SENIORITY_PREFIXES). Only the leading
#                   one, and only if it is followed by a space.
#     0  none
#
# Mechanical by design: no similarity model, no embedding, no judgment
# call. Deciding what counts as "close enough" is not the ranker's job
# — the interview asks the model for it explicitly, and the answer
# arrives as `related_titles`.
# ═══════════════════════════════════════════════════════════════
def title_points(user_title: str, job_title: str) -> int:
    got = norm_key(job_title)
    return _tier(_prepared(user_title), got, set(got.split())) if got else _TIER_NONE


@lru_cache(maxsize=512)
def _prepared(title: str) -> tuple[str, frozenset[str], str]:
    """A title reduced to the three forms the tiers compare against.

    Cached because the caller asks for the SAME handful of strings over and over: a
    search scores one user's 2-15 titles against every candidate, so without this the
    user's own titles were re-normalised once per candidate — roughly 50,000 identical
    norm_key calls per request for a wide search, all producing the same answer.
    Measured on the real corpus: 158 ms of title tiering became 9 ms, with zero
    disagreements across every candidate.

    Bounded at 512 so a long-lived process cannot accumulate entries for the job titles
    that also pass through here; those are mostly distinct and would otherwise turn a
    cache into a leak.
    """
    key = norm_key(title)
    return key, frozenset(key.split()), _strip_seniority(key)


def _tier(want: tuple[str, frozenset[str], str], got: str, got_words: set) -> int:
    """The three tiers, against a job title that is ALREADY normalised.

    Split out so the caller normalises the job side once per candidate rather than once
    per (candidate × user title) — the other half of the same redundancy.
    """
    key, words, stripped = want
    if not key:
        return _TIER_NONE
    if key == got:
        return _TIER_EXACT
    if words <= got_words:
        return _TIER_ALL_WORDS
    if stripped == _strip_seniority(got):
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
def title_score(
    titles, related_titles, job_title: str, job_title_root: str = ""
) -> tuple[int, bool]:
    """(points, is_related). `is_related` labels the card's receipt.

    A listing is compared on TWO surfaces: the title the employer wrote, and the same
    title with its seniority and decoration stripped (job_radar's `title_root`). The
    best tier across both wins — never a swap.

    THE MAX FORM IS THE WHOLE POINT, and it is measured. Swapping the root in regresses
    3,254 pairs, 3,202 of them 80 -> 0: `Software Engineer, Applied AI` scores 80 for
    someone wanting "AI Engineer" on the full title and 0 on the root, because the root
    is where the qualifier went. Taking the max can only ever RAISE a tier, so it needs
    no relevance judgment to be safe — over all 93,139 retrieved pairs it changes 10,452
    with ZERO regressions.

    Both surfaces are folded in HERE rather than by max()-ing two calls, so the
    precedence rule survives untouched: a related title can never outrank a real one,
    because every title the user actually named — on either surface — is tried before
    any suggestion is considered. Two separate calls would each apply that rule to half
    the evidence and then have to reconcile the flags.

    `job_title_root` defaults to "" so every caller that predates the column, and the
    234 goldens, score exactly as they did.
    """
    got = norm_key(job_title)  # once per candidate, not once per user title
    root = norm_key(job_title_root)
    if root == got:
        root = ""  # the common case: nothing was stripped, so do not compare twice
    if not got and not root:
        return _TIER_NONE, False
    surfaces = [(g, set(g.split())) for g in (got, root) if g]
    best = max(
        (_tier(_prepared(t), g, gw) for t in titles or [] if t for g, gw in surfaces),
        default=0,
    )
    if best:
        return best, False
    for t in related_titles or []:
        if t and any(_tier(_prepared(t), g, gw) for g, gw in surfaces):
            return _TIER_RELATED, True
    return _TIER_NONE, False
