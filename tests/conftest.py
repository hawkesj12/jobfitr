"""Suite-wide guards: no test may reach the network or leak a conversation into the next.

Four things in the conversation path would otherwise do one or the other, and each fails
quietly rather than loudly:

* `checks.dead_urls` asks every chosen posting whether it is still up. In a test that is
  a real GET to whatever url the fixture row carries.
* `chat._model` streams from OpenRouter unless told not to. The tests replace `chat._call`,
  which the streaming path never calls, so a test would pass its fake and then dial out.
* `chat._sessions` is module state. A conversation left behind by one test is a transcript
  the next one inherits.
* `chat._usage` holds the day's turn, spend and per-address counts. Left alone, every
  test client shares one address and the suite would trip the per-address cap on its own.
"""

from __future__ import annotations

import pytest

from jobfitr import chat, checks


@pytest.fixture(autouse=True)
def _no_network_no_leftovers(monkeypatch):
    monkeypatch.setattr(checks, "dead_urls", lambda urls: set())
    monkeypatch.setattr(chat, "STREAM", False)
    chat._sessions.clear()
    chat._usage["date"] = ""     # the daily caps: an empty date makes the next check roll the day
    yield
    chat._sessions.clear()
    chat._usage["date"] = ""
