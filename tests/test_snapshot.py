"""Snapshot cache tests."""


def test_truncating_text_never_leaves_a_section_pointing_past_it():
    """`sections[].start/end` index `text`. Truncating one without the other made
    36,270 spans point past the end of their own body across 13,447 rows (13.1%) of a
    102,799-row harvest -- 23,575 slicing to "" and 12,695 returning a partial body
    that looks complete. The engine emits ZERO out-of-range spans; every one was
    created here, and it fails silently because a truncated span is indistinguishable
    from a genuine empty section."""
    from jobfitr.snapshot import TEXT_CAP, _clean_row

    row = {
        "text": "x" * (TEXT_CAP + 5000),
        "sections": [
            {"type": "about_company", "header": "About", "start": 0, "end": 100},
            {"type": "benefits", "header": "Perks", "start": TEXT_CAP - 50,
             "end": TEXT_CAP + 400},                      # straddles the cut
            {"type": "eeo_legal", "header": "EEO", "start": TEXT_CAP + 900,
             "end": TEXT_CAP + 1200},                     # wholly past it
            {"type": None, "header": "Unlocatable"},      # no span: must survive
        ],
    }
    out = _clean_row(row)
    text, secs = out["text"], out["sections"]
    assert len(text) == TEXT_CAP

    for s in secs:
        if s.get("end") is not None:
            assert s["end"] <= len(text), f"span past the end of text: {s}"

    kinds = [s.get("type") for s in secs]
    assert "eeo_legal" not in kinds, "a wholly-past span survived"
    assert kinds.count(None) == 1, "the unlocatable section was dropped or altered"
    assert {"start", "end"} & set(secs[-1]) == set(), "an unlocatable span gained offsets"

    perks = next(s for s in secs if s.get("type") == "benefits")
    assert perks["end"] == TEXT_CAP, "a straddling span was not clamped"
    assert text[perks["start"]:perks["end"]] != "", "clamped span reads empty"

    # `text_basis` stays the engine's to set: `excerpt` means the SOURCE truncated.
    assert "text_basis" not in out or out.get("text_basis") is None
