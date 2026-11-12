"""The daily limits on the metered conversation, and what a visitor sees when the money runs out.

Every test here goes through the REAL endpoints with the rate limiter left ON. The chat
fixture in test_chat.py turns the limiter off; these caps are separate code and must be
shown working without anything stubbed away. The only fake is the model: `chat._call`
(the non-streaming path), or an `httpx.MockTransport` where the HTTP status itself is the
thing under test. Nothing here reaches OpenRouter.

Each test uses its own documentation-range address (RFC 5737, 203.0.113.0/24) so the rate
limiter's in-memory counts, which persist across tests, never couple two tests together.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from jobfitr import chat, server


def _reply(text, cost=0.0):
    return {"choices": [{"message": {"content": text}}], "model": "test",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": cost}}


@pytest.fixture
def model(monkeypatch):
    """A fake model that answers every call; `cost` is what each call reports."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(chat, "store_glimpse", lambda force=False: "")
    monkeypatch.setattr(chat, "ALLOW_CLIENT_HISTORY", False)
    state = {"cost": 0.0, "payloads": []}

    async def fake(payload):
        state["payloads"].append(payload)
        return _reply("Where do you live?", state["cost"])

    monkeypatch.setattr(chat, "_call", fake)
    assert server.limiter.enabled
    return state


def _client(addr):
    return TestClient(server.app, client=(addr, 50000))


def _say(c, msg="I drive a forklift", stream=False):
    path = "/api/chat/stream" if stream else "/api/chat"
    return c.post(path, json={"session": None, "message": msg})


def _frames(r):
    return [json.loads(f[5:]) for f in r.text.split("\n\n") if f.startswith("data:")]


# ── 1. per-address turn cap ──────────────────────────────────────────────────────
def test_one_address_is_stopped_at_its_own_cap_and_another_is_not(model, monkeypatch):
    monkeypatch.setattr(chat, "PER_ADDRESS_DAILY", 3)
    a, b = _client("203.0.113.10"), _client("203.0.113.11")
    assert [_say(a).status_code for _ in range(3)] == [200, 200, 200]
    r = _say(a)
    assert r.status_code == 429 and r.json()["detail"] == "address_daily_cap"
    # The stream endpoint shares the count: it is the one the page calls.
    r = _say(a, stream=True)
    assert r.status_code == 429 and r.json()["detail"] == "address_daily_cap"
    # Someone else is untouched, on both endpoints.
    assert _say(b).status_code == 200
    assert _frames(_say(b, stream=True))[-1]["type"] == "done"


def test_a_refused_turn_never_reaches_the_model(model, monkeypatch):
    monkeypatch.setattr(chat, "PER_ADDRESS_DAILY", 1)
    a = _client("203.0.113.12")
    _say(a)
    calls = len(model["payloads"])
    assert _say(a).status_code == 429
    assert len(model["payloads"]) == calls


def test_the_address_caps_reset_with_the_day(model, monkeypatch):
    monkeypatch.setattr(chat, "PER_ADDRESS_DAILY", 1)
    a = _client("203.0.113.13")
    _say(a)
    assert _say(a).status_code == 429
    chat._usage["date"] = "1999-01-01"
    assert _say(a).status_code == 200


# ── 2. the spend ceilings ───────────────────────────────────────────────────────────
def test_the_site_stops_at_its_daily_spend(model, monkeypatch):
    monkeypatch.setattr(chat, "DAILY_SPEND_USD", 1.0)
    monkeypatch.setattr(chat, "PER_ADDRESS_SPEND_USD", 100.0)
    model["cost"] = 0.6
    a = _client("203.0.113.20")
    assert _say(a).status_code == 200            # $0.60
    assert _say(a).status_code == 200            # $1.20: started under the cap, so it ran
    assert chat._usage["spend"] == pytest.approx(1.2)
    r = _say(_client("203.0.113.21"))          # ANYONE is refused now: it is the site's cap
    assert r.status_code == 503 and r.json()["detail"] == "daily_spend"
    r = _say(a, stream=True)
    assert r.status_code == 503 and r.json()["detail"] == "daily_spend"


def test_one_address_cannot_spend_the_sites_day(model, monkeypatch):
    monkeypatch.setattr(chat, "DAILY_SPEND_USD", 10.0)
    monkeypatch.setattr(chat, "PER_ADDRESS_SPEND_USD", 0.5)
    model["cost"] = 0.3
    a, b = _client("203.0.113.22"), _client("203.0.113.23")
    assert _say(a).status_code == 200 and _say(a).status_code == 200   # $0.60
    r = _say(a)
    assert r.status_code == 429 and r.json()["detail"] == "address_daily_cap"
    assert _say(b).status_code == 200


def test_spend_is_counted_per_model_call_not_per_finished_turn(model, monkeypatch):
    # A turn that searches makes several calls; all of them cost money.
    monkeypatch.setattr(chat, "search_jobs", lambda a: {"returned": 0, "jobs": []})
    replies = iter([
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": "search_jobs", "arguments": '{"titles": ["Driver"]}'}}]}}],
         "model": "test", "usage": {"cost": 0.25}},
        _reply("I found nothing; tell me more.", 0.25),   # the nudge's answer
        _reply("Nothing passed.", 0.25),
    ])

    async def fake(payload):
        return next(replies)

    monkeypatch.setattr(chat, "_call", fake)
    _say(_client("203.0.113.24"))
    assert chat._usage["spend"] == pytest.approx(0.75)


def test_a_cost_that_is_missing_or_junk_counts_as_nothing():
    chat._usage["date"] = ""
    for junk in (None, "", "abc", float("nan"), -1, {"x": 1}):
        chat.note_spend(junk, "203.0.113.25")
    assert chat._usage["spend"] == 0.0


# ── 3. the account is out of credit, or the key is refused ──────────────────────────
def _upstream(monkeypatch, status, body=b'{"error":{"message":"Insufficient credits"}}',
              content_type="application/json"):
    """Route the REAL `_call`/`_model` HTTP through a MockTransport answering `status`."""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    real = httpx.AsyncClient
    monkeypatch.setattr(chat.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(chat, "store_glimpse", lambda force=False: "")
    real_sleep = asyncio.sleep                  # the retry's 1 s pause, made instant
    monkeypatch.setattr(chat.asyncio, "sleep", lambda s, *a, **k: real_sleep(0, *a, **k))
    return seen


@pytest.mark.parametrize("stream_mode", [False, True])
@pytest.mark.parametrize("status", [402, 401])
def test_an_account_refusal_says_unavailable_and_leaves_the_conversation_clean(
        monkeypatch, caplog, status, stream_mode):
    monkeypatch.setattr(chat, "STREAM", stream_mode)
    seen = _upstream(monkeypatch, status)
    c = _client(f"203.0.113.{30 + status % 10 + (2 if stream_mode else 0)}")
    with caplog.at_level(logging.WARNING, logger="jobfitr"):
        d = _say(c).json()
    assert d["error"] == "service_unavailable" and d["reply"] == ""
    assert len(seen) == 1                       # not retried: a retry cannot add credit
    assert any(f"HTTP {status}" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)
    st = chat._sessions[d["session"]]
    assert st["convo"] == []                    # no half-written turn left behind
    assert st["turns"] == 0                     # and their turn was not spent
    assert not st["lock"].locked()


def test_the_stream_reports_an_account_refusal_as_its_own_error(monkeypatch):
    monkeypatch.setattr(chat, "STREAM", True)
    _upstream(monkeypatch, 402)
    frames = _frames(_say(_client("203.0.113.40"), stream=True))
    assert frames[-1] == {"type": "error", "error": "service_unavailable"}
    st = chat._sessions[frames[0]["id"]]       # the page's endpoint leaves it clean too
    assert st["convo"] == [] and st["turns"] == 0 and not st["lock"].locked()


def test_a_402_arriving_inside_the_stream_is_caught_too(monkeypatch):
    # OpenRouter can answer 200 and then put the error in the event stream.
    monkeypatch.setattr(chat, "STREAM", True)
    body = b'data: {"error": {"code": 402, "message": "Insufficient credits"}}\n\n'
    _upstream(monkeypatch, 200, body=body, content_type="text/event-stream")
    frames = _frames(_say(_client("203.0.113.41"), stream=True))
    assert frames[-1] == {"type": "error", "error": "service_unavailable"}


def test_after_a_top_up_the_same_conversation_carries_on_cleanly(monkeypatch):
    monkeypatch.setattr(chat, "STREAM", False)
    _upstream(monkeypatch, 402)
    c = _client("203.0.113.42")
    sid = _say(c).json()["session"]
    calls = []

    async def topped_up(payload):
        calls.append(payload)
        return _reply("Where do you live?")

    monkeypatch.setattr(chat, "_call", topped_up)
    d = c.post("/api/chat", json={"session": sid, "message": "I drive a forklift"}).json()
    assert d["reply"] == "Where do you live?" and d["session"] == sid
    # The failed turn left nothing behind: one system prompt, one user message.
    assert [m["role"] for m in calls[0]["messages"]] == ["system", "user"]


def test_an_ordinary_upstream_fault_is_still_retried(monkeypatch):
    # The account path must not swallow the transient one: a 503 is retried (four tries).
    monkeypatch.setattr(chat, "STREAM", False)
    monkeypatch.setattr(chat, "RETRY_WAITS", (0, 0, 0))
    seen = _upstream(monkeypatch, 503, body=b"{}")
    d = _say(_client("203.0.113.43")).json()
    assert len(seen) == 4 and d["error"].startswith("upstream:")


# ── 5. memory bound and nothing identifying ─────────────────────────────────────────
def test_the_address_table_is_bounded_and_forgets_the_oldest(monkeypatch):
    monkeypatch.setattr(chat, "MAX_ADDRESSES", 3)
    chat._usage["date"] = ""
    for i in range(50):
        chat.note_request(f"198.51.100.{i}")
    assert len(chat._usage["addrs"]) == 3
    chat.note_request("198.51.100.47")          # touched again: now the newest
    chat.note_request("198.51.100.99")          # so 48 is the one forgotten
    assert chat._usage["addrs"][chat._addr_key("198.51.100.47")][0] == 2
    assert chat._addr_key("198.51.100.48") not in chat._usage["addrs"]
    assert len(chat._usage["addrs"]) == 3


def test_the_counter_holds_no_address(model, monkeypatch):
    addr = "203.0.113.77"
    _say(_client(addr))
    held = repr(chat._usage)
    assert addr not in held and "203.0.113" not in held
    # ...and the address never reaches the model provider either.
    assert addr not in json.dumps(model["payloads"])


def test_no_log_line_carries_the_address(model, monkeypatch, caplog):
    # Everything that writes a log line in the chat path, run from one address: turns,
    # a per-address refusal, a rate-limit refusal (slowapi logs the key at WARNING), and
    # an upstream fault. Root is captured at DEBUG so nothing is missed by level.
    addr = "203.0.113.88"
    monkeypatch.setattr(chat, "PER_ADDRESS_DAILY", 3)
    c = _client(addr)
    with caplog.at_level(logging.DEBUG):
        fake = chat._call
        monkeypatch.setattr(chat, "_call", _raise_402)
        assert _say(c).json()["error"] == "service_unavailable"     # refused, so given back
        monkeypatch.setattr(chat, "_call", fake)
        first = [_say(c).status_code for _ in range(4)]            # 1, 2, 3, then the cap
        monkeypatch.setattr(chat, "PER_ADDRESS_DAILY", 1000)
        statuses = [_say(c).status_code for _ in range(25)]        # past CHAT_RATE_LIMIT
    assert first == [200, 200, 200, 429]
    assert 429 in statuses
    assert any("exceeded" in r.getMessage() for r in caplog.records), \
        "the rate limiter's line was not captured, so this test proved nothing about it"
    leaked = [r.getMessage() for r in caplog.records if addr in r.getMessage()]
    assert leaked == []


async def _raise_402(payload):
    req = httpx.Request("POST", chat.OPENROUTER_URL)
    raise httpx.HTTPStatusError("402", request=req, response=httpx.Response(402, request=req))


def test_the_request_log_line_has_its_address_blanked(caplog):
    # uvicorn writes this line for every request. Emitted here exactly as uvicorn 0.52
    # does (h11_impl / httptools_impl), since TestClient never runs uvicorn.
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "203.0.113.99:51234", "POST", "/api/chat", "1.1", 200)
    msg = caplog.records[-1].getMessage()
    assert "203.0.113.99" not in msg and '"POST /api/chat HTTP/1.1" 200' in msg


# ═══════════════════════════════════════════════════════════════════════════════════
# Second round: what the review of the first version broke (parallel turns past the
# dollar caps, a 402 forgetting delivered picks, an outage spending allowances).
# ═══════════════════════════════════════════════════════════════════════════════════
def _call_reply(content=None, cost=0.0, tool=None):
    m = {"role": "assistant", "content": content}
    if tool:
        name, args, tid = tool
        m["tool_calls"] = [{"id": tid, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]
    return {"choices": [{"message": m}], "model": "test", "usage": {"cost": cost}}


def _fake_tools(name, args, state):
    """search finds one posting; recommend delivers it. Stands in for the real tools,
    whose own checks are tested in test_chat.py."""
    if name == "search_jobs":
        return {"returned": 1, "jobs": [{"url": "https://ex.test/j1", "title": "Driver"}]}
    if name == "recommend":
        return {"delivered": 1, "picks": [{"url": "https://ex.test/j1", "title": "Driver"}],
                "ruled_out": [], "brief": []}
    return {"error": "no tool"}


def _script(monkeypatch, steps):
    """`_call` answers from `steps`; the string "402" raises an account refusal."""
    it = iter(steps)

    async def fake(payload):
        x = next(it)
        if x == "402":
            await _raise_402(payload)
        return x

    monkeypatch.setattr(chat, "_call", fake)
    monkeypatch.setattr(chat, "_run_tool", _fake_tools)


_SEARCH = _call_reply(cost=0.29, tool=("search_jobs", {"titles": ["Driver"]}, "t1"))
_RECOMMEND = _call_reply(cost=0.29, tool=("recommend", {"picks": [{"url": "https://ex.test/j1"}]}, "t2"))


# ── finding 1: parallel turns ──────────────────────────────────────────────────────────
def test_parallel_turns_from_one_address_cannot_run_past_its_dollar_cap(model, monkeypatch):
    # The review's repro: 20 at once, a model at $0.29 a call taking 0.2 s. It was $5.80
    # against a $2.50 cap.
    monkeypatch.setattr(chat, "PER_ADDRESS_SPEND_USD", 2.5)
    monkeypatch.setattr(chat, "DAILY_SPEND_USD", 100.0)
    monkeypatch.setattr(chat, "PER_ADDRESS_DAILY", 1000)

    async def slow(payload):
        await asyncio.sleep(0.2)
        return _reply("ok", 0.29)

    monkeypatch.setattr(chat, "_call", slow)
    monkeypatch.setattr(server.limiter, "enabled", False)   # 20+ requests: the cap under test is not the rate limit
    addr = "203.0.113.110"

    async def burst(n):
        tr = httpx.ASGITransport(app=server.app, client=(addr, 1))
        async with httpx.AsyncClient(transport=tr, base_url="http://t") as c:
            return await asyncio.gather(*[
                c.post("/api/chat", json={"session": None, "message": "hi"}) for _ in range(n)])

    rs = asyncio.run(burst(20))
    codes = [r.status_code for r in rs]
    assert codes.count(200) == chat.PER_ADDRESS_CONCURRENT == 2
    assert {r.json()["detail"] for r in rs if r.status_code == 429} == {"address_busy"}
    assert chat._inflight == {}                 # every running mark was released
    # Burst after burst, the address stops within one call per running turn of its cap.
    for _ in range(9):                          # 9 x 2 = 18 more chances
        asyncio.run(burst(2))
    spend = chat._usage["addrs"][chat._addr_key(addr)][1]
    assert 2.5 <= spend <= 2.5 + chat.PER_ADDRESS_CONCURRENT * 0.29 + 1e-9


def test_a_second_address_can_run_while_the_first_is_at_its_limit(model, monkeypatch):
    for _ in range(chat.PER_ADDRESS_CONCURRENT):
        chat.start_running("203.0.113.111")
    r = _say(_client("203.0.113.111"))
    assert r.status_code == 429 and r.json()["detail"] == "address_busy"
    assert _say(_client("203.0.113.112")).status_code == 200


def test_a_running_mark_that_is_never_released_expires(monkeypatch):
    # A stream dropped before it started never reaches its `finally`.
    addr = "203.0.113.113"
    for _ in range(chat.PER_ADDRESS_CONCURRENT):
        chat.start_running(addr)
    assert chat.too_many_running(addr)
    real = chat.time.time
    monkeypatch.setattr(chat.time, "time", lambda: real() + chat.INFLIGHT_TTL + 1)
    assert not chat.too_many_running(addr)
    assert chat._inflight == {}


def test_the_dollar_cap_is_rechecked_before_every_model_call(model, monkeypatch):
    # Search ($0.29) then recommend ($0.29) delivers; the closing note's call would take
    # the address past $0.50, so the turn stops there and keeps the picks.
    monkeypatch.setattr(chat, "PER_ADDRESS_SPEND_USD", 0.5)
    _script(monkeypatch, [_SEARCH, _RECOMMEND, _call_reply("closing", 0.29)])
    c = _client("203.0.113.114")
    d = _say(c, "I drive a forklift, that's everything, search now").json()
    assert d["error"] == "address_daily_cap"
    assert [p["url"] for p in d["picks"]] == ["https://ex.test/j1"]
    assert chat._usage["addrs"][chat._addr_key("203.0.113.114")][1] == pytest.approx(0.58)
    st = chat._sessions[d["session"]]
    assert any(m.get("role") == "tool" and "ex.test/j1" in m["content"] for m in st["convo"])


def test_the_site_cap_reached_mid_turn_stops_it_on_the_stream_too(model, monkeypatch):
    monkeypatch.setattr(chat, "DAILY_SPEND_USD", 0.3)
    _script(monkeypatch, [_SEARCH, _RECOMMEND])
    frames = _frames(_say(_client("203.0.113.115"), "drive, search now", stream=True))
    assert frames[-1]["type"] == "done" and frames[-1]["result"]["error"] == "daily_spend"


# ── finding 2: a 402 after picks were delivered ───────────────────────────────────────
@pytest.mark.parametrize("stream", [False, True])
def test_a_402_after_delivery_keeps_the_answer_and_a_follow_up_sees_it(model, monkeypatch, stream):
    _script(monkeypatch, [_SEARCH, _RECOMMEND, "402"])
    c = _client(f"203.0.113.{120 + stream}")
    r = _say(c, "I drive a forklift, that's everything, search now", stream=stream)
    if stream:
        frames = _frames(r)
        assert "picks" in [f["type"] for f in frames]
        assert frames[-1]["type"] == "done"     # it ENDS with the answer, not an error frame
        d = frames[-1]["result"]
    else:
        d = r.json()
    assert d["error"] == "service_unavailable"
    assert [p["url"] for p in d["picks"]] == ["https://ex.test/j1"]
    sid = d["session"]
    assert chat._sessions[sid]["turns"] == 1     # an answer was given: the turn counts
    # After a top-up the next message reaches the model with the earlier picks in it.
    seen = []

    async def topped_up(payload):
        seen.append(payload)
        return _reply("The Driver job is the closest.")

    monkeypatch.setattr(chat, "_call", topped_up)
    d2 = c.post("/api/chat", json={"session": sid, "message": "tell me more about that one"}).json()
    assert d2["reply"] == "The Driver job is the closest."
    sent = json.dumps(seen[0]["messages"])
    assert "ex.test/j1" in sent and "recommend" in sent


def test_a_402_after_a_search_but_before_delivery_keeps_the_search(model, monkeypatch):
    _script(monkeypatch, [_SEARCH, "402"])
    d = _say(_client("203.0.113.123"), "drive, search now").json()
    assert d["error"] == "service_unavailable"
    st = chat._sessions[d["session"]]
    assert any(m.get("role") == "tool" for m in st["convo"])   # as the generic path keeps it
    assert st["turns"] == 0                                    # nothing delivered: given back


# ── finding 3: an outage must not spend anyone's allowance ────────────────────────────
@pytest.mark.parametrize("stream", [False, True])
def test_an_outage_gives_back_the_address_and_site_turns(model, monkeypatch, stream):
    monkeypatch.setattr(chat, "PER_ADDRESS_DAILY", 3)
    monkeypatch.setattr(chat, "_call", _raise_402)
    addr = f"203.0.113.{130 + stream}"
    c = _client(addr)
    for _ in range(6):                          # twice the cap, all refused upstream
        assert _say(c, stream=stream).status_code == 200
    assert chat._usage["count"] == 0
    assert chat._usage["addrs"][chat._addr_key(addr)][0] == 0
    monkeypatch.setattr(chat, "_call", lambda p: asyncio.sleep(0, result=_reply("Hi")))
    assert _say(c).status_code == 200           # after the top-up they are not locked out


def test_a_turn_that_delivered_before_the_402_is_not_given_back(model, monkeypatch):
    _script(monkeypatch, [_SEARCH, _RECOMMEND, "402"])
    _say(_client("203.0.113.132"), "drive, search now")
    assert chat._usage["count"] == 1


# ── finding 4: the defaults, and the arithmetic they rest on ──────────────────────────
def test_the_defaults_are_sized_for_a_shared_network():
    import math
    import os
    if any(os.environ.get(v) for v in ("CHAT_PER_ADDRESS_DAILY", "CHAT_PER_ADDRESS_SPEND_USD",
                                       "CHAT_DAILY_SPEND_USD", "CHAT_PER_ADDRESS_CONCURRENT")):
        pytest.skip("overridden in this environment")
    assert (chat.PER_ADDRESS_DAILY, chat.PER_ADDRESS_SPEND_USD, chat.DAILY_SPEND_USD) == (50, 4.0, 10.0)
    assert chat.PER_ADDRESS_DAILY // 5 == 10                      # ten five-turn conversations
    assert int(chat.PER_ADDRESS_SPEND_USD // 0.32) == 12          # dearest measured conversation
    assert math.ceil(chat.DAILY_SPEND_USD / chat.PER_ADDRESS_SPEND_USD) == 3   # networks per site day
    # Fifty turns that each search would outspend the site; the dollar cap is what binds.
    assert chat.PER_ADDRESS_DAILY * 0.29 > chat.DAILY_SPEND_USD
