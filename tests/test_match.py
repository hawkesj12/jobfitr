"""The matcher — unit contract plus a corpus-anchored regression.

Two layers, and the second is the point. The unit tests pin the RULE; the corpus tests
pin the NUMBERS the rule produces against the real 39,597-row snapshot. Those numbers are
what the whole ranking rebuild is measured against, so if a future change to the matcher
moves them, it should fail here loudly rather than quietly shift every downstream result.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from jobfitr.match import has_term, norm_key, term_hits, title_points, title_score

# ── unit: the matching rule ──────────────────────────────────────────────────

ACCEPTS = [
    ("rag", "we use rag pipelines", "bare term"),
    ("stakeholder", "align with stakeholders weekly", "plural"),
    ("warehouse", "two warehouses in ohio", "plural"),
    ("startup", "backed by several startups", "plural"),
    ("icu", "night shift in the icu", "short term, no plural rule"),
    ("excel", "advanced excel required", "bare term"),
    ("warehouse automation", "warehouse automation experience", "multi-word phrase"),
    ("supply chain", "global supply chains", "multi-word, plural on last word"),
    ("RAG", "we use rag", "case-insensitive"),
]

REJECTS = [
    ("rag", "leverage cloud storage", "the 97%-false case: leverage, storage"),
    ("rag", "coverage across fragmented teams", "coverage, fragmented"),
    ("excel", "excellent communication skills", "the case Porter stemming fails"),
    ("lean", "clean the work area", "clean"),
    ("icu", "a particularly difficult curricula", "particular, difficult, curricula"),
    ("agent", "agentic systems design", "accepted miss — documented, not a bug"),
    (
        "warehouse automation",
        "warehouse ops and test automation",
        "phrase, not two words",
    ),
    ("supply chain", "supply the chain of command", "phrase, not two words"),
]


@pytest.mark.parametrize(
    "term,text,why", ACCEPTS, ids=[a[0] + ":" + a[2] for a in ACCEPTS]
)
def test_matcher_accepts(term, text, why):
    assert term_hits(term, text) >= 1, why
    assert has_term(term, text), why


@pytest.mark.parametrize(
    "term,text,why", REJECTS, ids=[r[0] + ":" + r[2] for r in REJECTS]
)
def test_matcher_rejects(term, text, why):
    assert term_hits(term, text) == 0, why
    assert not has_term(term, text), why


def test_term_hits_counts_occurrences():
    """The 8/6/4/2 decay reads occurrence COUNT, not just presence."""
    assert term_hits("rag", "rag and rag and more rag") == 3
    assert term_hits("rag", "no match here") == 0


def test_empty_and_missing_inputs_are_safe():
    for term in ("", "   ", None):
        assert term_hits(term, "anything") == 0
        assert has_term(term, "anything") is False
    assert term_hits("rag", "") == 0
    assert has_term("rag", "") is False


# ── unit: the title tiers ────────────────────────────────────────────────────

WANT = "Senior AI Product Builder"

TIERS = [
    ("Senior AI Product Builder", 100, "exact"),
    ("senior  ai   product builder", 100, "exact after normalisation"),
    ("AI Product Builder, Senior", 80, "all words, any order"),
    ("Staff AI Product Builder", 60, "core role, different seniority"),
    ("Principal AI Product Builder", 60, "core role, different seniority"),
    ("AI Product Manager", 0, "half the words is NOT a tier — the related tier holds "
     "the model's suggestions, not a word count"),
    ("Registered Nurse", 0, "unrelated"),
    ("", 0, "empty job title"),
]


@pytest.mark.parametrize(
    "job,pts,why", TIERS, ids=[t[2] + "->" + str(t[1]) for t in TIERS]
)
def test_title_tiers(job, pts, why):
    assert title_points(WANT, job) == pts, why


def test_tiers_do_not_stack():
    """An exact match scores 100, not 100+80+60 — the best single tier only."""
    assert title_points(WANT, WANT) == 100


# ── the related tier: the model's suggestions, flat 30 ───────────────────────

SUGGESTED = ["AI Engineer", "Applied Scientist"]


def test_a_suggested_title_earns_the_related_tier():
    pts, is_related = title_score([WANT], SUGGESTED, "AI Engineer")
    assert (pts, is_related) == (30, True)


def test_a_suggested_title_is_flat_30_however_well_it_matches():
    """No weight, no fraction of a primary tier. An EXACT match on a suggestion scores
    the same 30 as a seniority-shifted one, because the tier is a statement about whose
    list the title came from — not about how close the strings are."""
    for job in ("AI Engineer", "Senior AI Engineer", "AI Engineer, Staff"):
        assert title_score([WANT], SUGGESTED, job) == (30, True)


def test_the_users_own_title_always_wins():
    """Precedence, not addition. Even the WEAKEST primary tier outranks the strongest
    possible related match — the user's own words beat the machine's guess."""
    pts, is_related = title_score([WANT], ["Staff AI Product Builder"], "Staff AI Product Builder")
    assert (pts, is_related) == (60, False), "core+modifier on a named title, not related"


def test_no_suggestions_means_no_related_tier():
    """Every stored search predates this field. Absent must score exactly as before."""
    assert title_score([WANT], None, "AI Engineer") == (0, False)
    assert title_score([WANT], [], "AI Engineer") == (0, False)


def test_the_word_overlap_rule_is_gone():
    """REGRESSION. A job title sharing half its words used to earn 30 on that alone —
    which caught 'Warehouse Lead' for 'Warehouse Supervisor' and also 'Senior Full-Stack
    Developer' for 'Instructional Designer'. Right by accident; now it scores nothing
    unless the model actually suggested it."""
    assert title_score(["Instructional Designer"], [], "Senior Full-Stack Developer") == (0, False)
    assert title_score(["Warehouse Supervisor"], [], "Warehouse Lead") == (0, False)
    # ...and comes back the moment it IS suggested
    assert title_score(["Warehouse Supervisor"], ["Warehouse Lead"], "Warehouse Lead") == (30, True)


def test_title_outweighs_a_keyword_stuffed_body():
    """The regression that forced the doubling: an exact title must beat a listing with
    NO title match whose body merely repeats the user's boost terms.

    Observed in production — a $235-315k exact-title role ranked below a $94,876
    off-title listing. At the old 50/40/30/15 values only 7 single-occurrence boosts
    were needed to overturn an exact title; it now takes 13.
    """
    assert title_points(WANT, WANT) > 8 * 12  # 12 boosts, one occurrence each


def test_empty_user_title_scores_nothing():
    assert title_points("", "AI Engineer") == 0


def test_norm_key_matches_the_behaviour_it_replaced():
    """Moved from server.py; the deduper depends on this exact shape."""
    assert norm_key("  Acme,  Inc. ") == "acme inc"
    assert norm_key(None) == ""
    assert norm_key("Senior AI/ML Engineer") == "senior ai ml engineer"


# ── corpus regression: the numbers the rebuild is measured against ───────────

_FROZEN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "_private",
    "before-after",
    "frozen.db",
)

# Measured 2026-08-01 against frozen.db (39,597 rows). Counting concatenates
# title + " " + body, lowercased — a DIFFERENT join will not reproduce these and will
# look like a matcher bug rather than a test bug.
MEASURED = {
    "rag": 171,  # rejects storage / leverage / coverage
    "stakeholder": 3259,  # accepts the plural
    "warehouse": 719,  # accepts the plural
    "startup": 2377,  # accepts the plural
    "icu": 15,  # short term, no plural rule
    "excel": 646,  # rejects "excellent" — the case Porter stemming fails
    "agent": 3263,  # the accepted "agentic" miss, pinned so it cannot drift silently
}


@pytest.fixture(scope="module")
def corpus():
    """Every listing as `title + " " + body`, lowercased. Read-only."""
    con = sqlite3.connect(f"file:{_FROZEN}?mode=ro", uri=True)
    try:
        rows = [
            ((t or "") + " " + (b or "")).lower()
            for t, b in con.execute("SELECT title, COALESCE(body,'') FROM jobs")
        ]
    finally:
        con.close()
    return rows


@pytest.mark.skipif(
    not os.path.exists(_FROZEN),
    reason="frozen corpus is gitignored — the unit tests carry the contract without it",
)
@pytest.mark.parametrize("term,expected", sorted(MEASURED.items()))
def test_corpus_counts_match_the_measurement(corpus, term, expected):
    actual = sum(1 for text in corpus if has_term(term, text))
    tolerance = max(1, round(expected * 0.01))
    assert abs(actual - expected) <= tolerance, (
        f"{term!r}: matched {actual:,} listings, measured {expected:,}. "
        "Every ranking number downstream is computed on this matcher — if the rule "
        "changed on purpose, re-measure and update MEASURED with the new value and why."
    )


@pytest.mark.skipif(not os.path.exists(_FROZEN), reason="frozen corpus is gitignored")
def test_substring_matching_would_have_been_catastrophic(corpus):
    """The bug this module exists to remove, pinned so nobody reintroduces it."""
    substring = sum(1 for text in corpus if "rag" in text)
    matched = sum(1 for text in corpus if has_term("rag", text))
    assert substring > 6000, (
        "expected the old substring behaviour to be wildly permissive"
    )
    assert matched < substring / 30, (
        f"substring matched {substring:,} listings for 'rag'; whole-word matched "
        f"{matched:,}. The gap IS the bug — 97% of substring hits were 'leverage', "
        "'storage' and 'coverage'."
    )
