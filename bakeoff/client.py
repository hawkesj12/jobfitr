"""The model-agnostic OpenRouter caller — the ONLY thing in the bakeoff that
hits the wire.

Mirrors the transport pattern already proven in job_radar/llm.py (stdlib urllib,
base_url + Bearer key, OpenAI-compatible /chat/completions) and adds the three
things a multi-model benchmark needs that a one-shot re-rank doesn't:

  1. Rate-limit discipline — OpenRouter free variants cap at ~20 requests/minute
     (a live 429 confirmed this). Every call is spaced by MIN_INTERVAL and, on a
     429/5xx, retried with exponential backoff that honors the Retry-After header.
  2. Measurement — every call records wall-clock latency and usage.total_tokens,
     the two things that decide the fast-vs-accurate trade-off.
  3. Flexibility — the same call() drives tool-calling (asking role) and
     response_format json_schema (applying role), because we MEASURE whether a
     free model actually honors either.

Network boundary: `_http_post` is the only code that touches urllib, so tests
monkeypatch it and run with zero real network (same discipline as test_chat).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

BASE_URL = "https://openrouter.ai/api/v1"
CHAT_URL = f"{BASE_URL}/chat/completions"
API_KEY_ENV = "OPENROUTER_API_KEY"

# Spacing between calls to stay under the ~20 rpm free-variant burst limit.
# ~3.5s ≈ 17/min with headroom. Tests set this to 0.
MIN_INTERVAL = float(os.environ.get("BAKEOFF_MIN_INTERVAL", "3.2"))
# Fail fast on a congested free endpoint: a model that 429s repeatedly is a
# RELIABILITY result, not something to wait out. 3 retries with a capped backoff
# gives up in ~15s instead of stalling the whole run for two minutes.
MAX_RETRIES = int(os.environ.get("BAKEOFF_MAX_RETRIES", "3"))
BACKOFF_CAP = float(os.environ.get("BAKEOFF_BACKOFF_CAP", "8"))
DEFAULT_TIMEOUT = float(os.environ.get("BAKEOFF_TIMEOUT", "60"))

_last_call = 0.0  # monotonic ts of the last request, for spacing


# ═══════════════════════════════════════════════════════════════
# load_dotenv()
# ═══════════════════════════════════════════════════════════════
# Minimal .env reader (no python-dotenv dep) so the bakeoff picks up
# OPENROUTER_API_KEY from the repo's gitignored .env the same way the app does.
# Only sets keys not already in the environment; never prints a value.
# ═══════════════════════════════════════════════════════════════
def load_dotenv(path: str | os.PathLike | None = None) -> None:
    p = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def api_key() -> str:
    return os.environ.get(API_KEY_ENV, "") or ""


@dataclass
class Result:
    """One model call's outcome. `ok` is True only on a parsed 2xx response."""

    ok: bool
    content: str = ""
    tool_calls: list = field(default_factory=list)  # [{"name","arguments"(str)}]
    latency: float = 0.0
    tokens: int = 0
    cost: float = (
        0.0  # USD for this call, from OpenRouter usage.cost (0 for free models)
    )
    status: int = 0
    error: str | None = None
    raw: dict = field(default_factory=dict)

    def tool_args(self) -> dict:
        """Parsed arguments of the first tool call, or {} if none/unparseable."""
        for tc in self.tool_calls:
            args = tc.get("arguments")
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                    return parsed if isinstance(parsed, dict) else {}
                except json.JSONDecodeError:
                    return {}
            if isinstance(args, dict):
                return args
        return {}


def _sleep(seconds: float) -> None:  # indirection so tests can neutralize waits
    if seconds > 0:
        time.sleep(seconds)


# ═══════════════════════════════════════════════════════════════
# _http_post()
# ═══════════════════════════════════════════════════════════════
# The sole network boundary. Returns (status_code, parsed_json_or_{},
# retry_after_seconds_or_None). Raises only on a transport error the caller
# turns into a retry. Monkeypatched in tests for zero-network runs.
# ═══════════════════════════════════════════════════════════════
def _http_post(url: str, headers: dict, body: dict, timeout: float):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        retry_after = e.headers.get("Retry-After") if e.headers else None
        try:
            payload = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            payload = {}
        ra = float(retry_after) if retry_after and retry_after.isdigit() else None
        return e.code, payload, ra


# ═══════════════════════════════════════════════════════════════
# call()
# ═══════════════════════════════════════════════════════════════
# One OpenAI-compatible chat completion against any OpenRouter model slug.
# Spaces requests (MIN_INTERVAL), retries 429/5xx with exponential backoff that
# honors Retry-After, and records latency + token usage. `tools` drives the
# asking role; `response_format` drives the applying role. Returns a Result —
# never raises for an API-level failure (ok=False carries the error).
# ═══════════════════════════════════════════════════════════════
def call(
    model: str,
    messages: list,
    tools: list | None = None,
    tool_choice: str | dict | None = None,
    response_format: dict | None = None,
    max_tokens: int = 512,
    temperature: float = 0.0,
    timeout: float = DEFAULT_TIMEOUT,
) -> Result:
    global _last_call

    key = api_key()
    if not key:
        return Result(ok=False, error="no OPENROUTER_API_KEY (run load_dotenv?)")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://jobfitr.app",
        "X-Title": "jobfitr-bakeoff",
    }
    body: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools is not None:
        body["tools"] = tools
        body["tool_choice"] = tool_choice or "auto"
    if response_format is not None:
        body["response_format"] = response_format

    last_err = "unknown"
    for attempt in range(MAX_RETRIES):
        # space requests under the burst limit
        gap = MIN_INTERVAL - (time.monotonic() - _last_call)
        _sleep(gap)
        _last_call = time.monotonic()

        t0 = time.monotonic()
        try:
            status, data, retry_after = _http_post(CHAT_URL, headers, body, timeout)
        except Exception as e:  # transport-level (timeout, conn reset) → retry
            last_err = f"{type(e).__name__}: {e}"
            _sleep(_backoff(attempt))
            continue
        latency = time.monotonic() - t0

        if status == 200:
            return _parse_ok(data, latency, status)

        # 429 / 5xx → transient, retry. Honor a MEANINGFUL Retry-After (capped so
        # a provider can't force a long stall), but a 0/absent Retry-After must
        # fall back to exponential backoff — never an instant retry loop that
        # exhausts attempts in ~0s and mislabels a transient 429 as a hard failure.
        last_err = _err_text(status, data)
        if status == 429 or 500 <= status < 600:
            wait = min(retry_after, BACKOFF_CAP) if retry_after else _backoff(attempt)
            _sleep(wait)
            continue
        # 4xx other than 429 → not retryable
        return Result(
            ok=False, status=status, error=last_err, raw=data, latency=latency
        )

    return Result(ok=False, error=f"exhausted {MAX_RETRIES} retries: {last_err}")


def _backoff(attempt: int) -> float:
    """Exponential backoff (2s, 4s, 8s…) capped at BACKOFF_CAP so a congested
    free endpoint fails fast instead of stalling the run."""
    return min(2.0 * (2**attempt), BACKOFF_CAP)


def _err_text(status: int, data: dict) -> str:
    err = (data or {}).get("error")
    if isinstance(err, dict):
        return f"{status}: {err.get('message', err)}"
    return f"{status}: {err or data}"


def _parse_ok(data: dict, latency: float, status: int) -> Result:
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return Result(ok=False, status=status, error="no choices in response", raw=data)
    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        fn = (tc or {}).get("function") or {}
        tool_calls.append(
            {"name": fn.get("name", ""), "arguments": fn.get("arguments")}
        )
    usage = data.get("usage") or {}
    tokens = int(usage.get("total_tokens") or 0)
    cost = float(usage.get("cost") or 0.0)  # OpenRouter reports actual USD spent
    return Result(
        ok=True,
        content=msg.get("content") or "",
        tool_calls=tool_calls,
        latency=latency,
        tokens=tokens,
        cost=cost,
        status=status,
        raw=data,
    )
