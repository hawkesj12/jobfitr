"""Phase F web tests. The old zero-network guarantee is retired — a request now
does a BOUNDED live fetch on a cache miss and serves the fresh cache on a hit. These
tests pin: config mapping, the snapshot round-trip, the store-backed BM25 + rerank
score path (tags + facets), the live-fetch-on-miss + fresh-cache-hit branches, and
graceful degradation when the daily ceiling trips.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from jobfitr import live, server, snapshot, store, vocab
from jobfitr.config_builder import config_from_dict

RECENT = (date.today() - timedelta(days=3)).isoformat()


def _job(
    title,
    text="",
    company="Acme",
    location="Remote",
    posted=RECENT,
    url=None,
    **kw,
):
    # default a UNIQUE url per title so upsert (dedups by url) keeps distinct rows
    row = {
        "title": title,
        "text": text,
        "company": company,
        "location": location,
        "posted": posted,
        "url": url or f"https://x/{title.lower().replace(' ', '-')}",
        "source": "adzuna",
    }
    row.update(kw)
    return row


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient over an isolated tmp store, the rate limiter off, fetch usage reset."""
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    store.init()  # DB_PATH now points at the tmp db
    server.limiter.enabled = False  # don't let 40/min trip across the suite
    # the live-fetch tally now lives in the (fresh, per-test) store, so nothing to reset
    server._last_fetch_ok["at"] = None
    monkeypatch.setattr(
        server, "ADZUNA_DAILY_CEILING", 800
    )  # a known ceiling for the degrade test
    yield TestClient(server.app)
    server.limiter.enabled = True


def _seed(rows):
    store.upsert_jobs(rows)


def _mark_fresh(titles, location=""):
    store.mark_fetched(store.search_key(titles, location))


# ── config_from_dict ──────────────────────────────────────────────────────────
def test_config_from_dict_maps_the_five_answers():
    cfg = config_from_dict(
        {
            "titles": ["Zookeeper", "Animal Keeper"],
            "boosts": ["Reptiles", "biology degree"],
            "exclude": ["Intern", "volunteer"],
            "rank_down": ["staffing"],
            "location": "Louisville, KY",
            "max_age_days": 45,
            "min_score": "strong",
        }
    )
    assert cfg.title_queries == ["zookeeper", "animal keeper"]
    assert cfg.exclude_titles == ["intern", "volunteer"]
    assert cfg.agency_penalty == {"staffing": 8}
    assert cfg.location == "Louisville, KY"
    assert cfg.remote_only is False  # a real place turns off remote-only


def test_config_from_dict_no_location_shows_all():
    # The live-fetch default: no location named → show ALL jobs, not remote-only.
    assert config_from_dict({}).remote_only is False
    assert config_from_dict({"titles": ["nurse"]}).remote_only is False


def test_config_from_dict_remote_and_anywhere():
    assert config_from_dict({"location": "remote"}).remote_only is True
    assert config_from_dict({"location": "anywhere"}).remote_only is False
    assert (
        config_from_dict({"location": "Denver", "remote_only": True}).remote_only
        is True
    )


def test_config_from_dict_does_not_inherit_tech_exclude_defaults():
    from job_radar.scoring import relevant

    cfg = config_from_dict({"titles": ["accountant"]})
    assert cfg.exclude_titles == []
    assert relevant("General Accountant", cfg) is True
    cfg2 = config_from_dict({"titles": ["engineer"], "exclude": ["intern"]})
    assert cfg2.exclude_titles == ["intern"]
    assert relevant("Engineering Intern", cfg2) is False


# ── snapshot round-trip (still writes jobs.json; now also feeds the store) ─────
def test_snapshot_roundtrip(tmp_path, monkeypatch):
    from job_radar.config import Config

    # keep the baseline harvest's store-upsert isolated to a tmp db
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "snap.db"))
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    store.init()

    rows = [
        # longer than the cap ON PURPOSE, expressed relative to it so raising the
        # constant can never quietly stop exercising the truncation it guards.
        {
            **_job("Data Engineer", text="x" * (snapshot.TEXT_CAP + 1000)),
            "sources": {"remotive", "remoteok"},
        },
        {**_job("Product Manager", url="https://x/pm"), "sources": {"jobicy"}},
    ]
    monkeypatch.setattr(
        snapshot.engine,
        "harvest",
        lambda cfg, *a, **kw: (rows, [], ["boom: himalayas timed out"]),
    )
    out = tmp_path / "jobs.json"
    meta = snapshot.build_snapshot(Config(), None, str(out))

    assert out.exists()
    assert meta["count"] == 2
    assert "himalayas" in meta["errors"][0]
    # the harvest also fed the store (the demoted baseline inflow)
    assert store.pool_size() == 2

    snap = snapshot.load_snapshot(str(out))
    j0 = snap["jobs"][0]
    assert isinstance(j0["sources"], list) and j0["sources"] == ["remoteok", "remotive"]
    assert len(j0["text"]) == snapshot.TEXT_CAP


def test_load_snapshot_missing_file_is_empty(tmp_path):
    snap = snapshot.load_snapshot(str(tmp_path / "nope.json"))
    assert snap["jobs"] == [] and snap["meta"]["count"] == 0


# ── /api/score: BM25 candidates + personalized rerank + tags + facets ─────────
def test_score_ranks_boosts_excludes_and_tags(client, monkeypatch):
    _seed(
        [
            _job(
                "Senior Python Engineer",
                text="python kubernetes docker accountant-free",
                location="Austin, TX",
                salary="$140,000",
                category="IT Jobs",
                seniority="Senior Level",
                remote_type="onsite",
                remote_basis="stated",
                employment_type="full_time",
            ),
            _job("Data Engineer", text="python etl pipelines", category="IT Jobs"),
            _job("Marketing Engineer", text="seo content calendar growth"),
            _job("Engineering Intern", text="python kubernetes internship"),
        ]
    )
    _mark_fresh(["engineer"])  # fresh cache → no live fetch
    monkeypatch.setattr(
        live,
        "coalesced_fetch",
        lambda *a: (_ for _ in ()).throw(AssertionError("fresh cache must not fetch")),
    )

    d = client.post(
        "/api/score",
        json={
            "titles": ["engineer"],
            "boosts": ["python", "kubernetes"],
            "exclude": ["intern"],
            "min_score": "plenty",
        },
    ).json()
    titles = [j["title"] for j in d["jobs"]]
    assert "Engineering Intern" not in titles  # hard-excluded
    assert titles[0] == "Senior Python Engineer"  # both boosts + title
    scores = [j["points"] for j in d["jobs"]]
    assert scores == sorted(scores, reverse=True)

    top = d["jobs"][0]
    assert top["why"]  # matched signals
    assert "text" not in top and "snippet" in top  # body not leaked
    # The raw "IT Jobs" is mapped to the canonical field, not passed through.
    assert top["category"] == "Software Engineering"
    assert top["employment_type"] == "full_time"
    assert "senior" in top["tags"] and "onsite" in top["tags"]  # both source-STATED
    assert isinstance(top["points"], int)
    assert sum(delta for _, delta in top["parts"]) == top["points"]
    # facets counted over the returned set
    assert d["facets"]["category"]["Software Engineering"] >= 1
    assert d["pool"] == store.pool_size()
    assert d["degraded"] is None


# ── Phase A: ranking correctness ──────────────────────────────────────────────
def _no_fetch(monkeypatch):
    monkeypatch.setattr(
        live,
        "coalesced_fetch",
        lambda *a: (_ for _ in ()).throw(AssertionError("fresh cache must not fetch")),
    )


def _filler(n=40):
    """Unrelated rows so BM25 behaves like it does against the real ~10k pool.

    NOT padding — load-bearing. BM25 scores a term by how RARE it is, so in a corpus
    of two documents that both contain the query terms the IDF collapses to zero and
    every candidate ties at 0.000, leaving the rerank to decide everything. That is an
    artifact of a toy corpus, not of the app. These rows restore the discrimination
    real users get; delete them and the ranking assertions below stop meaning anything.
    """
    return [
        _job(
            f"Warehouse Associate {i}",
            text="forklift picking packing shift work",
            company=f"Filler{i}",
            location="Louisville, KY",
            url=f"https://x/filler-{i}",
        )
        for i in range(n)
    ]


def test_exact_title_with_no_body_outranks_a_keyword_stuffed_off_title_listing(
    client, monkeypatch
):
    """The regression the live run demanded: a perfect-title match must not lose to a
    listing that merely repeats the user's boost words.

    Observed before the fix: a $235k-$315k "Senior Principal Forward Deployed AI
    Engineer" ranked #21 of 30, below a $94,876 off-title listing. Boosts were summed
    flat over title+body, so nine boosts handed the keyword-stuffed row up to +18 while
    the body-less Greenhouse row could earn nothing at all — a swing BM25 could never
    overcome, and one structurally unavailable to the better job.
    """
    boosts = [
        "rag",
        "multi-agent orchestration",
        "llm application development",
        "python",
        "customer discovery",
        "postgres",
        "azure",
        "warehouse automation",
        "logistics",
    ]
    _seed(
        [
            # the real role — exact title, and NO body (Greenhouse rows arrive this way)
            _job(
                "Senior Principal Forward Deployed AI Engineer",
                text="",
                company="Smartsheet",
                salary="$235,000-$315,000",
                url="https://x/smartsheet",
            ),
            # the impostor — off title, body stuffed with every boost term
            _job(
                "Consultant",
                company="Bright Vision Technologies",
                text="forward deployed ai engineer " + " ".join(boosts),
                salary="$94,876",
                url="https://x/brightvision",
            ),
            *_filler(),
        ]
    )
    _mark_fresh(["forward deployed ai engineer"])
    _no_fetch(monkeypatch)

    d = client.post(
        "/api/score",
        json={"titles": ["forward deployed ai engineer"], "boosts": boosts},
    ).json()
    titles = [j["title"] for j in d["jobs"]]
    assert titles, "both listings should be candidates"
    assert titles[0] == "Senior Principal Forward Deployed AI Engineer"


def test_a_missing_body_simply_earns_no_boost_points(client, monkeypatch):
    """Replaces test_a_missing_body_is_treated_as_unknown_not_as_zero_evidence.

    The old test asserted that a body-less listing was IMPUTED neutral boost credit, on
    the stated premise that "Greenhouse rows arrive body-less". Measured against the
    39,597-row corpus, Greenhouse has ZERO body-less rows (only 46 SmartRecruiters and 12
    Lever do), so the premise was false and NO_BODY_PRIOR was compensating for a problem
    that did not exist.

    The scoreboard is honest instead: no body means no boost evidence, so no boost points.
    The title still carries the listing, which is the whole reason the title is the anchor.
    """
    _seed(
        [
            _job("Data Engineer", text="", company="NoBody Corp", url="https://x/nb"),
            _job(
                "Data Engineer",
                text="python postgres airflow",
                company="HasBody Corp",
                url="https://x/hb",
            ),
        ]
    )
    _mark_fresh(["data engineer"])
    _no_fetch(monkeypatch)

    d = client.post(
        "/api/score",
        json={"titles": ["data engineer"], "boosts": ["python", "postgres"]},
    ).json()
    by_company = {j["company"]: j["points"] for j in d["jobs"]}
    # Both are an exact title match; only the one with evidence earns boost points.
    assert by_company["NoBody Corp"] == 100, "exact title alone"
    assert by_company["HasBody Corp"] == 116, "exact title + two boosts at one hit each"


def test_more_boosts_can_never_lower_a_score(client):
    """Replaces test_boost_swing_is_capped_regardless_of_how_many_boosts_are_given.

    The old cap existed to stop nine boosts swamping relevance. It also produced the
    perverse result that made the cap worth removing: because the bonus was a FRACTION of
    the boosts given, a listing matching 3 of 14 boosts scored LOWER than one matching
    0 of 0 — so the interview's own advice ("name as many skills as you can think of")
    punished everyone who followed it.

    The scoreboard promises the opposite, and this is that promise as a test: evidence
    only ever adds, so naming another skill can never cost you.
    """
    title, company, body = (
        "data engineer",
        "acme",
        "python postgres airflow kafka spark",
    )
    prev = -1
    for n in range(0, 6):
        boosts = ["python", "postgres", "airflow", "kafka", "spark"][:n]
        pts = server.scoreboard(title, company, body, ["data engineer"], boosts, [])[
            "points"
        ]
        assert pts >= prev, f"adding a boost lowered the score at n={n}"
        prev = pts
    # And nothing is capped: five matching boosts are worth five boosts.
    assert prev == 100 + 5 * 8


def test_exclude_matches_the_company_not_just_the_title(client, monkeypatch):
    """'recruiting agency' is an employer trait, not a job trait. Testing the title
    alone let 'Recruiting From Scratch' rank #20 under the very term meant to kill it."""
    _seed(
        [
            _job(
                "Software Engineer",
                text="python",
                company="Recruiting From Scratch",
                url="https://x/rfs",
            ),
            _job(
                "Software Engineer",
                text="python",
                company="Acme Robotics",
                url="https://x/acme",
            ),
        ]
    )
    _mark_fresh(["software engineer"])
    _no_fetch(monkeypatch)

    d = client.post(
        "/api/score",
        json={"titles": ["software engineer"], "exclude": ["recruiting"]},
    ).json()
    companies = [j["company"] for j in d["jobs"]]
    assert "Recruiting From Scratch" not in companies
    assert "Acme Robotics" in companies


def test_duplicate_listings_collapse_to_the_richest_copy(client, monkeypatch):
    """The same opening reaching the pool twice reads as a broken search — the live run
    showed one role at both #17 and #18. Identity ignores punctuation/spacing drift."""
    _seed(
        [
            _job(
                "Forward Deployed Engineer",
                text="short",
                company="iSpace, Inc",
                url="https://x/dup-a",
            ),
            _job(
                "Forward Deployed Engineer",
                text="a much longer body carrying the real detail about this role",
                company="iSpace,  Inc.",
                url="https://x/dup-b",
            ),
        ]
    )
    _mark_fresh(["forward deployed engineer"])
    _no_fetch(monkeypatch)

    d = client.post("/api/score", json={"titles": ["forward deployed engineer"]}).json()
    assert len(d["jobs"]) == 1  # one job, not two
    assert "much longer body" in d["jobs"][0]["description"]  # kept the richer copy


def test_dedup_keeps_genuinely_different_roles_at_the_same_company(client, monkeypatch):
    """The cap must not swallow real variety — same employer, different titles stay."""
    _seed(
        [
            _job("Data Engineer", company="Acme", url="https://x/de"),
            _job("Data Analyst", company="Acme", url="https://x/da"),
        ]
    )
    _mark_fresh(["data"])
    _no_fetch(monkeypatch)

    d = client.post("/api/score", json={"titles": ["data"]}).json()
    assert len({j["title"] for j in d["jobs"]}) == 2


def test_a_flat_search_no_longer_needs_a_gauge_to_look_sane(client, monkeypatch):
    """Replaces test_fit_gauge_stays_readable_when_relevance_alone_cannot_separate.

    The old failure: BM25 rates fifty jobs all titled "...Engineer" as equally relevant —
    which is CORRECT — and the relative gauge then had nothing to spread, so every card
    rendered an identical "3 · Fair" and the board looked broken. The gauge existed to
    paper over that.

    With an ABSOLUTE score there is nothing to paper over. Fifty equally-good matches
    honestly score the same number, and that number means something on its own: 100 is an
    exact title match whether it is alone on the board or one of fifty.
    """
    _seed(
        [
            _job(f"Engineer {i}", text="python", company=f"C{i}", url=f"https://x/e{i}")
            for i in range(12)
        ]
    )
    _mark_fresh(["engineer"])
    _no_fetch(monkeypatch)
    d = client.post("/api/score", json={"titles": ["engineer"]}).json()
    pts = [j["points"] for j in d["jobs"]]
    assert pts, "the search should return something"
    assert len(set(pts)) == 1, "equally-good matches score equally — that is honest"
    assert "fit_pct" not in d["jobs"][0], "the relative gauge is gone"


def test_a_plain_one_word_search_still_produces_a_usable_board(client, monkeypatch):
    _seed(
        [
            _job(
                "Senior Engineer",
                text="python kubernetes",
                company="A",
                url="https://x/1",
            ),
            _job("Staff Engineer", text="python", company="B", url="https://x/2"),
            _job("Engineer", text="", company="C", url="https://x/3"),
            *_filler(20),
        ]
    )
    _mark_fresh(["engineer"])
    _no_fetch(monkeypatch)
    d = client.post("/api/score", json={"titles": ["engineer"]}).json()
    pts = [j["points"] for j in d["jobs"]]
    assert pts, "the search should return something"
    # No normalisation, so the numbers are comparable to any other board: the two
    # "Engineer"-titled rows earn the exact-title 100, the rest earn what they earn.
    assert max(pts) == 100
    assert all(isinstance(p, int) for p in pts)


# ── Phase B: data hygiene at the source ───────────────────────────────────────
def test_html_bodies_render_as_clean_text(client, monkeypatch):
    """The P0 leak: Greenhouse ships the JD as HTML and the top card opened with a
    literal '<div class="content-intro"><h3>About Arize</h3>'."""
    _seed(
        [
            _job(
                "Applied AI Engineer",
                text=(
                    '<div class="content-intro"><h3>About Arize</h3> '
                    "<p>AI is rapidly transforming the world.</p>"
                    "<p><strong>Arize AI</strong> is the leading AI &amp; Agent "
                    "Engineering&nbsp;observability platform.</p>"
                    "<script>window.tracking=1;</script></div>"
                ),
                url="https://x/arize",
            )
        ]
    )
    _mark_fresh(["applied ai engineer"])
    _no_fetch(monkeypatch)

    top = client.post("/api/score", json={"titles": ["applied ai engineer"]}).json()[
        "jobs"
    ][0]
    for field in ("description", "snippet"):
        assert "<" not in top[field] and ">" not in top[field]  # no markup
        assert "&amp;" not in top[field] and "&nbsp;" not in top[field]  # decoded
        assert "window.tracking" not in top[field]  # script body dropped
    assert "AI & Agent Engineering observability" in top["description"]
    # tags become a SPACE, never nothing — headings must not fuse into the next word
    assert "About Arize AI is rapidly" in top["description"]


def test_a_body_truncated_mid_tag_does_not_leak_markup(client, monkeypatch):
    """Found live on production after shipping the HTML strip. The tag regex needs a
    closing '>', and the harvest caps body text at ~2000 chars — so a body cut mid-tag
    left an unterminated one that rendered as literal markup on the card
    ('<a href="https://www.cnbc.com/2022/05'). A real less-than must still survive."""
    assert server._plain_text('has <a href="https://www.cnbc.com/2022') == "has"
    assert (
        server._plain_text("<p>Complete</p> tags are fine") == "Complete tags are fine"
    )
    assert (
        server._plain_text("a < b is a real less-than") == "a < b is a real less-than"
    )
    assert server._plain_text("trailing <") == "trailing <"

    _seed(
        [
            _job(
                "Engineer",
                text='We raised a round. <a href="https://example.com/very/long/url',
                url="https://x/cut",
            )
        ]
    )
    _mark_fresh(["engineer"])
    _no_fetch(monkeypatch)
    top = client.post("/api/score", json={"titles": ["engineer"]}).json()["jobs"][0]
    assert "<a" not in top["description"] and "href" not in top["description"]
    assert "We raised a round." in top["description"]


def test_employment_type_spellings_collapse_to_one_facet(client, monkeypatch):
    """Four spellings of full-time split 621 live rows across four unfilterable chips."""
    _seed(
        [
            _job("Engineer A", employment_type="Full Time", url="https://x/a"),
            _job("Engineer B", employment_type="Full-Time", url="https://x/b"),
            _job("Engineer C", employment_type="full_time", url="https://x/c"),
            _job("Engineer D", employment_type="Full-time", url="https://x/d"),
        ]
    )
    _mark_fresh(["engineer"])
    _no_fetch(monkeypatch)

    d = client.post("/api/score", json={"titles": ["engineer"]}).json()
    assert {j["employment_type"] for j in d["jobs"]} == {"full_time"}
    assert list(d["facets"]["employment_type"]) == ["full_time"]  # one chip, not four


def test_free_text_schedules_never_become_filter_chips(client, monkeypatch):
    """USAJOBS uses the field for prose. A chip you cannot filter by is worse than none."""
    _seed(
        [
            _job(
                "Analyst",
                employment_type=(
                    "This is a full-time position.  Work schedules, including telework, "
                    "are at the discretion of the supervisor, consistent with agency policy."
                ),
                url="https://x/prose",
            ),
            _job("Analyst II", employment_type="Contractor", url="https://x/contract"),
        ]
    )
    _mark_fresh(["analyst"])
    _no_fetch(monkeypatch)

    d = client.post("/api/score", json={"titles": ["analyst"]}).json()
    types = {j["employment_type"] for j in d["jobs"]}
    assert types == {"", "contract"}  # prose dropped, Contractor canonicalized


def test_category_emits_only_canonical_fields(client, monkeypatch):
    """`category` drives the "Field" facet, so it must be a job FUNCTION — and it is now
    an ALLOWLIST of 22, not a denylist of regexes. Sources put four other kinds of value
    in the field: a USAJOBS agency (~550 rows of "Department of the Navy" — an EMPLOYER),
    a seniority, an employer's internal ATS code, and an org-unit name. The denylist had
    to name each pattern and passed anything it forgot — 2,239 distinct strings survived
    it. The vocabulary can only ever emit one of 22 values, so all four die by default.
    """
    _seed(
        [
            # distinct companies, kept from when the employer cap could have bitten
            _job("Role A", company="Ay", category="IT Jobs", url="https://x/it"),
            _job(
                "Role B",
                company="Bee",
                category="Department of the Navy",
                url="https://x/navy",
            ),
            _job(
                "Role C",
                company="Cee",
                category="Mid-Senior Level",
                url="https://x/lvl",
            ),
            _job(
                "Role D",
                company="Dee",
                category="220 - Solutions PS",
                url="https://x/code",
            ),
            _job(
                "Role E",
                company="Ee",
                category="Legal &amp; Compliance",
                url="https://x/legal",
            ),
            # the one the denylist could never have caught: an org-unit name that reads
            # like a field. 2,239 of these leaked through it.
            _job("Role F", company="Eff", category="Go To Market", url="https://x/gtm"),
        ]
    )
    _mark_fresh(["role"])
    _no_fetch(monkeypatch)

    d = client.post("/api/score", json={"titles": ["role"]}).json()
    cats = {j["title"]: j["category"] for j in d["jobs"]}
    assert cats["Role A"] == "Software Engineering"  # mapped to the canonical field
    assert cats["Role B"] == ""  # an employer, not a field
    assert cats["Role C"] == ""  # a seniority, not a field
    assert cats["Role D"] == ""  # an internal code — no longer half-salvaged
    assert cats["Role E"] == "Legal Services"  # entity decoded, THEN mapped
    assert cats["Role F"] == ""  # an org unit that reads like a field
    assert set(cats.values()) <= set(vocab.CATEGORIES) | {""}


def test_score_miss_triggers_live_fetch(client, monkeypatch):
    calls = {"n": 0}

    def fake_fetch(titles, location):
        calls["n"] += 1
        return [
            _job(
                "Grocery Store Manager",
                text="retail grocery store manager",
                url="https://x/gm",
                department="Retail Jobs",
            )
        ]

    monkeypatch.setattr(live, "coalesced_fetch", fake_fetch)
    d = client.post(
        "/api/score",
        json={
            "titles": ["grocery store manager"],
            "location": "ohio",
            "min_score": "plenty",
        },
    ).json()
    assert calls["n"] == 1  # the miss went live
    assert d["jobs"] and d["jobs"][0]["title"] == "Grocery Store Manager"
    assert store.pool_size() == 1  # upserted into the store
    # a second identical search is now fresh → no second fetch
    client.post(
        "/api/score",
        json={
            "titles": ["grocery store manager"],
            "location": "ohio",
            "min_score": "plenty",
        },
    )
    assert calls["n"] == 1


def test_prefetch_warms_cache_then_score_does_not_refetch(client, monkeypatch):
    # The progressive-harvest path: prefetch the moment titles + location are known,
    # then the later score reuses the warm cache — ONE upstream fetch across both.
    calls = {"n": 0}

    def fake_fetch(titles, location):
        calls["n"] += 1
        return [_job("Line Cook", text="restaurant line cook", url="https://x/lc")]

    monkeypatch.setattr(live, "coalesced_fetch", fake_fetch)
    p = client.post(
        "/api/prefetch", json={"titles": ["line cook"], "location": "reno, nv"}
    ).json()
    assert p["ok"] is True and p["warmed"] is True
    assert calls["n"] == 1  # prefetch did the live fetch
    assert store.pool_size() == 1

    d = client.post(
        "/api/score",
        json={"titles": ["line cook"], "location": "reno, nv", "min_score": "plenty"},
    ).json()
    assert calls["n"] == 1  # score saw the fresh cache → NO second fetch
    assert d["jobs"] and d["jobs"][0]["title"] == "Line Cook"


def test_prefetch_without_titles_is_a_noop(client, monkeypatch):
    calls = {"n": 0}

    def fake_fetch(titles, location):
        calls["n"] += 1
        return []

    monkeypatch.setattr(live, "coalesced_fetch", fake_fetch)
    p = client.post("/api/prefetch", json={"location": "denver, co"}).json()
    assert p["warmed"] is False
    assert calls["n"] == 0  # nothing to fetch without a title


def test_an_old_job_is_reachable_when_the_user_set_no_recency_preference(client):
    """Replaces test_score_ladder_relaxes_freshness_to_find_matches.

    The ladder always imposed an age, 90 days at its loosest, so nothing older was
    reachable by anyone. Now the only age filter is one the USER asked for — and a
    120-day-old job, previously invisible, comes back.
    """
    old = (date.today() - timedelta(days=120)).isoformat()
    _seed(
        [
            _job(
                "Staff Data Scientist",
                text="ml data scientist role",
                posted=old,
                url="https://x/sds",
            )
        ]
    )
    _mark_fresh(["data scientist"], "remote")  # cache fresh → no live fetch
    d = client.post(
        "/api/score", json={"titles": ["data scientist"], "location": "remote"}
    ).json()
    assert d["jobs"] and d["jobs"][0]["title"] == "Staff Data Scientist"
    assert "tier" not in d  # the ladder is gone, and so is its receipt


def test_a_recency_preference_the_user_DID_set_is_still_honoured(client):
    """The flip side: dropping the ladder must not drop the user's own age filter."""
    old = (date.today() - timedelta(days=120)).isoformat()
    _seed(
        [
            _job(
                "Staff Data Scientist",
                text="ml data scientist role",
                posted=old,
                url="https://x/sds",
            )
        ]
    )
    _mark_fresh(["data scientist"], "remote")
    d = client.post(
        "/api/score",
        json={"titles": ["data scientist"], "location": "remote", "max_age_days": 30},
    ).json()
    assert d["jobs"] == []


def test_the_whole_tail_ships_up_to_the_delivery_cap(client):
    """Replaces test_score_delivers_past_fifty_without_relaxing_the_ladder.

    RESULT_CAP is now the ONLY thing between a scored listing and the user, and it is a
    bandwidth decision. 120 equally-good rows must all ship; nothing may be withheld for
    being the 51st, for scoring below some fraction of the top, or for sharing an
    employer with four other rows.
    """
    _seed(
        [
            _job(
                f"Data Engineer {i}",
                text="data engineer pipeline etl",
                company="MegaCorp",  # ALL one employer — the old cap kept 4
                url=f"https://x/de{i}",
            )
            for i in range(120)
        ]
    )
    _mark_fresh(["data engineer"], "remote")
    d = client.post(
        "/api/score", json={"titles": ["data engineer"], "location": "remote"}
    ).json()
    assert len(d["jobs"]) == 120  # every one of them, from a single company
    assert len(d["jobs"]) <= server.RESULT_CAP


def test_score_degrades_to_cache_when_ceiling_reached(client, monkeypatch):
    _seed(
        [
            _job(
                "Registered Nurse",
                text="patient care icu nurse",
                url="https://x/rn",
                department="Healthcare & Nursing Jobs",
            )
        ]
    )
    monkeypatch.setattr(server, "ADZUNA_DAILY_CEILING", 0)  # force the ceiling shut
    monkeypatch.setattr(
        live,
        "coalesced_fetch",
        lambda *a: (_ for _ in ()).throw(AssertionError("ceiling must skip the fetch")),
    )
    d = client.post(
        "/api/score", json={"titles": ["nurse"], "min_score": "plenty"}
    ).json()
    assert d["degraded"] == "live_search_limit"  # load-shed
    assert (
        d["jobs"] and d["jobs"][0]["title"] == "Registered Nurse"
    )  # served from cache


def test_meta_and_health(client):
    _seed([_job("Engineer")])
    m = client.get("/api/meta").json()
    assert m["count"] == 1
    h = client.get("/api/health").json()
    assert h["ok"] is True
    assert "adzuna_ok" in h and "openrouter_ok" in h
    assert h["daily_fetch_ceiling"] == 800
    assert h["pool_size"] == 1
    assert h["last_successful_fetch"] is None


def test_garbage_post_is_clean_4xx(client):
    resp = client.post("/api/score", json=["not", "an", "object"])
    assert resp.status_code == 422
    assert client.post("/api/score", json={}).status_code == 200  # empty → defaults


# ── the front end is served same-origin ───────────────────────────────────────
def test_static_front_end_is_served():
    c = TestClient(server.app)
    root = c.get("/")
    assert root.status_code == 200
    assert "text/html" in root.headers["content-type"]
    assert "jobfitr" in root.text
    assert c.get("/api/health").json()["ok"] is True


# ── the ledger drives the harvest (the wiring that makes resolution pay) ─────
def test_harvest_polls_the_LEDGER_not_the_watchlist_file(tmp_path, monkeypatch):
    """REGRESSION: the resolution ledger was a table nothing read. Resolving a company
    produced zero extra jobs until build_snapshot fed the ledger to the engine."""
    from job_radar.config import Config

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "led.db"))
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    store.init()
    store.record_resolution(
        "Discovered Co", {"ats": "greenhouse", "slug": "discovered", "roles": 9}
    )

    seen = {}
    monkeypatch.setattr(
        snapshot.engine,
        "harvest",
        lambda cfg, *a, **kw: seen.update(kw) or ([], [], []),
    )
    snapshot.build_snapshot(Config(), None, str(tmp_path / "out.json"))
    assert [c["slug"] for c in seen["companies"]] == ["discovered"]


def test_discovered_companies_flow_back_into_the_ledger(tmp_path, monkeypatch):
    """Discovery RETURNS candidates now instead of appending to a file, so the store
    has to catch them or they are lost."""
    from job_radar.config import Config

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "led2.db"))
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    store.init()
    found = [{"name": "Fresh Co", "ats": "ashby", "slug": "freshco", "roles": 4}]
    monkeypatch.setattr(
        snapshot.engine, "harvest", lambda cfg, *a, **kw: ([], found, [])
    )
    snapshot.build_snapshot(Config(), None, str(tmp_path / "out.json"))
    assert [c["slug"] for c in store.resolved_companies()] == ["freshco"]


def test_harvest_falls_back_to_the_watchlist_when_the_ledger_is_empty(
    tmp_path, monkeypatch
):
    """The depth lane is ~40% of the corpus and 23x more productive per company than
    breadth — it must never silently vanish because the store had a bad day."""
    import json as _json

    from job_radar.config import Config

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "led3.db"))
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    wl = tmp_path / "wl.json"
    wl.write_text(
        _json.dumps(
            {"companies": [{"name": "Seed Co", "ats": "lever", "slug": "seedco"}]}
        )
    )
    seen = {}
    monkeypatch.setattr(
        snapshot.engine,
        "harvest",
        lambda cfg, *a, **kw: seen.update(kw) or ([], [], []),
    )
    snapshot.build_snapshot(Config(), str(wl), str(tmp_path / "out.json"))
    assert [c["slug"] for c in seen["companies"]] == ["seedco"]


# ── employer concentration: visible, not corrected ───────────────────────────
# Replaces the six _spread_companies tests. The cap DROPPED an employer's roles past
# the fourth, which solved a real problem (Veterans Health has 915 rows in the corpus,
# Anduril 1,063) with an instrument that deleted evidence invisibly — the user could
# not tell the difference between "this employer has 4 openings" and "this employer has
# 900 and we hid 896". The promise has changed shape: concentration is now something
# the user SEES and can filter on the board, not something the server silently fixes.


def test_one_employer_may_now_dominate_and_that_is_visible(client):
    _seed(
        [
            _job(
                f"Python Engineer {i}",
                text="python",
                company="MegaCorp",
                url=f"https://x/mega-{i}",
            )
            for i in range(20)
        ]
        + [
            _job(
                "Python Engineer A",
                text="python",
                company="Tiny Inc",
                url="https://x/tiny-a",
            )
        ]
    )
    _mark_fresh(["python engineer"])
    d = client.post("/api/score", json={"titles": ["python engineer"]}).json()
    from collections import Counter

    counts = Counter(j["company"] for j in d["jobs"])
    assert counts["MegaCorp"] == 20, "all 20 ship — nothing is dropped for the employer"
    assert counts["Tiny Inc"] == 1
    assert len(d["jobs"]) == 21


def test_the_facet_counts_let_the_user_see_the_concentration(client):
    """The replacement for the cap is INFORMATION. Whatever the board offers as a filter
    has to be able to describe a dominated result set, or removing the cap just moves the
    problem onto the user with no tool to fix it."""
    _seed(
        [
            _job(f"Nurse {i}", text="nurse", company="MegaCorp", url=f"https://x/n{i}")
            for i in range(12)
        ]
    )
    _mark_fresh(["nurse"])
    d = client.post("/api/score", json={"titles": ["nurse"]}).json()
    assert len(d["jobs"]) == 12
    assert d["facets"], "the board needs facets to filter a dominated set"


def test_load_dotenv_survives_an_unreadable_directory(tmp_path, monkeypatch):
    """REGRESSION: load_dotenv stat'd .env in the CWD and let PermissionError escape.
    The CLI runs as the `jobfitr` service user, often from a directory that user
    cannot stat — which killed a whole resolution run before it read one company,
    over an optional file production does not even use (systemd supplies the env)."""
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / ".env").write_text("FOO=bar\n")
    locked.chmod(0o000)
    try:
        monkeypatch.chdir(tmp_path)
        assert snapshot.load_dotenv(str(locked / ".env")) == 0
    finally:
        locked.chmod(0o755)


def test_load_dotenv_still_reads_a_readable_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nexport ALPHA=1\nBETA=2\n\n")
    import os as _os

    _os.environ.pop("ALPHA", None)
    _os.environ.pop("BETA", None)
    assert snapshot.load_dotenv(str(env)) == 2
    assert _os.environ["ALPHA"] == "1" and _os.environ["BETA"] == "2"


def test_missing_harvest_config_warns_loudly(tmp_path, monkeypatch, capsys):
    """REGRESSION: the config is resolved relative to the CWD, and falling through to
    job_radar's built-in defaults is a cliff, not a soft default — those defaults are
    narrow and tech-only. A harvest launched from the wrong directory silently
    returned ~1,700 jobs instead of ~20,000, with no error at all."""
    monkeypatch.chdir(tmp_path)
    assert snapshot._resolve_config(None) is None
    assert "NARROW" in capsys.readouterr().out


def test_harvest_config_is_found_from_the_repo_root(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "web-harvest.example.yaml").write_text("profile: {}\n")
    assert snapshot._resolve_config(None) == "web-harvest.example.yaml"
    assert "NARROW" not in capsys.readouterr().out


# ── M3/M4: health surfaces the persisted tally and the snapshot size ─────────
def test_health_exposes_snapshot_count_for_the_ratio_gate(client, tmp_path):
    """M4: verify-slot.sh gates on pool_size vs snapshot_count instead of a fixed
    floor, so the slot must publish what it should be serving."""
    d = client.get("/api/health").json()
    assert "snapshot_count" in d and isinstance(d["snapshot_count"], int)


def test_health_reports_the_persisted_fetch_tally(client):
    """M3: the count survives outside the request handler's memory."""
    store.note_live_fetch()
    store.note_live_fetch()
    assert client.get("/api/health").json()["daily_fetches_used"] == 2


# ── M5: a failed discovered-company write is reported, not swallowed ─────────
def test_discovered_write_failure_is_reported_not_silent(tmp_path, monkeypatch, capsys):
    """REGRESSION (panel M5): build_snapshot swallowed a ledger-write failure with a
    bare `except: pass`, so a harvest could discover companies and persist none of
    them, invisibly and forever."""
    from job_radar.config import Config

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    store.init()

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(
        snapshot.engine,
        "harvest",
        lambda cfg, *a, **kw: (
            [],
            [{"name": "New Co", "ats": "ashby", "slug": "n"}],
            [],
        ),
    )
    monkeypatch.setattr(store, "record_resolution", boom)
    meta = snapshot.build_snapshot(Config(), None, str(tmp_path / "out.json"))
    assert meta["count"] == 0  # harvest still succeeded (didn't raise)
    assert "could not record" in capsys.readouterr().out  # but it said so


# ── related titles: the model's suggestions ──────────────────────────────────


def test_a_suggested_title_finds_jobs_the_users_own_wording_cannot(client):
    """THE POINT OF THE FEATURE. _fts_query ORs quoted exact phrases, so a multi-word
    title only matches verbatim. In the real corpus '"High School Teacher"' matches 0
    rows while 'Teacher' matches 225 — the user is invisible to a board that holds
    exactly the job they want."""
    _seed([_job("Teacher", text="classroom teaching", url="https://x/t1")])
    _mark_fresh(["high school teacher"])

    alone = client.post("/api/score", json={"titles": ["High School Teacher"]}).json()
    assert alone["jobs"] == [], (
        "the exact phrase matches nothing — the bug being routed around"
    )

    with_related = client.post(
        "/api/score",
        json={"titles": ["High School Teacher"], "related_titles": ["Teacher"]},
    ).json()
    assert len(with_related["jobs"]) == 1
    assert with_related["jobs"][0]["title"] == "Teacher"


def test_a_suggested_match_is_labelled_so_the_card_can_say_so(client):
    _seed([_job("Teacher", text="classroom teaching", url="https://x/t1")])
    _mark_fresh(["high school teacher"])
    d = client.post(
        "/api/score",
        json={"titles": ["High School Teacher"], "related_titles": ["Teacher"]},
    ).json()
    job = d["jobs"][0]
    assert ["related title", 30] in [list(p) for p in job["parts"]]
    assert job["points"] == 30


def test_the_users_own_title_outranks_a_suggested_one(client):
    """Precedence is the whole design. A job matching what the user ASKED for must sit
    above one matching only what the machine guessed, however good the guess looks."""
    _seed(
        [
            _job("Teacher", text="classroom", url="https://x/sug"),
            _job("High School Teacher", text="classroom", url="https://x/own"),
        ]
    )
    _mark_fresh(["high school teacher"])
    d = client.post(
        "/api/score",
        json={"titles": ["High School Teacher"], "related_titles": ["Teacher"]},
    ).json()
    assert [j["title"] for j in d["jobs"]] == ["High School Teacher", "Teacher"]
    assert d["jobs"][0]["points"] == 100 and d["jobs"][1]["points"] == 30


def test_a_config_without_related_titles_scores_exactly_as_before(client):
    """Every stored search and the whole production config predate this field."""
    _seed([_job("High School Teacher", text="classroom", url="https://x/own")])
    _mark_fresh(["high school teacher"])
    without = client.post("/api/score", json={"titles": ["High School Teacher"]}).json()
    empty = client.post(
        "/api/score", json={"titles": ["High School Teacher"], "related_titles": []}
    ).json()
    assert without["jobs"][0]["points"] == empty["jobs"][0]["points"] == 100


def test_the_retrieval_flag_keeps_suggestions_out_of_the_query(monkeypatch, client):
    """The measurement seam. With the flag off a suggested title still SCORES but never
    RETRIEVES — which is what separates 'the ranker helped' from 'the retrieval helped'.
    Bundled, the before/after cannot credit either one."""
    monkeypatch.setattr(server, "RELATED_IN_RETRIEVAL", False)
    _seed([_job("Teacher", text="classroom teaching", url="https://x/t1")])
    _mark_fresh(["high school teacher"])
    d = client.post(
        "/api/score",
        json={"titles": ["High School Teacher"], "related_titles": ["Teacher"]},
    ).json()
    assert d["jobs"] == [], "retrieval must not see the suggestion when the flag is off"


# ── penalties that read the meaning, not just the word ───────────────────────


def test_a_company_named_somebody_elses_client_is_penalised(client):
    """Found by READING a board, not by a test. u11's #2 result was a Forward Deployed
    Engineer role at a company called "TechTree's client" — a placement shop declaring
    the arrangement in the employer field — scoring 122 with no penalty, because the
    avoid-term is "our client" and phrase matching does exactly what it says."""
    _seed(
        [
            _job(
                "Forward Deployed Engineer",
                text="build integrations",
                company="TechTree's client",
                url="https://x/tt",
            ),
            _job(
                "Forward Deployed Engineer",
                text="build integrations",
                company="Twilio",
                url="https://x/tw",
            ),
        ]
    )
    _mark_fresh(["forward deployed engineer"])
    d = client.post("/api/score", json={"titles": ["Forward Deployed Engineer"]}).json()
    by_co = {j["company"]: j for j in d["jobs"]}
    assert ["client", -30] in [list(p) for p in by_co["TechTree's client"]["parts"]]
    assert by_co["Twilio"]["points"] > by_co["TechTree's client"]["points"]


def test_the_client_rule_does_not_sink_client_facing_job_titles(client):
    """The scoping is load-bearing, not tidy. 244 job TITLES in the corpus contain
    "client" — Client Success Director, Client Support Engineer — all ordinary roles.
    The penalty path tests title and company as one blob, so a term added the usual way
    would have sunk every one of them."""
    _seed(
        [
            _job(
                "Client Success Director",
                text="lead the account team",
                company="Acme",
                url="https://x/csd",
            )
        ]
    )
    _mark_fresh(["client success director"])
    d = client.post("/api/score", json={"titles": ["Client Success Director"]}).json()
    assert d["jobs"][0]["points"] == 100
    assert not [p for p in d["jobs"][0]["parts"] if p[1] < 0]


def test_serving_clients_is_not_the_same_as_placing_you_at_one(client):
    """The 19:1 case. Of 1,619 corpus bodies using "our client(s)", 753 have the client
    as the party being SERVED and 39 as the party HIRING. The qualifier keys on which."""
    _seed(
        [
            # distinct companies: _dedupe_listings collapses same-title-same-employer
            # rows, which is correct behaviour and would hide half of this test
            _job(
                "Data Analyst",
                text="we help our clients transform complex data",
                company="Servesco",
                url="https://x/served",
            ),
            _job(
                "Data Analyst",
                text="our client is seeking an analyst for a contract",
                company="Placeco",
                url="https://x/hiring",
            ),
        ]
    )
    _mark_fresh(["data analyst"])
    d = client.post(
        "/api/score", json={"titles": ["Data Analyst"], "rank_down": ["our client"]}
    ).json()
    by_url = {j["url"]: j for j in d["jobs"]}
    assert not [p for p in by_url["https://x/served"]["parts"] if p[1] < 0], (
        "serving clients is ordinary B2B"
    )
    assert ["our client", -15] in [list(p) for p in by_url["https://x/hiring"]["parts"]]


def test_filter_fields_reach_the_card_and_the_drawer(client, monkeypatch):
    """The filters are the product surface these columns exist for. `state` is a closed
    set of USPS codes, `apply_via` is a word rather than the stored 0/1 (facet_counts
    skips falsy values, so an integer 0 meaning "we checked, it's an aggregator" would
    vanish), and `salary_min` is annualised USD."""
    _seed(
        [
            _job("Role A", url="https://x/a", state="Ohio", direct_apply=True,
                 salary="$140,000", salary_min=140000, salary_period="year",
                 salary_currency="USD"),
            _job("Role B", url="https://x/b", state="Ontario", direct_apply=False,
                 salary="$170/hr", salary_min=170, salary_period="hour",
                 salary_currency="USD"),
        ]
    )
    _mark_fresh(["role"])
    _no_fetch(monkeypatch)
    d = client.post("/api/score", json={"titles": ["role"]}).json()
    by = {j["title"]: j for j in d["jobs"]}

    assert by["Role A"]["state"] == "OH"  # spelled-out name folded onto the code
    assert by["Role B"]["state"] == ""  # foreign subdivision is not a US state
    assert by["Role A"]["apply_via"] == "employer"
    assert by["Role B"]["apply_via"] == "aggregator"
    # $170/hr is a $353,600 job, not a $170 one — the whole reason for annualising.
    assert by["Role B"]["salary_min"] == 170 * 2080
    assert by["Role A"]["salary_min"] == 140000
    assert d["facets"]["state"] == {"OH": 1}
    assert d["facets"]["apply_via"] == {"employer": 1, "aggregator": 1}


def test_a_foreign_currency_posting_never_reaches_the_board(client, monkeypatch):
    """jobfitr is US and USD only. A yen figure sorted as dollars is what put a
    JPY 15,500,000 job at the top of the live pool; the row is now dropped at intake
    rather than shown with an unusable salary."""
    _seed([_job("Yen Role", url="https://x/y", salary="¥15.5M", salary_min=15500000,
                salary_period="year", salary_currency="JPY")])
    _seed([_job("Dollar Role", url="https://x/d", salary="$150,000", salary_min=150000,
                salary_period="year", salary_currency="USD")])
    _mark_fresh(["role"])
    _no_fetch(monkeypatch)
    d = client.post("/api/score", json={"titles": ["role"]}).json()
    titles = [j["title"] for j in d["jobs"]]
    assert "Yen Role" not in titles
    assert "Dollar Role" in titles


# ── the 2026-08-11 panel blockers ────────────────────────────────────────────
def test_exclude_hides_by_whole_word_not_substring(client, monkeypatch):
    """B3. The exclude path is the most destructive thing this code does — it deletes a
    listing rather than ranking it — and it was the least precise test in the codebase.
    Measured on the frozen corpus: 220 of 2,573 exclusions (8.6%) were false hides,
    13 of them EXACT title matches, including one on the owner's own profile."""
    _seed(
        [
            _job("Software Engineer, Internal Systems", url="https://x/internal"),
            _job("AI Engineer - Salesforce Health Cloud", url="https://x/sfdc"),
            _job("Engineering Intern", url="https://x/intern"),
        ]
    )
    _mark_fresh(["engineer"])
    _no_fetch(monkeypatch)
    d = client.post(
        "/api/score", json={"titles": ["engineer"], "exclude": ["intern", "sales"]}
    ).json()
    titles = {j["title"] for j in d["jobs"]}
    assert "Software Engineer, Internal Systems" in titles  # 'intern' in 'Internal'
    assert "AI Engineer - Salesforce Health Cloud" in titles  # 'sales' in 'Salesforce'
    assert "Engineering Intern" not in titles  # the TRUE positive still hides


def test_a_remote_job_survives_dedupe_against_a_longer_nonremote_twin(
    client, monkeypatch
):
    """B2. `_dedupe_listings` collapses on (company, title) keeping the longest body —
    a tiebreak blind to whether the survivor is one the user can receive. Deduping
    before filtering therefore deleted the remote copy and then filtered out the twin
    that replaced it. Measured: 54 jobs restored across 14 of 20 remote-only profiles,
    0 lost."""
    _seed(
        [
            _job(
                "Data Engineer",
                text="x" * 4000,  # the LONGER body, so dedupe would prefer it
                url="https://x/onsite",
                location="Austin, TX",
                remote_type="onsite",
                remote_basis="stated",
            ),
            _job(
                "Data Engineer",
                text="short",
                url="https://x/remote",
                location="Remote",
                remote_type="remote",
                remote_basis="stated",
            ),
        ]
    )
    _mark_fresh(["data engineer"])
    _no_fetch(monkeypatch)
    d = client.post(
        "/api/score", json={"titles": ["data engineer"], "location": "remote"}
    ).json()
    urls = {j["url"] for j in d["jobs"]}
    assert "https://x/remote" in urls, "the remote job was eaten by dedupe"
