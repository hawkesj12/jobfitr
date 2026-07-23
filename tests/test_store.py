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
            department="IT Jobs",
            employment_type="full_time",
            salary="$120,000–$140,000",
            location="Austin, TX",
        )
    )
    assert r["category"] == "IT Jobs"
    assert r["employment_type"] == "full_time"
    assert r["seniority"] == "senior"
    assert r["remote"] == "onsite"
    assert r["salary_band"] == "120-180k"
    assert (
        store.normalize_job(_job("u2", "Nurse", location="Remote"))["remote"]
        == "remote"
    )


def test_normalize_strips_adzuna_remote_artifact():
    # job_radar appends " (Remote)" to EVERY Adzuna location — noise, not signal.
    r = store.normalize_job(
        _job(
            "u",
            "Grocery Store Manager",
            location="Wahpeton, ND (Remote)",
            source="adzuna",
        )
    )
    assert r["location"] == "Wahpeton, ND"  # artifact stripped
    assert r["remote"] == "onsite"  # so an on-site grocery job isn't mislabeled remote
    # a genuinely remote-titled Adzuna job still reads remote (title signal)
    assert (
        store.normalize_job(
            _job(
                "u2",
                "Remote Customer Advocate",
                location="Austin (Remote)",
                source="adzuna",
            )
        )["remote"]
        == "remote"
    )
    # the free remote boards' conditional "(Remote)" is real signal — keep it
    assert (
        store.normalize_job(
            _job("u3", "Designer", location="Anywhere (Remote)", source="remotive")
        )["remote"]
        == "remote"
    )


def test_normalize_remote_from_body():
    # The keyed sources (Adzuna/USAJOBS) carry no remote flag and lose their
    # "(Remote)" artifact, so a genuinely-remote role reads onsite from title+loc
    # alone. The body scan recovers it — this is the fix for empty remote searches.
    r = store.normalize_job(
        _job(
            "b1",
            "Front End Developer",
            location="Austin, TX (Remote)",
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
                location="Austin, TX (Remote)",
                text="On-site only. This is not a remote position; you must work from our Austin office.",
            )
        )["remote"]
        == "onsite"
    )
    # incidental "remote" in prose must NOT flip an on-site job (no false positive)
    assert (
        store.normalize_job(
            _job(
                "b3",
                "Systems Engineer",
                location="Dayton, OH (Remote)",
                text="You will administer remote servers and support remote teams across our data centers.",
            )
        )["remote"]
        == "onsite"
    )


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
