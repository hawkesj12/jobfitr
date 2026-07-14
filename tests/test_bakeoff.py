"""Bakeoff tests — zero real network, same discipline as test_chat: the client's
network boundary (client._http_post) is monkeypatched, so nothing here hits the
wire. Covers the scorer math, client retry/parse, and run_applying end-to-end.
"""

from __future__ import annotations

import json

import pytest

from bakeoff import client, scoring


# ── scorer: schema validity ───────────────────────────────────────────────────
def test_schema_valid_accepts_contract():
    assert scoring.schema_valid(
        {"titles": ["x"], "remote_only": True, "location": "Louisville, KY"}
    )


def test_schema_valid_rejects_unknown_key():
    # the gpt-oss spike shape: invented keys + nested object
    assert not scoring.schema_valid(
        {"job_titles": ["x"], "exclusions": {"intern": True}}
    )


def test_schema_valid_rejects_wrong_types():
    assert not scoring.schema_valid({"remote_only": "yes"})
    assert not scoring.schema_valid(
        {"titles": 123}
    )  # an int is not a valid titles list
    assert not scoring.schema_valid({"location": ["remote"]})


def test_schema_valid_rejects_empty_and_nondict():
    assert not scoring.schema_valid({})
    assert not scoring.schema_valid(None)
    assert not scoring.schema_valid("titles")


# ── scorer: field accuracy ────────────────────────────────────────────────────
def test_f1_perfect_and_partial():
    exp = {"titles": ["zookeeper", "animal keeper"]}
    perfect = scoring.score_case("c", {"titles": ["Zookeeper", "Animal Keeper"]}, exp)
    assert perfect.field_scores["titles"] == 1.0  # _clean_list normalizes casing
    half = scoring.score_case("c", {"titles": ["zookeeper", "vet"]}, exp)
    assert 0.0 < half.field_scores["titles"] < 1.0


def test_location_state_name_and_abbrev_match():
    # "Austin, Texas" (a fuller, arguably more correct form) must not be marked
    # wrong against gold "Austin, TX" — the methodology reviewer's finding.
    exp = {"location": "Austin, TX"}
    assert (
        scoring.score_case("c", {"location": "Austin, Texas"}, exp).field_scores[
            "location"
        ]
        == 1.0
    )
    assert (
        scoring.score_case("c", {"location": "austin tx"}, exp).field_scores["location"]
        == 1.0
    )
    assert (
        scoring.score_case("c", {"location": "Dallas, TX"}, exp).field_scores[
            "location"
        ]
        == 0.0
    )


def test_remote_only_omission_is_not_free_credit():
    # omitting remote_only must NOT auto-score 1.0 via bool(None)==False — that
    # would measure the default, not extraction (methodology reviewer's finding).
    exp = {"remote_only": False}
    omitted = scoring.score_case("c", {"titles": ["x"]}, exp)
    assert omitted.field_scores["remote_only"] == 0.0
    emitted = scoring.score_case("c", {"remote_only": False}, exp)
    assert emitted.field_scores["remote_only"] == 1.0


def test_scalar_exact_match():
    exp = {"location": "Louisville, KY", "remote_only": False}
    s = scoring.score_case(
        "c",
        {"location": "louisville, ky", "remote_only": False},
        exp,
    )
    assert s.field_scores["location"] == 1.0
    assert s.field_scores["remote_only"] == 1.0


def test_schema_validity_reported_separately_from_accuracy():
    # invented keys → schema invalid AND the real field wasn't extracted → 0 acc,
    # but the two signals are independent (schema_ok is its own column).
    s = scoring.score_case(
        "c", {"job_titles": ["zookeeper"]}, {"titles": ["zookeeper"]}, "zookeeper"
    )
    assert s.schema_ok is False
    assert s.overall == 0.0  # because titles wasn't extracted, not because of a gate


def test_partial_credit_when_some_fields_right_despite_bad_schema():
    # location is a bad type (schema invalid) but titles is extracted correctly →
    # the model still earns credit for titles; accuracy isn't punitively zeroed.
    s = scoring.score_case(
        "c",
        {"titles": ["zookeeper"], "location": ["oops-a-list"]},
        {"titles": ["zookeeper"], "location": "Louisville, KY"},
        "zookeeper in louisville",
    )
    assert s.schema_ok is False  # location wrong type
    assert s.field_scores["titles"] == 1.0
    assert 0.0 < s.overall < 1.0  # partial credit, not a punitive zero


def test_only_expected_fields_are_scored():
    s = scoring.score_case("c", {"titles": ["x"], "boosts": ["y"]}, {"titles": ["x"]})
    assert set(s.field_scores) == {"titles"}  # boosts not in gold → not scored


# ── scorer: hallucination ─────────────────────────────────────────────────────
def test_hallucination_flags_invented_terms():
    s = scoring.score_case(
        "c",
        {"titles": ["zookeeper"], "boosts": ["astronaut", "quantum physics"]},
        {"titles": ["zookeeper"]},
        transcript="I want to be a zookeeper",
    )
    assert s.hallucination > 0.5  # 2 of 3 items unsupported by the transcript


def test_hallucination_zero_when_supported():
    s = scoring.score_case(
        "c",
        {"titles": ["zookeeper"], "boosts": ["reptiles"]},
        {"titles": ["zookeeper"]},
        transcript="zookeeper working with reptiles, from staffing agencies",
    )
    assert s.hallucination == 0.0


# ── aggregate ─────────────────────────────────────────────────────────────────
def test_aggregate_separates_reliability_from_quality():
    # a non-responded case must NOT drag down quality — quality is computed over
    # responded cases only, with reliability tracked separately (the core design).
    scores = [
        scoring.score_case(
            "a", {"titles": ["x"]}, {"titles": ["x"]}, "x", responded=True
        ),
        scoring.score_case("b", {}, {"titles": ["y"]}, "y", responded=False),
    ]
    rep = scoring.aggregate("m", scores)
    assert rep.n == 2 and rep.n_responded == 1
    assert rep.reliability_rate == 0.5
    assert (
        rep.mean_field == 1.0
    )  # the one responded case scored perfectly; the miss doesn't count


def test_aggregate_rolls_up():
    scores = [
        scoring.score_case("a", {"titles": ["x"]}, {"titles": ["x"]}, "x"),
        scoring.score_case("b", {"bad_key": 1}, {"titles": ["y"]}, "y"),
    ]
    rep = scoring.aggregate("m", scores)
    assert rep.n == 2
    assert rep.schema_valid_rate == 0.5
    assert "titles" in rep.per_field


# ── client: parsing + retry, all mocked (zero network) ────────────────────────
@pytest.fixture(autouse=True)
def _no_waits(monkeypatch):
    monkeypatch.setattr(client, "MIN_INTERVAL", 0.0)
    monkeypatch.setattr(client, "_sleep", lambda s: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


def _ok_response(content="", tool_args=None, tokens=42):
    msg = {"content": content}
    if tool_args is not None:
        msg["tool_calls"] = [
            {"function": {"name": "set_config", "arguments": tool_args}}
        ]
    return 200, {"choices": [{"message": msg}], "usage": {"total_tokens": tokens}}, None


def test_client_parses_tool_call(monkeypatch):
    args = json.dumps({"titles": ["zookeeper"], "location": "Louisville, KY"})
    monkeypatch.setattr(
        client, "_http_post", lambda *a, **k: _ok_response(tool_args=args)
    )
    r = client.call(
        "some/model:free", [{"role": "user", "content": "hi"}], tools=[{"x": 1}]
    )
    assert r.ok
    assert r.tool_args()["titles"] == ["zookeeper"]
    assert r.tokens == 42


def test_client_retries_on_429_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, {"error": {"message": "rate limited"}}, 0  # Retry-After 0
        return _ok_response(content="ok")

    monkeypatch.setattr(client, "_http_post", flaky)
    r = client.call("m", [{"role": "user", "content": "x"}])
    assert r.ok and calls["n"] == 2


def test_client_retry_after_zero_still_retries_all_attempts(monkeypatch):
    # a 429 with Retry-After: 0 must NOT instant-exhaust retries in ~0s — it must
    # back off and use every attempt (the free-tier bug that mislabeled transient
    # 429s as hard failures).
    monkeypatch.setattr(client, "MAX_RETRIES", 3)
    calls = {"n": 0}

    def always_429_zero(*a, **k):
        calls["n"] += 1
        return 429, {"error": {"message": "rate"}}, 0  # Retry-After: 0

    monkeypatch.setattr(client, "_http_post", always_429_zero)
    r = client.call("m", [{"role": "user", "content": "x"}])
    assert not r.ok and calls["n"] == 3  # all attempts used, not collapsed to 1


def test_client_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(client, "MAX_RETRIES", 2)
    monkeypatch.setattr(
        client, "_http_post", lambda *a, **k: (500, {"error": "boom"}, None)
    )
    r = client.call("m", [{"role": "user", "content": "x"}])
    assert not r.ok and "retries" in (r.error or "")


def test_client_4xx_not_retried(monkeypatch):
    calls = {"n": 0}

    def once(*a, **k):
        calls["n"] += 1
        return 400, {"error": {"message": "bad request"}}, None

    monkeypatch.setattr(client, "_http_post", once)
    r = client.call("m", [{"role": "user", "content": "x"}])
    assert not r.ok and calls["n"] == 1  # 400 fails fast, no retry


# ── run_applying end-to-end, mocked client (zero network) ─────────────────────
def test_run_applying_ranks_good_over_bad(monkeypatch):
    from bakeoff import run_applying

    good = {"titles": ["zookeeper"], "location": "Louisville, KY"}

    # good model returns the right config; bad model returns the spike's junk
    # (both responded — a quality difference, not an infra one)
    def fake_extract(model, transcript):
        if model == "good":
            return dict(good), 0.5, 100, True, 0.0
        return {"job_titles": ["zookeeper"]}, 6.0, 500, True, 0.0  # schema-invalid

    monkeypatch.setattr(run_applying, "preflight", lambda m: True)
    monkeypatch.setattr(run_applying, "extract_one", fake_extract)
    cases = [{"id": "t1", "transcript": "zookeeper in louisville", "expected": good}]
    reports, unavailable = run_applying.run(["good", "bad"], cases)
    assert reports[0].model == "good"  # ranked first
    assert reports[0].schema_valid_rate == 1.0
    assert reports[1].schema_valid_rate == 0.0
    assert unavailable == []


def test_run_applying_unreachable_model_set_aside_not_scored(monkeypatch):
    from bakeoff import run_applying

    # endpoint fails preflight → the model is set aside as unavailable, NOT scored
    # as slow/low-quality. Free-tier congestion can't tank a model's ranking.
    monkeypatch.setattr(run_applying, "preflight", lambda m: False)
    cases = [
        {"id": "t1", "transcript": "zookeeper", "expected": {"titles": ["zookeeper"]}}
    ]
    reports, unavailable = run_applying.run(["down-model"], cases)
    assert reports == []
    assert unavailable == ["down-model"]


def test_run_applying_case_skipped_when_no_genuine_response(monkeypatch):
    from bakeoff import run_applying

    # reachable (preflight ok) but every case returns empty → cases are SKIPPED,
    # never scored as a punitive zero. Model ends up with no scorable cases.
    monkeypatch.setattr(run_applying, "preflight", lambda m: True)
    monkeypatch.setattr(
        run_applying, "extract_one", lambda m, t: ({}, 0.0, 0, False, 0.0)
    )
    cases = [
        {"id": "t1", "transcript": "zookeeper", "expected": {"titles": ["zookeeper"]}}
    ]
    reports, unavailable = run_applying.run(["flaky"], cases)
    # no scorable cases → not in the quality ranking; recorded as unavailable
    assert reports == [] and unavailable == ["flaky"]


# ── asking: user_sim loop (mocked, zero network) ──────────────────────────────
def test_user_sim_completes_when_fields_filled(monkeypatch):
    from bakeoff import user_sim

    # sim always says a line; candidate always tool-calls a complete config
    monkeypatch.setattr(user_sim, "_user_turn", lambda *a, **k: "I want zookeeper jobs")

    def fake_candidate(model, convo):
        return "Got it.", {"titles": ["zookeeper"], "location": "Louisville, KY"}

    monkeypatch.setattr(user_sim, "_candidate_turn", fake_candidate)
    persona = {
        "id": "p1",
        "persona": "a zookeeper",
        "required_fields": ["titles", "location"],
    }
    iv = user_sim.run_interview("m", "sim", persona)
    assert iv.turns_to_complete == 1  # completed on the first turn
    assert iv.fields_missed == []
    assert iv.config["titles"] == ["zookeeper"]


def test_user_sim_never_completes_reports_missed(monkeypatch):
    from bakeoff import user_sim

    monkeypatch.setattr(user_sim, "_user_turn", lambda *a, **k: "hi")
    monkeypatch.setattr(
        user_sim, "_candidate_turn", lambda m, c: ("hmm?", {})
    )  # never fills
    persona = {"id": "p1", "persona": "x", "required_fields": ["titles"]}
    iv = user_sim.run_interview("m", "sim", persona)
    assert iv.turns_to_complete is None
    assert iv.fields_missed == ["titles"]
    assert len(iv.transcript) == user_sim.MAX_TURNS * 2  # ran the full budget


# ── asking: judge both-order agreement ────────────────────────────────────────
def test_judge_counts_winner_only_when_orders_agree(monkeypatch):
    from bakeoff import judge

    # order1 (A=alpha,B=beta) says "A"; order2 (A=beta,B=alpha) says "B" → alpha wins
    calls = iter(["A", "B"])
    monkeypatch.setattr(judge, "_one_order", lambda *a, **k: next(calls))
    v = judge.judge_pair("j", "goal", "alpha", "txtA", "beta", "txtB", "p1")
    assert v.winner == "alpha"


def test_judge_position_bias_is_a_tie(monkeypatch):
    from bakeoff import judge

    # both orders say "A" → the judge just favors slot 1 → no winner
    monkeypatch.setattr(judge, "_one_order", lambda *a, **k: "A")
    v = judge.judge_pair("j", "goal", "alpha", "txtA", "beta", "txtB", "p1")
    assert v.winner is None


# ── asking: ranking + kappa math ──────────────────────────────────────────────
def test_bradley_terry_or_winrate_orders_dominant_first():
    from bakeoff import rank

    # alpha beats beta 3x, beta beats gamma 3x → alpha should top, gamma bottom
    verdicts = [("alpha", "beta")] * 3 + [("beta", "gamma")] * 3
    r = rank.bradley_terry(["alpha", "beta", "gamma"], verdicts)
    order = [row["model"] for row in r.order]
    assert order[0] == "alpha" and order[-1] == "gamma"


def test_cohen_kappa_perfect_and_chance():
    from bakeoff import rank

    perfect = [("A", "A"), ("B", "B"), ("A", "A")]
    assert rank.cohen_kappa(perfect) == 1.0
    disagree = [("A", "B"), ("B", "A")]
    assert rank.cohen_kappa(disagree) is not None and rank.cohen_kappa(disagree) < 0.5
    assert rank.cohen_kappa([]) is None
