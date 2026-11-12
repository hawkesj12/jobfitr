"""Suite-wide guards: no test may reach the network or leak a conversation into the next.

Three things in the conversation path would otherwise do one or the other, and each fails
quietly rather than loudly:

* `checks.dead_urls` asks every chosen posting whether it is still up. In a test that is
  a real GET to whatever url the fixture row carries.
* `chat._model` streams from OpenRouter unless told not to. The tests replace `chat._call`,
  which the streaming path never calls, so a test would pass its fake and then dial out.
* `chat._sessions` is module state. A conversation left behind by one test is a transcript
  the next one inherits.
"""

from __future__ import annotations

import pytest

from jobfitr import chat, checks


@pytest.fixture(autouse=True)
def _no_network_no_leftovers(monkeypatch):
    monkeypatch.setattr(checks, "dead_urls", lambda urls: set())
    monkeypatch.setattr(chat, "STREAM", False)
    chat._sessions.clear()
    yield
    chat._sessions.clear()
