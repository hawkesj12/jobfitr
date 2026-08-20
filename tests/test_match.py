"""Term matching — what survived the scoreboard.

`match.py` used to hold the title tier ladder (100/80/60/30) and the boost decay too.
Those went with the scoreboard on 2026-08-20; the tests for them went with them.

What is left is load-bearing on a path that is very much alive: `_eligible` filters the
user's exclusions with `has_term`, on every /api/candidates request. The rule it pins is
WHOLE-WORD matching — the substring form deleted `Software Engineer, Internal Systems`
under "intern" and `…Salesforce Health Cloud` under "sales", 220 of 2,573 exclusions
(8.6%) being false hides. That is why these tests are worth keeping.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from jobfitr.match import _stem, has_term, norm_key, term_hits, term_pattern

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






# ── the related tier: the model's suggestions, flat 30 ───────────────────────

SUGGESTED = ["AI Engineer", "Applied Scientist"]
















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


# ── the root title ────────────────────────────────────────────────────────────
# job_radar 0.7.0 parses every title into a root with seniority and decoration removed.
# The scorer reads BOTH surfaces and takes the best tier — never a swap.
ROOT_CASES = [
    # want, job title, root, without root, with root, why
    ("Application Security Engineer", "Senior Application Security Engineer (Remote)",
     "Application Security Engineer", 80, 100,
     "the founding example — these are the same role"),
    ("Senior AI Engineer", "AI Engineer II", "AI Engineer", 0, 60,
     "the case that started the rebuild: a level suffix used to score ZERO"),
    ("AI Engineer", "Senior AI Engineer I", "AI Engineer", 80, 100,
     "seniority and a level are decoration, not a different role"),
    ("Senior AI Product Builder", "Staff AI Product Builder", "AI Product Builder",
     60, 60, "a different seniority is still the core-role tier, unchanged"),
    ("AI Engineer", "Software Engineer, Applied AI", "Software Engineer", 80, 80,
     "THE REASON IT IS A MAX: swapping would score this 0, and 3,202 pairs like it"),
]










# ── the substring gate ───────────────────────────────────────────────────────
def test_the_stem_gate_never_changes_an_answer():
    """`term_hits`/`has_term` skip the regex when the term's first word is not even a
    substring of the text. It is an OPTIMISATION ONLY — worth 2,071 ms -> 472 ms on the
    widest profile once bodies went to 8,000 chars — so any answer it changes is a bug.

    The cases below are chosen to attack the gate's assumption that the first word must
    appear literally: plurals (the suffix goes on the LAST word), hyphens (the
    alternative sits BETWEEN words), and punctuation inside a term."""
    cases = [
        ("rag", "we use RAG and RAGs heavily"),
        ("rag", "storage leverage coverage"),  # substring traps — must NOT match
        ("forward deployed", "a forward-deployed engineer"),
        ("forward deployed", "forward deployed engineer"),
        ("forward deployed", "deployed forward"),
        ("month end close", "owns the month end close cycle"),
        ("node.js", "we run Node.js in production"),
        ("c++", "strong C++ background"),
        ("variance analysis", "no such phrase here"),
        ("excel", "excels at communication"),  # whole-word: must not match
    ]
    for term, text in cases:
        gated_hits = term_hits(term, text)
        gated_has = has_term(term, text)
        # recompute WITHOUT the gate, straight off the pattern
        pat = term_pattern(term)
        raw_hits = sum(1 for _ in pat.finditer(text)) if pat else 0
        raw_has = bool(pat and pat.search(text))
        assert gated_hits == raw_hits, f"{term!r} in {text!r}: {gated_hits} != {raw_hits}"
        assert gated_has == raw_has, f"{term!r} in {text!r}: {gated_has} != {raw_has}"


def test_the_stem_is_the_first_word_normalised():
    assert _stem("Month End Close") == "month"
    assert _stem("forward-deployed") == "forward"
    assert _stem("node.js") == "node"
    assert _stem("") == ""


def test_the_gate_is_case_insensitive_like_the_pattern_it_guards():
    """The bug this nearly shipped. Patterns compile with re.IGNORECASE, so a
    case-SENSITIVE gate returns 0 for 'we use RAG' while the regex finds it. Every
    caller in this repo pre-lowercases, which is precisely what would have kept it
    hidden."""
    # 1, not 2: "rag" is 3 chars and _MIN_PLURAL_LEN is 4, so no plural suffix is
    # added and "RAGs" is correctly not a match. The gate must not change that either.
    assert term_hits("rag", "we use RAG and RAGs heavily") == 1
    assert term_hits("stakeholder", "STAKEHOLDERS and Stakeholder alike") == 2
    assert has_term("staffing", "A STAFFING AGENCY") is True
    assert term_hits("month end close", "owns the MONTH-END CLOSE") == 1


def test_lowered_is_opt_in_and_never_assumed():
    """B4. The gate must see lowercase text or it silently returns 0 while the
    re.IGNORECASE pattern would have matched. The fast path is therefore OPT-IN: the
    default keeps a function that cannot be wrong for a caller who did not read the
    docstring, and only `_rank` — which already lowercases everything — opts out of the
    per-term copy of an 8,000-character body."""
    # default path: correct on ANY case
    assert term_hits("rag", "we use RAG heavily") == 1
    assert has_term("staffing", "A STAFFING AGENCY") is True
    # opt-in path on already-lowered text: identical answer
    assert term_hits("rag", "we use rag heavily", lowered=True) == 1
    assert has_term("staffing", "a staffing agency", lowered=True) is True
    # and the two agree wherever the text is already lower
    for term, text in [("python", "senior python engineer"), ("rag", "no match here"),
                       ("month end close", "owns the month-end close")]:
        assert term_hits(term, text) == term_hits(term, text, lowered=True), term
        assert has_term(term, text) == has_term(term, text, lowered=True), term
