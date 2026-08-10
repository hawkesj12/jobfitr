"""jobfitr's controlled vocabularies — the opinion layer over job_radar's fidelity.

Coverage numbers here are measured on _private/raw/harvest-2026-08-08T0926/ (21,495 rows).
They are asserted as floors, not equalities: a new source may add raw values, and the test
should fail when coverage DROPS, not when the corpus grows.
"""

from jobfitr import vocab


def test_canonical_sets_are_small_enough_to_be_a_facet():
    """The whole point. 487 raw category values is not a filter; a drawer has to fit on a
    screen and be scannable. If either set grows past this, the facet stopped being one."""
    assert len(vocab.CATEGORIES) <= 25
    assert len(vocab.SENIORITY_LEVELS) == 8
    assert "Unknown" not in vocab.CATEGORIES  # unknown is None, never a value


def test_every_mapping_targets_a_canonical_value():
    """A typo in the map would create a 23rd category nobody declared, and it would show up
    in the drawer looking legitimate."""
    for raw, canon in vocab._CATEGORY_MAP.items():
        assert canon in vocab.CATEGORIES or canon == "Unknown", f"{raw} -> {canon}"
    for raw, canon in vocab._SENIORITY_MAP.items():
        assert canon in vocab.SENIORITY_LEVELS, f"{raw} -> {canon}"


def test_the_two_engineerings_do_not_merge():
    """Decided by reading real titles, not the label. adzuna's "IT Jobs" holds iOS Software
    Engineer; its "Engineering Jobs" holds Civil Engineer. Mapping on the word "engineering"
    would collapse them and put civil engineers in a software facet."""
    assert vocab.category("IT Jobs") == "Software Engineering"
    assert vocab.category("Engineering Jobs") == "Science and Engineering"
    # smartrecruiters' bare "Engineering" is general engineering — HVAC, automotive, mechanical
    assert vocab.category("Engineering") == "Science and Engineering"
    assert vocab.category("Information Technology") == "Software Engineering"


def test_html_entities_do_not_create_phantom_values():
    """Several sources send `&amp;` literally, so the same field arrived as two values and
    both missed a map keyed on either spelling."""
    assert vocab.category("Customer Support &amp; Success") == "Customer Service"
    assert vocab.category("Customer Support & Success") == "Customer Service"
    assert vocab.category("Marketing &amp; Sales") == vocab.category("Marketing & Sales")


def test_unknown_is_none_not_a_category():
    """None means "we do not know what field this is" — a real answer. Rendering it as a
    category would put a dead option in the drawer that returns rows nobody asked for."""
    for v in ("Unknown", "Other", "", None, "Graduate Jobs", "zzz not a field"):
        assert vocab.category(v) is None, v


def test_seniority_multi_value_takes_the_LOWEST_rung():
    """A job open to "Entry-level, Mid-level" must appear for someone searching entry level.
    Taking the highest would hide junior-accessible roles from the people who need them."""
    assert vocab.seniority("Entry-level, Mid-level") == "entry"
    assert vocab.seniority("Senior, Director") == "senior"
    assert vocab.seniority("Executive, Mid-level") == "mid"


def test_seniority_spellings_collapse():
    for raw in ("senior", "Senior", "Senior Level", "Mid-Senior Level"):
        assert vocab.seniority(raw) == "senior", raw
    for raw in ("Mid Level", "Mid-level", "Midweight"):
        assert vocab.seniority(raw) == "mid", raw
    for raw in ("Not Applicable", "Any", "", None):
        assert vocab.seniority(raw) is None, raw
