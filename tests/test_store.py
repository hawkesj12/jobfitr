"""Phase F: the SQLite/FTS5 store. No network; a fresh tmp DB per test."""

from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest

from jobfitr import store


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = str(tmp_path / "t.db")
    # no stray jobs.json import unless a test asks for it
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    store.init(p)
    return p


def _job(url, title, text="", **kw):
    base = {
        "url": url,
        "title": title,
        "text": text,
        "company": "Acme",
        "location": "Remote",
        "posted": "2026-07-10",
        "salary": "",
        "source": "adzuna",
    }
    base.update(kw)
    return base


# ── normalize + tags ──────────────────────────────────────────────────────────
def test_normalize_derives_tags():
    r = store.normalize_job(
        _job(
            "u",
            "Senior Data Engineer",
            category="IT Jobs",
            seniority="Senior Level",
            employment_type="full_time",
            salary="$120,000–$140,000",
            location="Austin, TX",
        )
    )
    assert r["category"] == "Software Engineering"  # mapped, not the raw string
    assert r["employment_type"] == "full_time"
    assert r["seniority"] == "senior"  # the SOURCE said so
    assert r["remote"] is None  # "Austin, TX" states no arrangement
    assert r["salary_band"] == "120-180k"
    assert (
        store.normalize_job(_job("u2", "Nurse", location="Remote"))["remote"]
        == "remote"
    )


def test_normalize_ignores_the_deprecated_department_alias():
    """`department` is not a category. On greenhouse/ashby/lever it is byte-identical
    to `team` — the employer's own org-unit name — for all 16,235 such rows in the
    capture. Consuming it looked like +17.7% facet coverage and mostly misfiled: 895
    rows of "Senior Software Engineer, Backend" under Science and Engineering, because
    their department is called "Engineering"."""
    r = store.normalize_job(
        _job("d", "Senior Software Engineer", department="Engineering")
    )
    assert r["category"] is None


def test_normalize_never_invents_a_seniority():
    """23,781 of 39,597 live rows used to read "mid" — a Level chip on jobs whose
    posting never said any such thing. Unknown is NULL now, and NULL is a real answer."""
    r = store.normalize_job(_job("s", "Data Engineer"))
    assert r["seniority"] is None
    assert r["seniority_basis"] is None
    # A title that the OLD regex would have called "lead" is still None — title parsing
    # belongs to the engine now, which reports what it found under seniority_basis.
    assert store.normalize_job(_job("s2", "Principal Architect"))["seniority"] is None
    stated = store.normalize_job(
        _job("s3", "Analyst", seniority="Entry Level", seniority_basis="stated")
    )
    assert (stated["seniority"], stated["seniority_basis"]) == ("entry", "stated")


def test_a_basis_never_outlives_its_value():
    """60 rows arrive as seniority "Not Applicable"/"Any" under basis 'stated'. The
    vocabulary refuses to map those — correctly — which used to leave the basis column
    asserting that a source stated a level we do not have. A basis with nothing behind
    it is worse than no basis: it is the field whose entire job is to say how far to
    trust the value."""
    for junk in ("Not Applicable", "Any", "n/a"):
        r = store.normalize_job(
            _job("j", "Analyst", seniority=junk, seniority_basis="stated")
        )
        assert r["seniority"] is None and r["seniority_basis"] is None, junk
    # the same invariant on the other pair, which gets it structurally from _remote()
    r = store.normalize_job(_job("j2", "Analyst", location="Dayton, OH"))
    assert r["remote"] is None and r["remote_basis"] is None


def test_normalize_remote_from_real_adzuna_and_board_shapes():
    # This replaced test_normalize_strips_adzuna_remote_artifact, which asserted that
    # a " (Remote)" suffix was stripped off Adzuna locations. job_radar used to append
    # that to EVERY Adzuna row; job-radar 5ab74df ("stop mislabeling every Adzuna job
    # remote") fixed it upstream, so the strip became dead code guarding a shape that
    # can no longer arrive — measured: 0 of 7,308 adzuna rows in the frozen corpus end
    # with " (Remote)". The invariants below are the ones that actually mattered, now
    # asserted against locations Adzuna really sends.
    r = store.normalize_job(
        _job("u", "Grocery Store Manager", location="Wahpeton, ND", source="adzuna")
    )
    assert r["location"] == "Wahpeton, ND"  # passed through verbatim, nothing stripped
    # NOT "onsite" — None. remote_posting() found no evidence of remote, and it has
    # never looked for evidence of onsite. What matters is that it is not "remote".
    assert r["remote"] is None
    # a genuinely remote-titled Adzuna job still reads remote (title signal)
    assert (
        store.normalize_job(
            _job("u2", "Remote Customer Advocate", location="Austin", source="adzuna")
        )["remote"]
        == "remote"
    )
    # ashby/smartrecruiters/workable and the free remote boards DO still append
    # " (Remote)", and there it is real signal — 2,758 rows carry it. Keep it.
    assert (
        store.normalize_job(
            _job("u3", "Designer", location="Anywhere (Remote)", source="remotive")
        )["remote"]
        == "remote"
    )
    assert (
        store.normalize_job(
            _job("u4", "Platform Engineer", location="Berlin (Remote)", source="ashby")
        )["remote"]
        == "remote"
    )


def test_normalize_remote_from_body():
    # The keyed sources (Adzuna/USAJOBS) carry no remote flag at all, so a genuinely
    # remote role reads onsite from title+location alone — "Austin, TX" says nothing.
    # The body scan recovers it; this is the fix for empty remote searches.
    # Locations here deliberately carry NO " (Remote)" suffix: that is what Adzuna
    # really sends, and a suffix would short-circuit remote_posting() on the head and
    # never exercise the body path this test exists to cover.
    r = store.normalize_job(
        _job(
            "b1",
            "Front End Developer",
            location="Austin, TX",
            text="We are hiring a front end developer. This is a fully remote position open to any US state.",
        )
    )
    assert r["remote"] == "remote"
    # explicit negation in the body keeps an on-site role onsite
    assert (
        store.normalize_job(
            _job(
                "b2",
                "Front End Developer",
                location="Austin, TX",
                text="On-site only. This is not a remote position; you must work from our Austin office.",
            )
        )["remote"]
        is None
    )
    # incidental "remote" in prose must NOT flip an on-site job (no false positive).
    # Plain "Dayton, OH" on purpose — this asserts the BODY guard, and a " (Remote)"
    # suffix would satisfy remote_posting() on the head and never reach it.
    assert (
        store.normalize_job(
            _job(
                "b3",
                "Systems Engineer",
                location="Dayton, OH",
                text="You will administer remote servers and support remote teams across our data centers.",
            )
        )["remote"]
        is None
    )


def test_normalize_remote_prefers_the_engines_stated_type():
    """The engine looked at a real field; the prose scan is the fallback, not the peer.
    This is what makes hybrid visible at all — remote_posting() would call these two
    the same thing, and 1,813 stated-hybrid rows used to render as Remote."""
    for state in ("remote", "hybrid", "onsite"):
        r = store.normalize_job(
            _job("h", "Engineer", remote_type=state, remote_basis="stated")
        )
        assert (r["remote"], r["remote_basis"]) == (state, "stated")
    # A stated ONSITE wins even when the prose is full of remote-sounding phrases —
    # that is the whole reason to prefer a real field over a phrase detector.
    r = store.normalize_job(
        _job(
            "h2",
            "Engineer",
            location="Remote",
            text="fully remote position, work from anywhere",
            remote_type="onsite",
            remote_basis="stated",
        )
    )
    assert r["remote"] == "onsite"
    # The derived fallback records how weak it is.
    r = store.normalize_job(_job("h3", "Engineer", location="Remote"))
    assert (r["remote"], r["remote_basis"]) == ("remote", "derived")


# ── upsert dedup + refresh ────────────────────────────────────────────────────
def test_upsert_dedup_and_refresh(db):
    assert store.upsert_jobs([_job("u1", "Accountant", salary="$50k")], path=db) == 1
    assert store.pool_size(db) == 1
    # re-upsert same url → still 1 row, salary refreshed
    store.upsert_jobs([_job("u1", "Accountant", salary="$70k")], path=db)
    assert store.pool_size(db) == 1
    cands = store.bm25_candidates(["accountant"], path=db)
    assert cands and cands[0]["salary"] == "$70k"


# ── BM25 differentiates a one-word query (the whole point) ────────────────────
def test_bm25_ranks_and_differentiates(db):
    store.upsert_jobs(
        [
            _job(
                "a1",
                "Senior Accountant",
                "audit gaap tax accountant accountant reporting",
            ),
            _job("a2", "Junior Accountant", "entry level accountant bookkeeping"),
            _job(
                "a3",
                "Accounting Manager",
                "team budgeting oversight some accountant duties",
            ),
            _job("m1", "Marketing Manager", "brand campaigns seo growth"),
            _job("e1", "Software Engineer", "python backend apis"),
            _job("n1", "Registered Nurse", "patient care icu clinical"),
            _job("s1", "Sales Representative", "quota pipeline crm outbound"),
            _job("d1", "Truck Driver", "cdl routes logistics deliveries"),
            _job("t1", "Teacher", "classroom curriculum students k-12"),
            _job("g1", "Designer", "figma brand ux visual"),
            _job("w1", "Warehouse Associate", "picking packing forklift"),
            _job("p1", "Product Manager", "roadmap discovery stakeholders"),
        ],
        path=db,
    )
    cands = store.bm25_candidates(["accountant"], path=db)
    urls = [c["url"] for c in cands]
    assert "m1" not in urls and "e1" not in urls  # non-matching excluded
    assert set(urls) == {"a1", "a2", "a3"}
    scores = [c["bm25"] for c in cands]
    assert scores == sorted(scores, reverse=True)  # ordered best-first
    assert len(set(round(s, 4) for s in scores)) > 1  # NOT tied (flat sum would tie)


# ── TTL freshness clock ───────────────────────────────────────────────────────
def test_ttl_fresh_then_stale(db):
    k = store.search_key(["grocery store manager"], "remote")
    assert store.search_fresh(k, path=db) is False  # never fetched
    store.mark_fetched(k, path=db)
    assert store.search_fresh(k, ttl=3600, path=db) is True  # just now
    assert store.search_fresh(k, ttl=0, path=db) is False  # 0s TTL → stale


# ── eviction: unseen>14d, posted>60d, LRU cap ────────────────────────────────
def test_evict_unseen_and_posted_and_cap(db, monkeypatch):
    now = time.time()
    store.upsert_jobs([_job("fresh", "Fresh Job", posted="2026-07-13")], path=db)
    store.upsert_jobs([_job("oldposted", "Old Posted", posted="2020-01-01")], path=db)
    store.upsert_jobs([_job("unseen", "Unseen Job", posted="2026-07-13")], path=db)
    # age "unseen"'s last_seen to 20 days ago via a direct write
    with store._conn(db) as c:
        c.execute("UPDATE jobs SET last_seen=? WHERE url='unseen'", (now - 20 * 86400,))
    removed = store.evict(now=now, path=db)
    remaining = {r["url"] for r in store.bm25_candidates(["job"], path=db)}
    assert removed >= 2
    assert "oldposted" not in remaining and "unseen" not in remaining
    assert "fresh" in remaining


def test_evict_lru_cap(db, monkeypatch):
    monkeypatch.setattr(store, "MAX_ROWS", 2)
    now = time.time()
    for i in range(5):
        store.upsert_jobs([_job(f"j{i}", "Widget Maker", posted="2026-07-13")], path=db)
        with store._conn(db) as c:
            c.execute("UPDATE jobs SET last_seen=? WHERE url=?", (now - i, f"j{i}"))
    store.evict(now=now, path=db)
    assert store.pool_size(db) == 2  # LRU capped


# ── facets ────────────────────────────────────────────────────────────────────
def test_facet_counts():
    rows = [
        {
            "category": "IT Jobs",
            "employment_type": "full_time",
            "remote": "remote",
            "seniority": "senior",
            "salary_band": "80-120k",
        },
        {
            "category": "IT Jobs",
            "employment_type": "full_time",
            "remote": "onsite",
            "seniority": "mid",
            "salary_band": "",
        },
        {
            "category": "Healthcare & Nursing Jobs",
            "employment_type": "part_time",
            "remote": "onsite",
            "seniority": "mid",
            "salary_band": "50-80k",
        },
    ]
    f = store.facet_counts(rows)
    assert f["category"]["IT Jobs"] == 2
    assert f["employment_type"]["full_time"] == 2
    assert f["remote"] == {"remote": 1, "onsite": 2}
    assert "" not in f["salary_band"]  # empty bands not counted


# ── jobs.json import: seeded on init, re-imported when the harvest rewrites it ──
def _write_snapshot(path, jobs, mtime=None):
    path.write_text(json.dumps({"meta": {"count": len(jobs)}, "jobs": jobs}))
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_imports_jobs_json_on_init(tmp_path, monkeypatch):
    jj = tmp_path / "jobs.json"
    _write_snapshot(jj, [_job("i1", "Imported One"), _job("i2", "Imported Two")])
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(jj))
    p = str(tmp_path / "imp.db")
    store.init(p)
    assert store.pool_size(p) == 2
    # init again with an UNCHANGED snapshot → no re-import work
    assert store.sync_snapshot(p) == 0
    assert store.pool_size(p) == 2


def test_resyncs_when_the_harvest_rewrites_the_snapshot(tmp_path, monkeypatch):
    """The live bug: a slot built yesterday served that pool forever, because the old
    import-once rule only fired on an EMPTY table. A newer jobs.json must flow in."""
    jj = tmp_path / "jobs.json"
    _write_snapshot(jj, [_job("i1", "Imported One")], mtime=1_000_000)
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(jj))
    p = str(tmp_path / "resync.db")
    store.init(p)
    assert store.pool_size(p) == 1

    # the nightly harvest rewrites it with more jobs, a newer mtime
    _write_snapshot(
        jj, [_job("i1", "Imported One"), _job("i2", "Fresh Two")], mtime=2_000_000
    )
    assert store.sync_snapshot(p) == 2  # imported (dedup by url keeps i1 single)
    assert store.pool_size(p) == 2
    # ...and it's idempotent at the same mtime
    assert store.sync_snapshot(p) == 0
    assert store.pool_size(p) == 2


def test_sync_records_mtime_only_after_a_successful_import(tmp_path, monkeypatch):
    """A crash mid-import must retry next time, not mark the snapshot as ingested."""
    jj = tmp_path / "jobs.json"
    _write_snapshot(jj, [_job("i1", "One")], mtime=1_000_000)
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(jj))
    p = str(tmp_path / "boom.db")
    with store._conn(p) as c:
        c.executescript(store._SCHEMA)

    def _boom(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "upsert_jobs", _boom)
    with pytest.raises(sqlite3.OperationalError):
        store.sync_snapshot(p)
    assert store.snapshot_imported_at(p) is None  # not recorded

    monkeypatch.undo()
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(jj))
    assert store.sync_snapshot(p) == 1  # retried and succeeded
    assert store.snapshot_imported_at(p) is not None


def test_missing_snapshot_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "absent.json"))
    p = str(tmp_path / "none.db")
    store.init(p)
    assert store.pool_size(p) == 0
    assert store.snapshot_imported_at(p) is None


# ── the company -> ATS resolution ledger ──────────────────────────────────────
def _seed_companies(db, pairs):
    """pairs = [(company, n_jobs)] — job count drives resolution priority."""
    store.upsert_jobs(
        [
            _job(f"{name}-{i}", "Engineer", company=name)
            for name, n in pairs
            for i in range(n)
        ],
        path=db,
    )


def test_unresolved_lists_never_checked_companies_busiest_first(db):
    _seed_companies(db, [("Small Co", 1), ("Big Co", 5), ("Mid Co", 3)])
    assert store.unresolved_companies(path=db) == ["Big Co", "Mid Co", "Small Co"]


def test_a_cached_negative_stops_the_company_being_reprobed(db):
    """The whole economics of the ledger: 'checked, found nothing' is an ANSWER.
    Without it every run re-probes ~3k dead-end employers forever."""
    _seed_companies(db, [("Veterans Health Administration", 4)])
    assert store.unresolved_companies(path=db) == ["Veterans Health Administration"]
    store.record_resolution("Veterans Health Administration", None, path=db)
    assert store.unresolved_companies(path=db) == []


def test_a_negative_expires_so_a_late_adopter_is_found(db, monkeypatch):
    """A company with no board today may adopt one next quarter — but slowly, so
    the retry window is long."""
    _seed_companies(db, [("Late Adopter", 2)])
    store.record_resolution("Late Adopter", None, path=db)
    assert store.unresolved_companies(path=db) == []
    with store._conn(db) as c:  # age the check past the retry window
        stale = time.time() - (store.UNRESOLVED_RETRY_DAYS + 1) * 86400
        c.execute("UPDATE companies SET checked_at=?", (stale,))
    assert store.unresolved_companies(path=db) == ["Late Adopter"]


def test_a_resolved_company_is_never_reprobed(db):
    _seed_companies(db, [("Stripe", 2)])
    store.record_resolution(
        "Stripe",
        {"ats": "greenhouse", "slug": "stripe", "roles": 516},
        variant="stripe",
        path=db,
    )
    assert store.unresolved_companies(path=db) == []
    got = store.resolved_companies(path=db)
    assert got[0]["slug"] == "stripe" and got[0]["roles"] == 516


def test_resolution_keeps_the_evidence_that_proved_it(db):
    """A wrong slug is sticky and silent, so the variant that won is recorded —
    that is what makes a bad resolution findable and reversible."""
    _seed_companies(db, [("LevelTen Energy", 1)])
    store.record_resolution(
        "LevelTen Energy",
        {"ats": "greenhouse", "slug": "leveltenenergy", "roles": 5},
        variant="leveltenenergy",
        path=db,
    )
    assert store.resolved_companies(path=db)[0]["matched_variant"] == "leveltenenergy"


def test_workday_triple_round_trips(db):
    _seed_companies(db, [("Barry-Wehmiller", 1)])
    store.record_resolution(
        "Barry-Wehmiller",
        {
            "ats": "workday",
            "slug": "barrywehmiller",
            "host": "wd1",
            "site": "BWCareers",
            "roles": 455,
        },
        path=db,
    )
    e = store.resolved_companies(path=db)[0]
    assert (e["host"], e["site"]) == ("wd1", "BWCareers")


def test_reresolution_updates_in_place_and_counts_attempts(db):
    _seed_companies(db, [("Acme", 1)])
    store.record_resolution("Acme", None, path=db)
    store.record_resolution(
        "Acme", {"ats": "ashby", "slug": "acme", "roles": 9}, path=db
    )
    assert len(store.resolved_companies(path=db)) == 1
    with store._conn(db) as c:
        assert c.execute("SELECT attempts FROM companies").fetchone()[0] == 2


def test_resolution_stats_counts_each_bucket(db):
    _seed_companies(db, [("A", 1), ("B", 1), ("C", 1)])
    store.record_resolution("A", {"ats": "ashby", "slug": "a", "roles": 1}, path=db)
    store.record_resolution("B", None, path=db)
    s = store.resolution_stats(path=db)
    assert (s["resolved"], s["unresolved"], s["never_checked"]) == (1, 1, 1)


# ── normalized key: one employer, one row, one answer ────────────────────────
def test_name_variants_collapse_to_one_company(db):
    """MEASURED on the live store: 43 collision groups in 3,162 strings. Keying on the
    raw string gave 'Westhab Inc.' / 'Westhab' / 'Westhab, Inc.' three rows, three
    probe budgets, and three independent answers for one employer."""
    _seed_companies(db, [("Westhab Inc.", 3), ("Westhab", 2), ("Westhab, Inc.", 1)])
    pending = store.unresolved_companies(path=db)
    assert len(pending) == 1, f"expected one company to probe, got {pending}"

    store.record_resolution(
        pending[0], {"ats": "greenhouse", "slug": "westhab", "roles": 4}, path=db
    )
    # every spelling is now answered — none comes back for another probe
    assert store.unresolved_companies(path=db) == []
    assert len(store.resolved_companies(path=db)) == 1


def test_case_only_differences_collapse(db):
    _seed_companies(db, [("Celsius", 2), ("CELSIUS", 1)])
    assert len(store.unresolved_companies(path=db)) == 1


def test_resolution_stats_counts_normalized_employers(db):
    _seed_companies(db, [("Westhab Inc.", 1), ("Westhab", 1), ("Acme", 1)])
    assert store.resolution_stats(path=db)["companies_in_store"] == 2


def test_raw_name_is_preserved_for_display(db):
    _seed_companies(db, [("LevelTen Energy, Inc.", 1)])
    store.record_resolution(
        "LevelTen Energy, Inc.",
        {"ats": "greenhouse", "slug": "leveltenenergy", "roles": 5},
        path=db,
    )
    assert store.resolved_companies(path=db)[0]["name"] == "LevelTen Energy, Inc."


# ── dead: a refusal is not a maybe ───────────────────────────────────────────
def test_a_refused_board_is_dead_and_never_retried(db, monkeypatch):
    """A 403 tenant EXISTS and has said no. Unlike 'unresolved' it must not come back
    when the retry window lapses — asking again nightly is futile and impolite."""
    _seed_companies(db, [("Fortress Corp", 2)])
    store.record_resolution("Fortress Corp", None, status="dead", path=db)
    assert store.unresolved_companies(path=db) == []

    with store._conn(db) as c:  # age it far past the unresolved retry window
        stale = time.time() - (store.UNRESOLVED_RETRY_DAYS + 400) * 86400
        c.execute("UPDATE companies SET checked_at=?", (stale,))
    assert store.unresolved_companies(path=db) == [], "dead must stay dead"
    assert store.resolution_stats(path=db)["dead"] == 1


def test_unresolved_still_expires_but_dead_does_not(db):
    _seed_companies(db, [("Late Adopter", 2), ("Refuser", 1)])
    store.record_resolution("Late Adopter", None, path=db)
    store.record_resolution("Refuser", None, status="dead", path=db)
    with store._conn(db) as c:
        stale = time.time() - (store.UNRESOLVED_RETRY_DAYS + 1) * 86400
        c.execute("UPDATE companies SET checked_at=?", (stale,))
    assert store.unresolved_companies(path=db) == ["Late Adopter"]


# ── seeding + the ledger->harvest wiring ─────────────────────────────────────
def test_seed_imports_the_curated_watchlist_as_resolved(db, tmp_path):
    """The 94 curated entries were each live-probed before being committed. Seeding
    them stops us spending requests to re-learn a fact we already trust, and stops
    them crowding the discovery queue."""
    wl = tmp_path / "watchlist.json"
    wl.write_text(
        json.dumps(
            {
                "companies": [
                    {"name": "Anthropic", "ats": "greenhouse", "slug": "anthropic"},
                    {
                        "name": "Barry-Wehmiller",
                        "ats": "workday",
                        "slug": "barrywehmiller",
                        "host": "wd1",
                        "site": "BWCareers",
                    },
                    {"name": "Broken", "ats": "greenhouse"},  # no slug -> skipped
                ]
            }
        )
    )
    assert store.seed_companies_from_watchlist(wl, path=db) == 2
    got = {c["name"]: c for c in store.resolved_companies(path=db)}
    assert set(got) == {"Anthropic", "Barry-Wehmiller"}
    assert (got["Barry-Wehmiller"]["host"], got["Barry-Wehmiller"]["site"]) == (
        "wd1",
        "BWCareers",
    )
    assert got["Anthropic"]["matched_variant"] == "curated"


def test_seeding_is_idempotent(db, tmp_path):
    wl = tmp_path / "w.json"
    wl.write_text(
        json.dumps({"companies": [{"name": "Acme", "ats": "ashby", "slug": "acme"}]})
    )
    store.seed_companies_from_watchlist(wl, path=db)
    store.seed_companies_from_watchlist(wl, path=db)
    assert len(store.resolved_companies(path=db)) == 1


def test_a_seeded_company_is_never_queued_for_discovery(db, tmp_path):
    _seed_companies(db, [("Anthropic", 5), ("Unknown Co", 3)])
    wl = tmp_path / "w.json"
    wl.write_text(
        json.dumps(
            {
                "companies": [
                    {"name": "Anthropic", "ats": "greenhouse", "slug": "anthropic"}
                ]
            }
        )
    )
    store.seed_companies_from_watchlist(wl, path=db)
    assert store.unresolved_companies(path=db) == ["Unknown Co"]


def test_missing_or_corrupt_watchlist_seeds_nothing(db, tmp_path):
    assert store.seed_companies_from_watchlist(tmp_path / "absent.json", path=db) == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert store.seed_companies_from_watchlist(bad, path=db) == 0


def test_cdx_failure_is_reported_not_swallowed(db, monkeypatch):
    """A discovery run that mined nothing because Common Crawl REFUSED us is a
    completely different event from one that found nothing new. Reporting a silent
    zero for both is the same failure shape as the frozen pool."""
    from jobfitr import resolve as _resolve

    def boom(ats, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(_resolve.discover, "mine", boom)
    monkeypatch.setattr(_resolve.store, "DB_PATH", db)
    out = _resolve.discover_new(ats_list=["greenhouse", "workday"], path=db)
    assert out["mined"] == 0
    assert len(out["mine_errors"]) == 2, "every failing pattern must be named"
    assert "greenhouse" in out["mine_errors"][0]


# ── apply-URL evidence: the ownership authority that reaches Ashby/Lever ──────
def test_board_evidence_reads_ownership_out_of_stored_urls(db):
    store.upsert_jobs(
        [
            _job(
                "https://jobs.ashbyhq.com/runway-ml/abc", "Engineer", company="Runway"
            ),
            _job(
                "https://boards.greenhouse.io/stripe/jobs/1",
                "Engineer",
                company="Stripe",
            ),
            _job("https://adzuna.com/land/ad/123", "Engineer", company="Some Agency"),
        ],
        path=db,
    )
    ev = store.board_evidence(path=db)
    assert ev[store.norm_company("Runway")] == {("ashby", "runway-ml")}
    assert ev[store.norm_company("Stripe")] == {("greenhouse", "stripe")}
    assert store.norm_company("Some Agency") not in ev  # aggregator link carries none


def test_audit_flags_a_resolution_its_own_links_contradict(db):
    """The real one: 'Runway' resolved to ashby/runway (4 roles, an FP&A startup)
    while its own postings link to ashby/runway-ml (41 roles, the AI video company).
    Ashby exposes no owner name, so no other check can see this."""
    store.upsert_jobs(
        [_job("https://jobs.ashbyhq.com/runway-ml/abc", "Engineer", company="Runway")],
        path=db,
    )
    store.record_resolution(
        "Runway",
        {"ats": "ashby", "slug": "runway", "roles": 4},
        variant="runway",
        path=db,
    )
    a = store.audit_resolutions(path=db)
    assert a["checked"] == 1 and a["agree"] == 0
    assert a["disagree"][0]["slug"] == "runway"
    assert ("ashby", "runway-ml") in a["disagree"][0]["url_says"]


def test_audit_passes_a_resolution_the_links_confirm(db):
    store.upsert_jobs(
        [_job("https://boards.greenhouse.io/stripe/jobs/1", "Eng", company="Stripe")],
        path=db,
    )
    store.record_resolution(
        "Stripe", {"ats": "greenhouse", "slug": "stripe", "roles": 9}, path=db
    )
    a = store.audit_resolutions(path=db)
    assert (a["agree"], a["disagree"]) == (1, [])


def test_audit_ignores_companies_with_no_url_evidence(db):
    """No evidence is not evidence of wrongness — most companies arrive via
    aggregators whose links point at themselves."""
    store.upsert_jobs(
        [_job("https://adzuna.com/x", "Eng", company="Opaque Co")], path=db
    )
    store.record_resolution(
        "Opaque Co", {"ats": "ashby", "slug": "opaque", "roles": 3}, path=db
    )
    assert store.audit_resolutions(path=db)["checked"] == 0


def test_a_company_may_legitimately_own_several_boards(db):
    store.upsert_jobs(
        [
            _job("https://jobs.ashbyhq.com/acme/1", "Eng", company="Acme"),
            _job("https://boards.greenhouse.io/acme/jobs/2", "Eng", company="Acme"),
        ],
        path=db,
    )
    store.record_resolution(
        "Acme", {"ats": "ashby", "slug": "acme", "roles": 2}, path=db
    )
    assert store.audit_resolutions(path=db)["agree"] == 1


def test_quarantine_retracts_but_keeps_the_evidence(db):
    """Marked, not deleted — the wrong slug and the variant that produced it stay
    legible, and the same bad guess is not simply remade tomorrow."""
    store.upsert_jobs([_job("u1", "Eng", company="Runway")], path=db)
    store.record_resolution(
        "Runway", {"ats": "ashby", "slug": "runway", "roles": 4}, path=db
    )
    store.quarantine("Runway", reason="url-says:runway-ml", path=db)
    assert store.resolved_companies(path=db) == []
    assert store.unresolved_companies(path=db) == []  # terminal, not re-probed
    with store._conn(db) as c:
        row = c.execute("SELECT status,slug,matched_variant FROM companies").fetchone()
    assert row["status"] == "quarantined" and row["slug"] == "runway"
    assert "runway-ml" in row["matched_variant"]


# ── discover_new must not collide with name-resolved companies ───────────────
# REGRESSION (panel blocker 1): board slugs and company names shared one name_key
# namespace, so a CDX-discovered board silently overwrote a correct resolution, and a
# refused board could mark a resolved company permanently `dead`. Both reproduced
# against a temp store during the panel review; neither had any test coverage.
def _mine_ashby(entry):
    from jobfitr import resolve as _r

    _r.discover.mine = lambda ats, **kw: [entry] if ats == entry["ats"] else []
    return _r


def test_discovered_board_does_not_clobber_a_name_resolution(db, monkeypatch):
    from jobfitr import resolve

    store.record_resolution(
        "Ramp", {"ats": "lever", "slug": "ramp", "roles": 88}, variant="ramp", path=db
    )
    monkeypatch.setattr(
        resolve.discover,
        "mine",
        lambda ats, **kw: [{"ats": "ashby", "slug": "ramp"}] if ats == "ashby" else [],
    )
    monkeypatch.setattr(
        resolve.discover,
        "probe",
        lambda c, outcomes=None, **kw: [{**x, "roles": 3, "outcome": "ok"} for x in c],
    )
    resolve.discover_new(ats_list=["ashby"], path=db)

    # the correct 88-role Lever binding survives, under its own name key...
    with store._conn(db) as cx:
        ramp = cx.execute("SELECT * FROM companies WHERE name_key='ramp'").fetchone()
    assert (ramp["ats"], ramp["roles"], ramp["name"]) == ("lever", 88, "Ramp")
    # ...and the unrelated discovered board lands under a namespaced key
    with store._conn(db) as cx:
        board = cx.execute(
            "SELECT * FROM companies WHERE name_key='board:ashby:ramp'"
        ).fetchone()
    assert board is not None and board["ats"] == "ashby"


def test_a_refused_discovered_board_cannot_kill_a_resolved_company(db, monkeypatch):
    from jobfitr import resolve

    store.record_resolution(
        "Acme", {"ats": "lever", "slug": "acme", "roles": 42}, variant="acme", path=db
    )
    monkeypatch.setattr(
        resolve.discover,
        "mine",
        lambda ats, **kw: (
            [{"ats": "greenhouse", "slug": "acme"}] if ats == "greenhouse" else []
        ),
    )
    monkeypatch.setattr(
        resolve.discover,
        "probe",
        lambda c, outcomes=None, **kw: (
            (
                outcomes.extend({**x, "outcome": "refused"} for x in c)
                if outcomes is not None
                else None
            )
            or []
        ),
    )
    resolve.discover_new(ats_list=["greenhouse"], path=db)

    assert [c["name"] for c in store.resolved_companies(path=db)] == ["Acme"]
    assert store.unresolved_companies(
        path=db
    ) == [] or "Acme" not in store.unresolved_companies(path=db)


def test_workday_boards_get_distinct_namespaced_keys():
    from jobfitr.resolve import board_key

    assert (
        board_key({"ats": "greenhouse", "slug": "stripe"}) == "board:greenhouse:stripe"
    )
    # Ace Hardware's several Workday sites must not collapse to one key
    a = board_key({"ats": "workday", "slug": "acehardware", "site": "External"})
    b = board_key({"ats": "workday", "slug": "acehardware", "site": "ARG_External"})
    assert a != b and a == "board:workday:acehardware/External"


def test_record_resolution_guard_blocks_a_resolved_to_dead_downgrade(db):
    store.record_resolution(
        "Keeper", {"ats": "lever", "slug": "keeper", "roles": 7}, path=db
    )
    store.record_resolution("Keeper", None, status="dead", path=db)  # try to bury it
    got = store.resolved_companies(path=db)
    assert [c["name"] for c in got] == ["Keeper"] and got[0]["roles"] == 7


def test_record_resolution_key_override_is_verbatim(db):
    store.record_resolution(
        "Whatever Display",
        {"ats": "ashby", "slug": "x", "roles": 1},
        key="board:ashby:x",
        path=db,
    )
    with store._conn(db) as cx:
        row = cx.execute("SELECT name_key, name FROM companies").fetchone()
    assert row["name_key"] == "board:ashby:x"  # NOT norm_company("Whatever Display")
    assert row["name"] == "Whatever Display"


# ── M1: a throttled/errored probe must not become a 90-day negative ──────────
def _stub_depth(monkeypatch, fn):
    from job_radar import sources

    for a in list(sources.DEPTH_ALL):
        monkeypatch.setitem(sources.DEPTH_ALL, a, fn)


def test_a_throttled_run_is_not_cached_as_a_negative(db, monkeypatch):
    """REGRESSION (panel M1): probe() marks a 429 `throttled` (retryable) on purpose,
    but resolve_batch wrote any un-found name as `unresolved` — a 90-day negative. One
    rate-limited night (which the sweep "reliably trips" at volume) froze those
    companies out of discovery for a quarter."""
    import urllib.error

    from jobfitr import resolve

    _seed_companies(db, [("Alpha Inc", 2), ("Beta Co", 1)])

    def throttled(slug, **kw):
        raise urllib.error.HTTPError("u", 429, "slow down", None, None)

    _stub_depth(monkeypatch, throttled)
    r = resolve.resolve_batch(limit=10, workers=2, path=db)
    assert r["deferred"] == 2 and r["unresolved"] == 0
    # both are still queued for the next run, not cached out
    assert set(store.unresolved_companies(path=db)) == {"Alpha Inc", "Beta Co"}


def test_a_definitive_miss_is_still_cached(db, monkeypatch):
    """The inverse must be unchanged: a real 404 means no board, and that answer is
    worth caching so we don't re-probe a genuine dead end every night."""
    import urllib.error

    from jobfitr import resolve

    _seed_companies(db, [("Gamma LLC", 1)])

    def missing(slug, **kw):
        raise urllib.error.HTTPError("u", 404, "no board", None, None)

    _stub_depth(monkeypatch, missing)
    r = resolve.resolve_batch(limit=10, workers=2, path=db)
    assert r["unresolved"] == 1 and r["deferred"] == 0
    assert store.unresolved_companies(path=db) == []  # cached, not re-queued now


# ── M3: the live-fetch tally persists across a "restart" ─────────────────────
def test_live_fetch_count_persists_and_rolls_over(db, monkeypatch):
    """REGRESSION (panel M3): the counter was in-process, so a restart/crash zeroed
    it and defeated the daily ceiling. It now lives in the store."""
    assert store.live_fetch_count(path=db) == 0
    for _ in range(3):
        store.note_live_fetch(path=db)
    assert store.live_fetch_count(path=db) == 3
    # a simulated restart is just a fresh read of the same store — tally survives
    assert store.live_fetch_count(path=db) == 3

    # a new day reads as 0 without any sweep
    import jobfitr.store as s

    real = s.datetime

    class _Tomorrow(real):
        @classmethod
        def now(cls, tz=None):
            return real(2099, 1, 1, tzinfo=tz)

    monkeypatch.setattr(s, "datetime", _Tomorrow)
    assert store.live_fetch_count(path=db) == 0


# ── US-only intake ────────────────────────────────────────────────────────────
def test_us_only_drops_remote_within_another_country():
    """This test used to assert the OPPOSITE — that a remote tag exempted a row from the
    country test entirely. That exemption kept 455 rows stating a foreign country
    outright: 'Enterprise Sales Director - Canada - Remote', 'Sales Director, DACH -
    Munich (Remote)'. Remote within another country is a job in that country."""
    assert store.servable_in_us({"country": "DE", "location": "Munich (Remote)"}) is False
    assert store.servable_in_us({"country": "CA", "location": "Canada - Remote"}) is False


def test_us_only_still_keeps_a_placeless_remote_job():
    """The reasoning the exemption was built on is still honoured — it just needs no
    special case. A genuinely location-independent posting has no country, and a blank
    country passes the country test on its own. 7,277 rows are in that bucket."""
    assert store.servable_in_us({"country": "", "location": "Remote"}) is True
    assert store.servable_in_us({"location": "Anywhere"}) is True


def test_us_only_drops_a_foreign_currency_salary():
    """The second signal, and the one that has teeth — 188 rows, 70 of which state no
    country at all and are caught by nothing else. Spot-checked, every one is
    'Canada (Remote)', 'Philippines (Remote)', 'Spain (Remote)'. Nobody quotes a US
    salary in zloty."""
    assert store.servable_in_us({"country": "", "salary_currency": "CAD"}) is False
    assert store.servable_in_us({"country": "US", "salary_currency": "PLN"}) is False
    assert store.servable_in_us({"country": "US", "salary_currency": "USD"}) is True
    assert store.servable_in_us({"country": "US", "salary_currency": ""}) is True


def test_us_only_drops_known_foreign():
    assert store.servable_in_us({"country": "GB"}) is False
    assert store.servable_in_us({"country": "IN"}) is False


def test_us_only_keeps_us_and_unknown_country():
    # Unknown passes on purpose: 7,277 of 21,495 rows carry no country and are
    # overwhelmingly US ATS boards. Dropping on suspicion costs far more than it saves.
    assert store.servable_in_us({"country": "US"}) is True
    assert store.servable_in_us({"country": ""}) is True
    assert store.servable_in_us({}) is True


def test_us_only_is_case_insensitive():
    assert store.servable_in_us({"country": "us"}) is True
    assert store.servable_in_us({"country": "gb"}) is False
    assert store.servable_in_us({"salary_currency": "usd"}) is True
    assert store.servable_in_us({"salary_currency": "cad"}) is False


# ── schema v2: the columns, the triggers, the version guard ───────────────────
# job_radar 0.7.0 emits a 39-field record and v1 kept 13. These pin the container,
# not the opinion: what the store PROMISES to hold, and the two ways that promise
# can break silently.

EXPECTED_COLUMNS = (
    "url",
    "title",
    "title_root",
    "title_level",
    "title_qualifiers",
    "company",
    "team",
    "location",
    "city",
    "state",
    "country",
    "locations",
    "remote",
    "remote_basis",
    "remote_areas",
    "remote_regions",
    "remote_scope_raw",
    "body",
    "category",
    "tags",
    "seniority",
    "seniority_basis",
    "employment_type",
    "employment_type_raw",
    "salary",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "salary_basis",
    "salary_estimated_min",
    "salary_estimated_max",
    "salary_band",
    "source",
    "source_extra",
    "direct_apply",
    "posted",
    "expires",
    "fetched_at",
    "last_seen",
)


def _cols(db):
    with sqlite3.connect(db) as c:
        return [r[1] for r in c.execute("PRAGMA table_info(jobs)")]


def test_schema_has_exactly_the_expected_columns(db):
    assert tuple(_cols(db)) == EXPECTED_COLUMNS


def test_geo_index_exists(db):
    with sqlite3.connect(db) as c:
        idx = {r[1] for r in c.execute("PRAGMA index_list(jobs)")}
    assert "idx_jobs_geo" in idx  # the location filter reads country/state/city


def test_upsert_sql_matches_normalize_job(db):
    """The INSERT binds by NAME. A column named in the SQL that normalize_job stopped
    returning raises at request time, not import time — taking down the nightly
    harvest and live search together, since both funnel through upsert_jobs."""
    assert set(store._ROW_COLUMNS) == set(store.normalize_job({}))
    assert set(store._ROW_COLUMNS) | {"fetched_at", "last_seen"} == set(_cols(db))


def test_engine_fields_round_trip(db):
    job = _job(
        "e1",
        "Senior Application Security Engineer (Remote)",
        title_root="Application Security Engineer",
        title_level="III",
        title_qualifiers=["remote", "southeast"],
        team="Platform Security",
        city="Austin",
        state="TX",
        country="US",
        locations=[{"raw": "Austin", "city": "Austin", "state": "TX"}],
        tags=["kubernetes"],
        employment_type="full_time",
        employment_type_raw="Full-Time",
        salary_min=180000.0,
        salary_max=220000.0,
        salary_currency="USD",
        salary_period="year",
        salary_basis="stated",
        source_extra={"updated_at": "2026-07-27"},
        direct_apply=True,
        expires="2026-09-01",
    )
    store.upsert_jobs([job], path=db)
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        r = dict(c.execute("SELECT * FROM jobs WHERE url='e1'").fetchone())

    assert r["title"] == "Senior Application Security Engineer (Remote)"  # verbatim
    assert r["title_root"] == "Application Security Engineer"
    assert (r["city"], r["state"], r["country"]) == ("Austin", "TX", "US")
    assert r["direct_apply"] == 1  # INTEGER, not the string "True"
    assert r["salary_min"] == 180000.0
    assert json.loads(r["locations"])[0]["city"] == "Austin"
    assert json.loads(r["title_qualifiers"]) == ["remote", "southeast"]
    assert json.loads(r["source_extra"])["updated_at"] == "2026-07-27"
    assert r["expires"] == "2026-09-01"


def test_absent_containers_store_null_not_the_string_null(db):
    """json.dumps(None) is the four-character string "null", which reads as PRESENT
    to every fill-rate count and `IS NOT NULL` filter. 43-95% of rows have no
    qualifiers/tags/source_extra, so getting this wrong makes them all look full."""
    store.upsert_jobs([_job("n1", "Analyst")], path=db)
    with sqlite3.connect(db) as c:
        row = c.execute(
            "SELECT title_qualifiers, tags, source_extra, locations FROM jobs "
            "WHERE url='n1'"
        ).fetchone()
    assert row == (None, None, None, None)


def test_direct_apply_false_is_zero_not_null(db):
    """False and absent are different answers — 0 means the engine checked."""
    store.upsert_jobs([_job("d1", "Analyst", direct_apply=False)], path=db)
    store.upsert_jobs([_job("d2", "Analyst")], path=db)
    with sqlite3.connect(db) as c:
        vals = dict(c.execute("SELECT url, direct_apply FROM jobs").fetchall())
    assert vals["d1"] == 0
    assert vals["d2"] is None


def test_reupsert_refreshes_new_columns_but_keeps_fetched_at(db):
    store.upsert_jobs([_job("r1", "Engineer", title_root="Engineer")], path=db)
    with sqlite3.connect(db) as c:
        first = c.execute("SELECT fetched_at FROM jobs WHERE url='r1'").fetchone()[0]
    time.sleep(0.01)
    store.upsert_jobs(
        [_job("r1", "Engineer", title_root="Engineer", city="Boise")], path=db
    )
    with sqlite3.connect(db) as c:
        got = c.execute(
            "SELECT fetched_at, last_seen, city FROM jobs WHERE url='r1'"
        ).fetchone()
    assert got[0] == first  # first-seen, never refreshed
    assert got[1] > first  # the eviction clock does refresh
    assert got[2] == "Boise"


def test_fts_triggers_still_populate_and_delete(db):
    """The failure this catches has no error message: a trigger whose column list
    drifted from jobs_fts stops populating the index, every search returns nothing,
    and every other check in the suite stays green."""
    store.upsert_jobs(
        [_job("f1", "Data Engineer"), _job("f2", "Nurse Practitioner")], path=db
    )
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT count(*) FROM jobs_fts").fetchone()[0] == 2
        assert (
            c.execute(
                "SELECT count(*) FROM jobs_fts WHERE jobs_fts MATCH 'title: nurse'"
            ).fetchone()[0]
            == 1
        )
        c.execute("DELETE FROM jobs WHERE url='f1'")
        assert c.execute("SELECT count(*) FROM jobs_fts").fetchone()[0] == 1


def test_init_refuses_a_v1_shaped_store(tmp_path, monkeypatch):
    """`CREATE TABLE IF NOT EXISTS` makes a schema change a silent no-op on an
    existing file. Without this guard the damage first appears as `no such column`
    inside the nightly harvest, hours later and in a log nobody is reading.

    Built as a real v1 table rather than by deleting the version marker, because the
    marker is not what makes a store stale — its SHAPE is. Every store written before
    SCHEMA_VERSION existed is unmarked, and the guard has to read those correctly."""
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    p = str(tmp_path / "v1.db")
    with sqlite3.connect(p) as c:
        c.execute(
            "CREATE TABLE jobs(url TEXT PRIMARY KEY, title TEXT, company TEXT,"
            " location TEXT, source TEXT, posted TEXT, salary TEXT, body TEXT,"
            " category TEXT, employment_type TEXT, remote TEXT, seniority TEXT,"
            " salary_band TEXT, fetched_at REAL, last_seen REAL)"
        )
        c.execute("INSERT INTO jobs(url,title) VALUES('u','Analyst')")
    with pytest.raises(store.StaleSchemaError, match="rebuild_store"):
        store.init(p)


def test_init_adopts_an_unmarked_but_current_store(db):
    """An unmarked store whose shape is already current is adopted, not rejected —
    rejecting it would demand a pointless rebuild of a perfectly good file."""
    with sqlite3.connect(db) as c:
        c.execute("DELETE FROM meta WHERE key='schema_version'")
    store.upsert_jobs([_job("s1", "Analyst")], path=db)
    store.init(db)
    with sqlite3.connect(db) as c:
        v = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert int(v[0]) == store.SCHEMA_VERSION


def test_init_marks_a_fresh_store_without_complaining(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    p = str(tmp_path / "fresh.db")
    store.init(p)
    store.init(p)  # idempotent
    with sqlite3.connect(p) as c:
        v = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert int(v[0]) == store.SCHEMA_VERSION


def test_init_refuses_a_future_schema_version(db):
    with sqlite3.connect(db) as c:
        c.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    with pytest.raises(store.StaleSchemaError, match="v99"):
        store.init(db)


def test_the_three_facets_only_speak_from_evidence(db):
    """The load-bearing property of step 2, in one place: none of the three facets may
    assert something the source did not say.

    (This replaces test_opinion_columns_are_unchanged_by_v2, which was step 1's guard
    that the container change moved no opinion. Step 2 IS the opinion change, so the
    guard it enforced is now exactly what must no longer hold.)"""
    r = store.normalize_job(
        _job("o1", "Senior Nurse", location="Remote", department="Healthcare Jobs")
    )
    assert (r["remote"], r["remote_basis"]) == ("remote", "derived")
    assert r["seniority"] is None  # "Senior" in the TITLE is not the source saying so
    assert r["category"] is None  # `department` is an org-unit name, not a category

    quiet = store.normalize_job(_job("o2", "Nurse", location="Wahpeton, ND"))
    for field in ("remote", "remote_basis", "seniority", "seniority_basis", "category"):
        assert quiet[field] is None, f"{field} invented a value from nothing"


def test_state_is_normalised_to_a_usps_code(db):
    """The one non-pass-through field. A drawer needs a closed set, and the raw column
    holds 126 values for a field whose real range is 53."""
    rows = [
        _job("s-a", "Nurse", state="Ohio"),
        _job("s-b", "Nurse", state="ca"),
        _job("s-c", "Nurse", state="Ontario"),
        _job("s-d", "Nurse", state=""),
    ]
    assert store.normalize_job(rows[0])["state"] == "OH"
    assert store.normalize_job(rows[1])["state"] == "CA"
    assert store.normalize_job(rows[2])["state"] is None  # foreign, not a US state
    assert store.normalize_job(rows[3])["state"] is None


def test_the_two_body_caps_must_stay_equal():
    """`snapshot.TEXT_CAP` truncates before jobs.json is written and `store.BODY_CAP`
    truncates on the way into the store. Raising only the second is a silent no-op for
    every harvested row; raising only the first leaves the harvest and the live fetch
    feeding the scorer different amounts of the same job. There is no reason for them
    ever to differ, and every reason for the difference to go unnoticed."""
    from jobfitr import snapshot

    assert store.BODY_CAP == snapshot.TEXT_CAP


# ── the salary cluster (panel majors M2, M3, M4) ─────────────────────────────
def test_k_notation_is_read_as_thousands():
    """M2. The old fallback regex needed 3+ digit characters, so "$255k" contained no
    number at all and the row banded `under-50k` — the best-paying jobs on the board in
    the lowest drawer, hidden outright by any salary floor. 1,919 of 5,203 salary
    strings in the capture write it this way."""
    assert store._first_figure("$255k – $290k") == 255_000
    assert store._salary_band({"salary": "$255k – $290k"}) == "180k-plus"
    # the leading K is often implied: "$200-260K" is $200K-$260K, not $200-$260,000
    assert store._first_figure("$200-260K") == 200_000
    assert store._first_figure("$300 - $350K") == 300_000
    # ...but a bare number already 1,000 or more is a full figure and stays one
    assert store._first_figure("$150,000 - 200K") == 150_000


def test_the_401k_shape_is_a_real_salary_in_this_field():
    """The trap that would break a K rule in BODY text — "401k match" reading as
    $401,000 — was measured against the real `salary` field before this was written:
    the only two strings of that shape are "$401K – $445K", a genuine $401,000 salary.
    The field is short and structured. This pins that scope: if someone reuses
    `_first_figure` against a job body, this test is the reminder to re-measure."""
    assert store._first_figure("$401K – $445K") == 401_000


def test_the_band_and_the_slider_read_the_same_end_of_a_range():
    """M3. `annual_salary` read `salary_min` while the fallback took `max(nums)`, so the
    same job got a different band depending only on whether the engine had parsed it —
    1,113 strings in the capture disagree. They must agree, and the MINIMUM is the end
    that makes them agree correctly: the card's slider is a FLOOR filter, so the number
    behind the chip has to be the floor too."""
    assert store._first_figure("$171,000 – $256,400") == 171_000
    assert store._salary_band({"salary": "$171,000 – $256,400"}) == "120-180k"
    # a wide range is banded by what the job GUARANTEES, not what it might pay
    assert store._salary_band({"salary": "$40,000 – $250,000"}) == "under-50k"


def test_a_blank_period_still_yields_an_annual_figure():
    """M4. 154 rows carried a clean parsed USD figure with no stated period and got
    nothing. ANNUAL_FLOOR is just under a US full-time year at the federal minimum."""
    usd = {"salary_currency": "USD", "salary_period": ""}
    assert store.annual_salary({**usd, "salary_min": 148_000}) == 148_000
    assert store.annual_salary({**usd, "salary_min": store.ANNUAL_FLOOR}) is not None
    # below the floor it is not a yearly salary — the capture's one such row is "$33–$41"
    assert store.annual_salary({**usd, "salary_min": 33}) is None
    # a STATED period still wins over the default
    assert store.annual_salary(
        {"salary_currency": "USD", "salary_period": "hour", "salary_min": 93}
    ) == 93 * 2080


def test_the_engine_figure_beats_the_string_fallback():
    """The fallback is a fallback. A row the engine parsed must not be re-read off its
    display string, or the two rules could disagree on the same row."""
    band = store._salary_band(
        {
            "salary": "$40,000",  # the string says one thing
            "salary_min": 200_000,  # the engine says another, and the engine wins
            "salary_currency": "USD",
            "salary_period": "year",
        }
    )
    assert band == "180k-plus"


def test_bm25_ordering_is_the_boards_tie_break(db):
    """`d["bm25"]`'s VALUE is read by nothing, and a handoff note once concluded from
    that the whole thing was dead code. It is not: `server._rank` sorts by points with
    Python's STABLE sort, so equal-scoring listings keep retrieval's order — and that is
    `ORDER BY rank`. Ties are the normal case (23 of the top 50 for a one-word query),
    and measured, shuffling candidate order moves 51-65 of the delivered top 200.

    Asserted as the PROPERTY, not a fixed permutation: BM25 length-normalises, so which
    document wins is its business. What must hold is that the rows come back in its
    order, and that a stable sort therefore preserves it for tied scores."""
    store.upsert_jobs(
        [
            _job("w1", "Data Analyst", "analyst " * 40),
            _job("w2", "Data Analyst", "analyst " * 4),
            _job("w3", "Data Analyst", "analyst"),
        ],
        path=db,
    )
    got = store.bm25_candidates(["data analyst"], path=db)
    assert len(got) == 3
    scores = [c["bm25"] for c in got]
    # returned in BM25 order, best first — this is what the stable sort inherits
    assert scores == sorted(scores, reverse=True), f"not in BM25 order: {scores}"
    assert len(set(scores)) > 1, "the fixture produced no BM25 spread to order by"

    # and the tie-break survives the scorer: three listings on the SAME points come out
    # in the order retrieval gave them, not an arbitrary one.
    from jobfitr.server import _rank

    kept, _ = _rank(got, ["data analyst"], [], [], [], False, None, 10, [])
    assert len({pts for _, pts, _, _ in kept}) == 1, "fixture should tie on points"
    assert [c["url"] for c, _, _, _ in kept] == [c["url"] for c in got]


# ── US-only intake: reading a place out of free text ─────────────────────────


def test_a_stated_foreign_boundary_is_dropped():
    """`remote_areas` is job-radar 0.8.x's parse of the boundary a posting actually
    STATED, and it is the strongest geography signal available: 3,398 rows kept under the
    old country+currency test have an entirely non-US stated boundary, against 188 for
    currency. Structured evidence, not a regex."""
    assert store.servable_in_us({"remote_areas": ["GB"]}) is False
    assert store.servable_in_us({"remote_areas": ["CA", "MX"]}) is False
    assert store.servable_in_us({"remote_areas": ["US", "US-TX"]}) is True
    assert store.servable_in_us({"remote_areas": ["GB", "US"]}) is True, (
        "a posting open to both is one a US worker can take"
    )


def test_an_empty_boundary_means_worldwide_not_unknown():
    """job-radar 0.8.0 made this load-bearing: None = the posting said nothing, [] = it
    STATED anywhere. Collapsing them drops the most permissive postings in the feed."""
    assert store.servable_in_us({"remote_areas": []}) is True
    assert store.servable_in_us({"remote_areas": None, "location": "Remote"}) is True


def test_the_location_text_catches_what_the_engine_will_not_guess():
    """The engine refuses a comma-less string and a two-letter tail, deliberately. Its
    curated 60-name country map also cannot see Uruguay, Armenia or Slovakia — 2,343 rows
    the structured field misses. jobfitr owns the US-only opinion, so jobfitr reads."""
    for loc in ("Singapore", "London, UK", "Slovakia", "Madrid", "Bengaluru",
                "LatAm (Remote)", "Remote - Europe", "Türkiye, Remote"):
        assert store.servable_in_us({"location": loc}) is False, loc


def test_a_us_city_sharing_a_foreign_name_is_kept():
    """THE false-positive class, and the reason US evidence wins outright. Measured: of
    2,435 rows naming a foreign city, 110 also carry a US signal and all 110 are correct
    keeps — `Cambridge, MA USA` x47, `Manchester, NH`, `Vienna, Virginia`."""
    for loc in ("Dublin, CA", "Berlin, CT", "Toronto, OH", "Paris, TX",
                "Manchester, NH", "Moscow, ID", "Odessa, TX", "Versailles, KY",
                "Cambridge, MA USA", "Vienna, Virginia"):
        assert store.servable_in_us({"location": loc}) is True, loc


def test_an_english_word_that_is_a_state_code_is_not_a_place():
    """`us_state('or')` is Oregon, so scanning loose tokens made `Italy or France or
    Germany` AMERICAN — 130 rows kept on exactly that. A state only ever appears as the
    tail of `City, ST`, so only comma-delimited segments are read. Same shape for
    in/me/hi/ok/de/la/co/id/ma/pa/oh."""
    assert store.servable_in_us({"location": "Italy or France or Germany"}) is False
    assert store.servable_in_us({"location": "London or Berlin HYBRID"}) is False
    assert store.servable_in_us({"location": "Austin, TX"}) is True


def test_unknown_still_passes():
    """Evidence is ADDED; the default is not inverted. A placeless remote job names no
    country and must not be inferred foreign."""
    for loc in ("Remote", "Anywhere", "Global", "Distributed", ""):
        assert store.servable_in_us({"location": loc}) is True, loc
