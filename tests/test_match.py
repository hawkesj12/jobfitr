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

from jobfitr.match import has_term, norm_key, term_hits, title_points

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
    ("Senior AI Product Builder", 50, "exact"),
    ("senior  ai   product builder", 50, "exact after normalisation"),
    ("AI Product Builder, Senior", 40, "all words, any order"),
    ("Staff AI Product Builder", 30, "core role, different seniority"),
    ("Principal AI Product Builder", 30, "core role, different seniority"),
    ("AI Product Manager", 15, "related — 2 of 4 words"),
    ("Registered Nurse", 0, "unrelated"),
    ("", 0, "empty job title"),
]


@pytest.mark.parametrize(
    "job,pts,why", TIERS, ids=[t[2] + "->" + str(t[1]) for t in TIERS]
)
def test_title_tiers(job, pts, why):
    assert title_points(WANT, job) == pts, why


def test_tiers_do_not_stack():
    """An exact match scores 50, not 50+40+30 — the best single tier only."""
    assert title_points(WANT, WANT) == 50


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
