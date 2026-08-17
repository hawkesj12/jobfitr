"""The committed board universe — and above all, that a missing one is never a quiet zero.

The privacy tests in test_searchlog.py exist because one regression there would make the
front page a lie. These exist for the mirror reason: one regression here would make
discovery silently stop finding anything, which is precisely the bug this module replaced.

Board discovery used to mine Common Crawl live on the box. Common Crawl refuses that box's
IP, so the nightly logged `mined 0 unknown boards` — indistinguishable from "the crawl knew
of nothing new" — for long enough that `host`/`site` are NULL on all 7,940 ledger rows and
Workday has never resolved once. That is this project's signature failure shape, shared with
`_s(job.get(...))` writing `''` for a dropped schema field and `searchlog.record()`
swallowing an EROFS: an error that renders as an empty success.

So the load-bearing assertions below are the NEGATIVE ones. A missing file must raise. A
malformed file must raise. An undated file must SAY it cannot judge staleness. None of those
may return an empty list, because an empty list is the bug.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from jobfitr import universe

_ET = ZoneInfo("America/New_York")


def _write(
    tmp_path,
    boards,
    *,
    generated_at=None,
    crawl="CC-MAIN-2026-30",
    meta=True,
    counts=None,
):
    """`counts` defaults to whatever the boards actually contain.

    That default matters: `counts` is the record of which ATSs the generator LOOKED FOR, and
    `for_ats` raises `UniverseNotQueried` for one that is absent. A helper hard-coding `{}`
    would make every read raise, which is how these tests first caught that behaviour.
    """
    from collections import Counter

    if meta:
        doc_counts = (
            counts if counts is not None else dict(Counter(b["ats"] for b in boards))
        )
    doc = {"boards": boards}
    if meta:
        doc["meta"] = {
            "crawl": crawl,
            "counts": doc_counts,
            "generated_at": generated_at
            if generated_at is not None
            else datetime.now(_ET).isoformat(timespec="seconds"),
        }
    p = tmp_path / "board-universe.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


# ── the silent-zero rule ─────────────────────────────────────────────────────


def test_a_missing_universe_raises_and_never_returns_an_empty_list(tmp_path):
    """The whole reason this module exists. [] here would reproduce the original bug."""
    missing = str(tmp_path / "nope.json")
    with pytest.raises(universe.UniverseUnavailable):
        universe.load(missing)
    with pytest.raises(universe.UniverseUnavailable):
        universe.for_ats("greenhouse", path=missing)


def test_the_missing_file_error_carries_the_regeneration_command(tmp_path):
    """A human step that is skipped must tell you how to un-skip it."""
    with pytest.raises(universe.UniverseUnavailable) as e:
        universe.load(str(tmp_path / "nope.json"))
    assert "mine_universe.py" in str(e.value)


def test_malformed_json_raises_rather_than_reading_as_no_boards(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(universe.UniverseUnavailable):
        universe.load(str(p))


def test_a_file_with_no_boards_list_raises(tmp_path):
    p = tmp_path / "shape.json"
    p.write_text(json.dumps({"meta": {}}), encoding="utf-8")
    with pytest.raises(universe.UniverseUnavailable):
        universe.load(str(p))


def test_an_explicitly_empty_universe_loads_rather_than_raising(tmp_path):
    """`[]` written on purpose is a real answer; `[]` from a failure is the bug. Only the
    second is forbidden, so an explicitly empty file must LOAD — the per-ATS complaint is a
    separate mechanism, tested below."""
    assert universe.load(_write(tmp_path, []))["boards"] == []


# ── the second silent zero: an ATS that was never mined ──────────────────────
# The first draft of universe.py shipped this bug. `CDX_ATS` asks for four ATSs; the
# generator produces two. Lever can NEVER be produced — jobs.lever.co/robots.txt sets
# `User-agent: CCBot` / `Disallow: /` — and workday is withheld deliberately. Both were
# invisible: the key was absent from meta.counts and for_ats returned [] forever.


def test_an_ats_that_was_never_mined_raises_instead_of_returning_empty(tmp_path):
    p = _write(tmp_path, [{"ats": "greenhouse", "slug": "axon"}])
    with pytest.raises(universe.UniverseNotQueried):
        universe.for_ats("lever", path=p)


def test_the_not_queried_error_names_what_WAS_mined(tmp_path):
    """So the reader can tell "lever is impossible" from "the generator broke"."""
    p = _write(
        tmp_path,
        [{"ats": "greenhouse", "slug": "axon"}, {"ats": "ashby", "slug": "runway-ml"}],
    )
    with pytest.raises(universe.UniverseNotQueried) as e:
        universe.for_ats("workday", path=p)
    assert "greenhouse" in str(e.value) and "ashby" in str(e.value)


def test_not_queried_is_catchable_as_universe_unavailable(tmp_path):
    """resolve.py distinguishes the two but must never miss one; the subclass guarantees a
    single `except UniverseUnavailable` still catches both."""
    assert issubclass(universe.UniverseNotQueried, universe.UniverseUnavailable)


def test_a_file_with_no_counts_map_does_not_invent_a_failure(tmp_path):
    """A hand-made or pre-`counts` file states nothing about what was mined, so reading it
    must not raise — only an explicit `counts` map that OMITS the ATS is a stated fact."""
    p = tmp_path / "nocounts.json"
    p.write_text(
        json.dumps({"meta": {"crawl": "x"}, "boards": [{"ats": "lever", "slug": "a"}]}),
        encoding="utf-8",
    )
    assert universe.for_ats("lever", path=str(p)) == [{"ats": "lever", "slug": "a"}]


# ── the drop-in shape ────────────────────────────────────────────────────────


def test_for_ats_returns_mine_shaped_entries(tmp_path):
    """Must be a drop-in for discover.mine: {'ats','slug'} and nothing surprising."""
    p = _write(tmp_path, [{"ats": "greenhouse", "slug": "axon"}])
    assert universe.for_ats("greenhouse", path=p) == [
        {"ats": "greenhouse", "slug": "axon"}
    ]


def test_for_ats_filters_to_the_requested_ats(tmp_path):
    p = _write(
        tmp_path,
        [
            {"ats": "greenhouse", "slug": "axon"},
            {"ats": "lever", "slug": "matchgroup"},
            {"ats": "ashby", "slug": "runway-ml"},
        ],
    )
    assert [b["slug"] for b in universe.for_ats("lever", path=p)] == ["matchgroup"]


def test_workday_entries_keep_their_host_and_site(tmp_path):
    """Workday's three-part key is the only thing a company NAME cannot produce, so
    dropping host/site would silently reduce it to an unusable tenant guess."""
    p = _write(
        tmp_path,
        [{"ats": "workday", "slug": "3m", "host": "wd1", "site": "Search"}],
    )
    assert universe.for_ats("workday", path=p) == [
        {"ats": "workday", "slug": "3m", "host": "wd1", "site": "Search"}
    ]


def test_a_board_with_no_slug_is_skipped(tmp_path):
    p = _write(tmp_path, [{"ats": "greenhouse", "slug": ""}])
    assert universe.for_ats("greenhouse", path=p) == []


# ── staleness is REPORTED, never silent ──────────────────────────────────────


def test_a_stale_file_says_so_in_words(tmp_path):
    old = (
        datetime.now(_ET) - timedelta(days=universe.STALE_AFTER_DAYS + 10)
    ).isoformat(timespec="seconds")
    p = _write(tmp_path, [{"ats": "greenhouse", "slug": "axon"}], generated_at=old)
    assert "STALE" in universe.describe(p)


def test_a_fresh_file_does_not_cry_wolf(tmp_path):
    p = _write(tmp_path, [{"ats": "greenhouse", "slug": "axon"}])
    assert "STALE" not in universe.describe(p)


def test_an_undated_file_admits_it_cannot_judge_staleness(tmp_path):
    """Worse than stale is not knowing. Silence here would read as fresh."""
    p = _write(tmp_path, [{"ats": "greenhouse", "slug": "a"}], meta=False)
    out = universe.describe(p)
    assert universe.age_days(p) is None
    assert "MISSING" in out


def test_describe_names_the_crawl_it_came_from(tmp_path):
    p = _write(tmp_path, [{"ats": "greenhouse", "slug": "a"}], crawl="CC-MAIN-2026-30")
    assert "CC-MAIN-2026-30" in universe.describe(p)


# ── the shipped file itself ──────────────────────────────────────────────────


def test_the_committed_universe_loads_and_holds_greenhouse_boards():
    """Guards the file, not just the loader. It is deployed like code, so a bad commit is a
    production change — and `deploy/board-universe.json` being absent means the nightly
    discovery lane is dark."""
    if not universe.DEFAULT_PATH.exists():
        pytest.skip("board-universe.json not generated in this checkout")
    boards = universe.for_ats("greenhouse")
    assert len(boards) > 500, f"suspiciously few greenhouse boards: {len(boards)}"
    assert all(b["ats"] == "greenhouse" and b["slug"] for b in boards)


def test_no_template_or_encoded_slugs_survive_generation():
    """The crawl carries template and encoding junk — `anthos%20capital`,
    `%7byour_company%7d`, `%20forbes`. Anything holding an unexpanded template or an encoded
    space never belonged to a real employer.

    Renamed 2026-08-17: this was called `..._free_of_the_retired_greenhouse_host_artifacts`
    and its docstring claimed `boards.greenhouse.io` has 0 successful fetches. Both were
    leftovers from a retracted framing — that junk is on the LIVE host, and the retired host
    carries 1,789 perfectly good slugs. The assertion never had anything to do with either.
    """
    if not universe.DEFAULT_PATH.exists():
        pytest.skip("board-universe.json not generated in this checkout")
    bad = [
        b["slug"]
        for b in universe.load()["boards"]
        if "%" in b["slug"] or "{" in b["slug"] or " " in b["slug"]
    ]
    assert not bad, f"template/encoding junk survived generation: {bad[:10]}"


# ── the junk gate (scripts/mine_universe.py) ─────────────────────────────────
# `ats_from_url` is the one slug PARSER, and an earlier draft of the generator claimed it
# was also the junk filter: "anything the one parser refuses is dropped here". It is not a
# slug VALIDATOR — measured, `ats_from_url('https://jobs.lever.co/robots.txt')` returns
# `('lever', 'robots.txt')`, and 65 of ashby's 2,821 slugs (2.3%) are percent-encoded
# (`anthos%20capital`, `%7byour_company%7d`) and passed a bare length check untouched.


def _junk():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "_mine_universe",
        Path(__file__).resolve().parent.parent / "scripts" / "mine_universe.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.junk_reason


@pytest.mark.parametrize(
    "slug",
    [
        "anthos%20capital",  # URL-encoded space — the real ashby case
        "%7byour_company%7d",  # an unexpanded template
        "{company}",
        "robots.txt",  # what the parser happily returns for a robots fetch
        "sitemap.xml",
        "favicon.ico",
        "a",  # too short to be a board
        "x" * 61,
    ],
)
def test_junk_slugs_are_rejected_at_generation(slug):
    """Junk cannot cause a FALSE resolution — this lane binds no company name — but it does
    cause a PERMANENT wasted request: discover_new writes a ledger row only for verified and
    refused boards, so a 404 is never cached and gets re-probed every night forever."""
    assert _junk()(slug) is not None, f"{slug!r} should have been rejected"


@pytest.mark.parametrize(
    "slug",
    [
        # from the ledger — name-guessed shapes
        "axon", "runway-ml", "datavant2", "archer56", "ecpcareers", "3m", "c3iot", "0x",
        # FROM THE MINED SET, and these are the ones that matter. Every slug below was
        # probed live and carries open roles; an earlier `looks-like-a-filename` rule
        # deleted all of them — 68 ashby boards, 1,291 roles.
        "checkout.com",    # 180 roles
        "roadsurfer.com",  # 135
        "rivianvw.tech",   # 114
        "kraken.com",      # 80
        "jerry.ai",        # 53
        "far.ai",          # 14
        "magic.dev",       # 10
        "qdrant.tech",
        "binance.us",
        "www.qogita.com",  # a www. prefix is worth one probe, not a silent delete
    ],
)
def test_real_board_slugs_survive_the_junk_gate(slug):
    """The gate must not become a false-drop machine.

    WHY THE LIST IS SPLIT, and it is the whole lesson of this test. The first version held
    only ledger slugs, and "verified" the gate against the 1,390 resolved slugs in
    production — 0 of which contain a dot. That check COULD NOT FAIL: every ashby row in
    the ledger arrived by name-guessing, and `name_variants` cannot emit a dot. So the test
    asserted "the gate must not become a false-drop machine" while the gate was deleting 68
    live boards.

    Ashby lets an employer use its own DOMAIN as the board slug and the AI/crypto tier does
    it constantly. Sample from the MINED set — the input the gate actually sees — not from
    the population the gate has already shaped.
    """
    assert _junk()(slug) is None, f"{slug!r} is a real live board and was rejected"
