"""Run the front-end facet tests as part of the ordinary `pytest` gate.

WHY THIS WRAPPER EXISTS. `tests/facets.test.mjs` tests the most destructive bug this front
end had — clicking one Field chip hid 168 of 200 jobs, ~136 of them for having NO value on
that facet rather than the wrong one. A test that only runs when someone remembers
`node --test` is a gate that never fires, and this repo has already been bitten by exactly
that shape: `verify-slot.sh`'s employer-diversity check stayed green for a week while
asserting a promise the system had stopped making.

It exercises the REAL `web/app.js` (evaluated in a `vm` with a Proxy DOM stub), not a
transcription of it, so it cannot drift from what ships.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).parent / "facets.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_front_end_facet_suite_passes():
    proc = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"web/app.js facet tests failed:\n{proc.stdout[-4000:]}\n{proc.stderr[-2000:]}"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_parses():
    """A syntax error in app.js is a blank board with a console trace and a green suite."""
    proc = subprocess.run(
        ["node", "--check", "web/app.js"], cwd=ROOT, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
