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
    # The account path must not swallow the transient one: a 503 is retried once.
    monkeypatch.setattr(chat, "STREAM", False)
    seen = _upstream(monkeypatch, 503, body=b"{}")
    d = _say(_client("203.0.113.43")).json()
    assert len(seen) == 2 and d["error"].startswith("upstream:")


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
        assert _say(c).json()["error"] == "service_unavailable"     # turn 1
        monkeypatch.setattr(chat, "_call", fake)
        first = [_say(c).status_code for _ in range(3)]            # 2, 3, then the cap
        monkeypatch.setattr(chat, "PER_ADDRESS_DAILY", 1000)
        statuses = [_say(c).status_code for _ in range(25)]        # past CHAT_RATE_LIMIT
    assert first == [200, 200, 429]
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
