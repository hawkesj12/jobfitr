"""The scoreboard's arithmetic — invariants over the real corpus, plus frozen goldens.

TWO LAYERS, AND THEY CATCH DIFFERENT THINGS.

**Invariants** compare the scorer to ITSELF under transformation: add a boost, permute
the list, run it twice. They need no second implementation, they sweep the whole corpus
in seconds, and they pin the promises the product makes out loud — the interview tells
users to name as many skills as they can think of, and the old fraction-based formula
made that a lie (3 of 14 boosts scored BELOW 0 of 0). What they cannot catch is a rule
implemented consistently wrong.

**Goldens** are the cases five independent AI readers computed by hand from the spec
prose, with no access to this repo's code, then adjudicated. That is the one check a
reference scorer written by the same author structurally cannot be: if the author
misreads the spec, they misread it twice and the two implementations agree.

The audit itself is nondeterministic and metered, so it does not live here — it ran once
(`_private/before-after/audit_cases.py` → agents → `audit_compare.py`) and its cleared
cases were frozen below.
"""

from __future__ import annotations

import os
import random

import pytest

from jobfitr.server import scoreboard

# ── corpus fixtures ──────────────────────────────────────────────────────────

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FROZEN = os.path.join(_ROOT, "_private", "before-after", "frozen.db")
_USERS = os.path.join(_ROOT, "_private", "before-after", "users.json")

needs_corpus = pytest.mark.skipif(
    not os.path.exists(_FROZEN),
    reason="the frozen corpus is gitignored — the unit layer carries the contract without it",
)

# Scoring 123,015 pairs takes ~16s per pass and the monotonicity checks need two, so the
# default run works a deterministic slice and the full sweep is opted into with -m slow.
# The slice is per-user rather than random: it keeps whole configs intact, so the rare
# combinations (14 boosts against 7 penalty terms) survive the sampling.
SAMPLE_PER_USER = 40
SEED = 20260802  # fixed — a failure has to be reproducible


def _load_pairs(full: bool):
    import json

    from jobfitr import store
    from jobfitr.config_builder import config_from_dict
    from jobfitr.server import _dedupe_listings

    rng = random.Random(SEED)
    out = []
    for u in json.loads(open(_USERS).read())["users"]:
        cfg = u["config"]
        rows = _dedupe_listings(
            store.bm25_candidates(
                cfg["titles"] + (cfg.get("related_titles") or []), None, _FROZEN
            )
        )
        if not full:
            rows = rng.sample(rows, min(SAMPLE_PER_USER, len(rows)))
        # Penalties as the SERVER derives them, not raw rank_down: a user who named none
        # gets twelve generic staffing terms injected, so reading rank_down directly
        # would test a scorer that never runs.
        penalties = list(config_from_dict(cfg).agency_penalty.keys())
        for r in rows:
            out.append(
                {
                    "id": u["id"],
                    "title": (r.get("title") or "").lower(),
                    "company": (r.get("company") or "").lower(),
                    "body": (r.get("body") or "").lower(),
                    "titles": cfg["titles"],
                    "boosts": cfg.get("boosts") or [],
                    "penalties": penalties,
                    "related": cfg.get("related_titles") or [],
                }
            )
    return out


@pytest.fixture(scope="module")
def pairs():
    return _load_pairs(full=False)


def _score(p, **over):
    d = {**p, **over}
    return scoreboard(
        d["title"],
        d["company"],
        d["body"],
        d["titles"],
        d["boosts"],
        d["penalties"],
        d["related"],
    )


# ── invariants: the promises the product makes out loud ──────────────────────


@needs_corpus
def test_naming_another_skill_can_never_cost_you(pairs):
    """The interview says to give as many skills as you can think of. If adding one
    could LOWER a score, the interview is lying to the people who follow it.

    This is not hypothetical — the formula this replaced computed the boost half as a
    FRACTION of the boosts given, so a listing matching 3 of 14 scored below one
    matching 0 of 0. Everyone who answered the question generously was punished for it.
    """
    for p in pairs:
        base = _score(p)["points"]
        more = _score(p, boosts=[*p["boosts"], "kubernetes"])["points"]
        assert more >= base, f"{p['id']} · {p['title']!r}: {base} -> {more}"


@needs_corpus
def test_naming_another_thing_to_avoid_can_never_raise_a_score(pairs):
    for p in pairs:
        base = _score(p)["points"]
        more = _score(p, penalties=[*p["penalties"], "internship"])["points"]
        assert more <= base, f"{p['id']} · {p['title']!r}: {base} -> {more}"


@needs_corpus
def test_a_suggestion_can_help_but_never_beyond_the_related_tier(pairs):
    """Two promises at once: a suggested title never costs the user anything, and it can
    never lift a listing by more than the 30 the related tier is worth. A suggestion
    outscoring a title the user actually named would invert the whole precedence."""
    for p in pairs:
        base = _score(p)["points"]
        more = _score(p, related=[*p["related"], p["title"]])["points"]
        assert base <= more <= base + 30, (
            f"{p['id']} · {p['title']!r}: {base} -> {more}"
        )


@needs_corpus
def test_the_order_boosts_were_typed_in_does_not_change_the_score(pairs):
    """Catches order-dependent accumulation — a bug that would make a user's score
    depend on which skill they happened to mention first."""
    rng = random.Random(SEED)
    for p in pairs:
        if len(p["boosts"]) < 2:
            continue
        shuffled = list(p["boosts"])
        rng.shuffle(shuffled)
        assert _score(p)["points"] == _score(p, boosts=shuffled)["points"], p["id"]


@needs_corpus
def test_scoring_is_deterministic(pairs):
    for p in pairs[:2000]:
        assert _score(p)["points"] == _score(p)["points"]


# ── the receipt: the card renders `parts`, so they had better add up ─────────


@needs_corpus
def test_the_parts_sum_to_the_points(pairs):
    """1.6 renders these as the why-chips. If they do not sum to the number beside them,
    the card is lying about where the score came from — which is worse than showing no
    breakdown at all, because it invites a user to trust a wrong explanation."""
    for p in pairs:
        b = _score(p)
        assert sum(d for _, d in b["parts"]) == b["points"], f"{p['id']}: {b['parts']}"


@needs_corpus
def test_the_components_decompose(pairs):
    for p in pairs:
        b = _score(p)
        assert (
            b["points"] == b["title_points"] + b["boost_points"] - b["penalty_points"]
        ), f"{p['id']}: {b}"


@needs_corpus
def test_every_part_is_a_legal_value(pairs):
    """Bounds catch silent drift with no second implementation needed: a decay applied
    as 8/6/4/2/2 shows up here as a boost part of 22, and a mis-set tier as a title of 50.
    """
    for p in pairs:
        b = _score(p)
        assert b["title_points"] in (0, 30, 60, 80, 100), b["title_points"]
        for label, delta in b["parts"]:
            if label in ("title", "related title"):
                continue
            assert delta in (8, 14, 18, 20, -15, -30), f"{label} {delta} · {p['id']}"
        assert b["boost_points"] <= 20 * len(p["boosts"])


# ── the same invariants over every pair, not a slice ─────────────────────────


@pytest.mark.slow
@needs_corpus
def test_the_full_corpus_decomposes_and_stays_in_bounds():
    """All 123,015 (user × listing) pairs in one pass, ~20s. Run before every version
    capture: the sampled layer above is a dev-loop convenience, never the proof of
    record."""
    n = 0
    for p in _load_pairs(full=True):
        b = _score(p)
        assert sum(d for _, d in b["parts"]) == b["points"], f"{p['id']}: {b['parts']}"
        assert (
            b["points"] == b["title_points"] + b["boost_points"] - b["penalty_points"]
        )
        assert b["title_points"] in (0, 30, 60, 80, 100)
        n += 1
    assert n > 100_000, f"expected the full corpus, got {n:,} pairs"


# ── goldens: 183 cases five independent readers computed by hand ─────────────
#
# These came from an audit, not from a second implementation. Each case was handed to an
# AI agent as the scoring rules in PROSE plus the raw listing, with no access to this
# repo, and the agent computed the total by hand. 183 of 200 matched the scorer exactly.
#
# That independence is the whole point. A reference scorer written by the same author who
# read the spec reproduces the author's misreadings and then agrees with itself — the
# differential certifies the bug. A reader who has never seen the code cannot do that.
#
# The 17 disagreements are NOT here. They are adjudicated in audit/report.md, and every
# one turned out to be a defect in the SPEC rather than in the arithmetic — penalties
# firing on the wrong word sense, and multi-word boosts blind to hyphens. Freezing a
# disputed number would pin the bug it revealed.
#
# The fixture lives under _private/ with the corpus it was drawn from, so the public repo
# does not carry 366 KB of job bodies.

_GOLDENS = os.path.join(_ROOT, "_private", "before-after", "audit", "goldens.json")


def _golden_cases():
    if not os.path.exists(_GOLDENS):
        return []
    import json

    return json.load(open(_GOLDENS))["cases"]


@pytest.mark.skipif(
    not os.path.exists(_GOLDENS),
    reason="the audit fixture is gitignored with the corpus",
)
@pytest.mark.parametrize(
    "case", _golden_cases(), ids=[c["case_id"] for c in _golden_cases()]
)
def test_a_human_readable_of_the_spec_gets_the_same_number(case):
    got = scoreboard(
        case["job_title"],
        case["job_company"],
        case["job_body"],
        case["user_wants"],
        case["user_boosts"],
        case["user_avoids"],
        case["user_suggested"],
    )
    assert got["points"] == case["expected"], (
        f"{case['case_id']} (reader {case['agent']}): expected {case['expected']}, "
        f"got {got['points']} · {got['parts']}\n  reader's working: {case['working']}"
    )
