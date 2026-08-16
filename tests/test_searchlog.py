"""The search log — what it records, and more importantly what it must never record.

The privacy tests here are not decoration. The product promises "no account, no
tracking" in three places a user can read (README, index.html, the app.js footer), and
this is the one file that writes a user's search to disk. A regression that quietly adds
an IP address makes the front page a lie, so it is asserted rather than trusted.
"""

from __future__ import annotations

import json

import pytest

from jobfitr import searchlog


def _kept(n=3):
    """The shape score_jobs hands to record(): (row, points, why, parts)."""
    return [
        (
            {
                "title": f"AI Engineer {i}",
                "company": f"Co {i}",
                "url": f"https://x/{i}",
            },
            100 - i * 10,
            ["because"],
            [["title", 100 - i * 10]],
        )
        for i in range(n)
    ]


def _record(**over):
    args = dict(
        titles=["AI Engineer"],
        related=["ML Engineer"],
        boosts=["rag"],
        exclude=["intern"],
        location="remote",
        remote_only=True,
        max_age_days=30,
        min_score="balanced",
        pool=32500,
        candidates=2158,
        kept=_kept(),
        degraded=False,
        elapsed_ms=1422.6,
    )
    args.update(over)
    searchlog.record(**args)


@pytest.fixture
def logfile(tmp_path, monkeypatch):
    p = tmp_path / "searches.jsonl"
    monkeypatch.setattr(searchlog, "LOG_PATH", str(p))
    return p


# ── off unless asked ─────────────────────────────────────────────────────────


def test_no_path_configured_writes_nothing(tmp_path, monkeypatch):
    """A self-hoster who never sets JOBFITR_SEARCH_LOG must never find a log file.
    Logging people's searches by default is not a defensible default for this product."""
    monkeypatch.setattr(searchlog, "LOG_PATH", "")
    _record()
    assert list(tmp_path.iterdir()) == []


def test_a_broken_log_never_breaks_a_search(tmp_path, monkeypatch):
    """The observer must not be able to take down the thing it observes. A path inside
    a nonexistent directory is the cheap stand-in for the real cases: a disk-full box,
    or a systemd ReadWritePaths that was never updated (EROFS)."""
    monkeypatch.setattr(searchlog, "LOG_PATH", str(tmp_path / "nope" / "s.jsonl"))
    _record()  # must not raise


# ── what it records ──────────────────────────────────────────────────────────


def test_one_search_writes_one_parseable_line(logfile):
    _record()
    lines = logfile.read_text().splitlines()
    assert len(lines) == 1
    d = json.loads(lines[0])
    assert d["titles"] == ["AI Engineer"]
    assert d["candidates"] == 2158 and d["delivered"] == 3
    assert d["score_max"] == 100
    assert d["ms"] == 1423, "rounded to a whole millisecond"
    assert [j["t"] for j in d["top"]] == [
        "AI Engineer 0",
        "AI Engineer 1",
        "AI Engineer 2",
    ]
    assert d["top"][0]["tier"] == 100, "the title rung, read off the card's receipt"


def test_lines_append_rather_than_overwrite(logfile):
    _record()
    _record(titles=["Nurse"])
    got = [json.loads(x)["titles"] for x in logfile.read_text().splitlines()]
    assert got == [["AI Engineer"], ["Nurse"]]


def test_a_search_that_returned_nothing_is_still_recorded(logfile):
    """The most interesting line in the file. A zero-result search is the failure this
    log exists to catch, so it must not be the one case that writes nothing."""
    _record(kept=[], candidates=0)
    d = json.loads(logfile.read_text())
    assert d["delivered"] == 0 and d["top"] == []
    assert d["score_max"] is None and d["score_p50"] is None


# ── what it must never record ────────────────────────────────────────────────


def test_record_takes_no_identifying_argument():
    """The structural guarantee, checked at the signature rather than the output: there
    is no parameter an IP, user agent, or session id could be passed through. A future
    caller cannot log one by accident — it would have to change this signature, and
    changing it fails this test."""
    import inspect

    params = set(inspect.signature(searchlog.record).parameters)
    forbidden = {
        "ip",
        "addr",
        "address",
        "remote_addr",
        "client",
        "request",
        "user_agent",
        "ua",
        "referer",
        "referrer",
        "cookie",
        "session",
        "session_id",
        "user",
        "user_id",
        "fingerprint",
        "device",
    }
    assert not (params & forbidden), (
        f"identifying parameter accepted: {params & forbidden}"
    )


def test_the_written_line_holds_no_identifier(logfile):
    _record()
    d = json.loads(logfile.read_text())
    assert set(d) == {
        "ts",
        "titles",
        "related",
        "boosts",
        "exclude",
        "location",
        "remote_only",
        "max_age_days",
        "min_score",
        "pool",
        "candidates",
        "delivered",
        "degraded",
        "ms",
        "score_max",
        "score_p50",
        "top",
    }, "a new key was added — is it something that could identify a person?"


# ── the file cannot run away ─────────────────────────────────────────────────


def test_a_line_stays_under_the_atomic_append_ceiling(logfile):
    """Both slots stay warm and can hold the file open at once. A single write() to an
    O_APPEND file is atomic only up to PIPE_BUF (4096 on Linux), so an over-long line
    could interleave and corrupt its neighbour. `top` is trimmed until it fits."""
    _record(
        titles=["Senior Staff Machine Learning Infrastructure Engineer"] * 40,
        boosts=["distributed training"] * 40,
        kept=_kept(60),
    )
    for line in logfile.read_text().splitlines():
        assert len(line.encode()) <= searchlog.LINE_CAP
        json.loads(line)


def test_the_search_inputs_survive_trimming(logfile):
    """When a line is too long the SAMPLE is dropped, never the inputs — a line without
    its top-5 still says whether the search worked; one without its titles cannot be
    read at all."""
    _record(boosts=["kubernetes orchestration at scale"] * 60, kept=_kept(40))
    d = json.loads(logfile.read_text())
    assert d["titles"] == ["AI Engineer"]
    assert len(d["top"]) < 40, "the sample gave way first"


def test_the_log_rotates_at_the_cap(logfile, monkeypatch):
    monkeypatch.setattr(searchlog, "MAX_BYTES", 400)
    for i in range(12):
        _record(titles=[f"Role {i}"])
    assert logfile.with_suffix(".jsonl.1").exists(), "one generation kept"
    assert logfile.stat().st_size < 400 * 2
    # The rotation must not corrupt either file.
    for p in (logfile, logfile.with_suffix(".jsonl.1")):
        for line in p.read_text().splitlines():
            json.loads(line)


# ── the deploy probe ─────────────────────────────────────────────────────────


def test_a_probe_is_marked_and_an_ordinary_search_is_not(logfile):
    """verify-slot.sh POSTs three searches at every slot before every flip. Unmarked,
    those are indistinguishable from real demand, and at three per deploy 'engineer',
    'nurse' and 'driver' would top the list of what people asked for — the digest would
    be measuring the deploy pipeline."""
    _record()
    _record(titles=["engineer"], probe=True)
    a, b = (json.loads(x) for x in logfile.read_text().splitlines())
    assert "probe" not in a, "absent, not false — a real search carries no marker"
    assert b["probe"] is True


# ── per-source outcomes ──────────────────────────────────────────────────────


def test_the_line_records_which_sources_answered(logfile):
    """`live._fetch_all` swallows a dead vendor by design, so before 2026-08-15 an
    exhausted quota or a missing key produced a thinner board and NOTHING else — no
    banner, no log line, no health signal. Two Louisville searches an hour apart
    differed by 130 rows and nothing on the box could say which source moved."""
    _record(
        sources={
            "adzuna": {"n": 130, "why": ""},
            "usajobs": {"n": 0, "why": "empty"},
            "google_jobs": {"n": 0, "why": "quota"},
        }
    )
    d = json.loads(logfile.read_text())
    assert d["sources"]["adzuna"]["n"] == 130
    assert d["sources"]["google_jobs"]["why"] == "quota", (
        "a source that returned nothing must say WHY — 'quota' and 'empty' are "
        "different facts and only one of them is a problem"
    )


def test_a_cached_search_records_no_sources(logfile):
    """Absent, not an empty dict. A search served from a fresh cache called nobody, and
    that is a different fact from 'every source returned nothing'."""
    _record(sources=None)
    assert "sources" not in json.loads(logfile.read_text())


def test_the_top_is_deep_enough_to_rebuild_a_judge_packet(logfile):
    """TOP_N went 5 -> 10 so a REAL search can be replayed through the judge panel the
    same way a synthetic profile is — rank_test.py grades a top-10 and a 5-row sample
    cannot fill one. The url and location are what make the listing re-readable."""
    _record(kept=_kept(12))
    d = json.loads(logfile.read_text())
    assert len(d["top"]) == 10
    row = d["top"][0]
    assert row["url"].startswith("https://") and "loc" in row
