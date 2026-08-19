"""Prompts live as text files, not as Python string literals.

A prompt is the thing you actually ship to the model, and when it is spelled as forty
lines of implicit string concatenation you cannot read it without executing it. Every
prompt bug found in this repo so far — a stale instruction contradicting a new one, a
missing escape hatch, an instruction nobody noticed had two owners — was invisible in the
source and obvious the moment the rendered text was printed.

So: `.md` files here, plain Python there. Edit a prompt without touching a module, diff a
prompt as prose, and read exactly what the model receives.

They ship inside the wheel via [tool.setuptools.package-data], so the box gets them from
the installed package rather than from a checkout path that may not exist.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """Read a prompt by stem. Cached — prompts are immutable for a process's life.

    Raises rather than returning "" on a missing file: a silently empty system prompt is a
    model with no instructions at all, which fails as plausible-looking garbage instead of
    as an error.
    """
    p = _DIR / f"{name}.md"
    if not p.is_file():
        raise FileNotFoundError(f"prompt {name!r} not found at {p}")
    return p.read_text(encoding="utf-8").strip()


def render(name: str, **parts: str) -> str:
    """Load a prompt and substitute {placeholders} with other named prompts or values."""
    text = load(name)
    return text.format(**parts) if parts else text
