"""Both-order pairwise LLM judge for the ASKING role.

Conversation quality is subjective, so a fixed free judge model compares two
interviews of the SAME persona and picks the better one. The one non-negotiable:
every pair is judged in BOTH orders (A-then-B and B-then-A) and the verdict only
counts when both orders agree — this cancels the judge's well-documented
first-position bias, so we measure interview quality, not slot order. Ties (the
two orders disagree) are dropped, not guessed.

The judge model is fixed in models.yaml and MUST NOT be an entrant, so no model
ever judges itself. Output feeds rank.py (Bradley-Terry).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from bakeoff import client

_RUBRIC = (
    "You are evaluating two AI assistants (A and B) that each interviewed the SAME "
    "job-seeker to build a job search. Judge which conducted the BETTER interview:\n"
    "- captured the person's real intent (titles + what matters to them),\n"
    "- was warm, natural, and concise (not robotic, not an interrogation),\n"
    "- handled vague or rambling answers gracefully,\n"
    "- got there in few turns without missing key preferences.\n"
    'Respond with ONLY JSON: {"winner": "A" | "B" | "tie", "reason": "<=15 words"}.'
)


@dataclass
class Verdict:
    persona_id: str
    a: str  # model slug shown as A in the FIRST ordering
    b: str
    winner: str | None  # slug of the agreed winner, or None (tie/disagreement)


def _one_order(
    judge_model: str, persona_goal: str, first: str, second: str
) -> str | None:
    """Return 'A'/'B'/'tie' for one presentation order."""
    user = (
        f"JOB-SEEKER (ground truth of what they wanted):\n{persona_goal}\n\n"
        f"=== ASSISTANT A ===\n{first}\n\n=== ASSISTANT B ===\n{second}"
    )
    r = client.call(
        judge_model,
        [{"role": "system", "content": _RUBRIC}, {"role": "user", "content": user}],
        max_tokens=120,
    )
    if not r.ok:
        return None
    m = re.search(r"\{.*\}", r.content or "", re.S)
    if not m:
        return None
    try:
        verdict = json.loads(m.group(0)).get("winner", "").strip().upper()
    except json.JSONDecodeError:
        return None
    return verdict if verdict in ("A", "B", "TIE") else None


# ═══════════════════════════════════════════════════════════════
# judge_pair()
# ═══════════════════════════════════════════════════════════════
# Judge two interviews in BOTH orders. The winner counts only when the two
# orders agree (order 1 says A and order 2 — where the models are swapped — says
# B, i.e. the same model). Disagreement = tie (position bias detected) → winner
# None. Returns a Verdict naming the agreed winning slug.
# ═══════════════════════════════════════════════════════════════
def judge_pair(
    judge_model, persona_goal, model_a, text_a, model_b, text_b, persona_id
) -> Verdict:
    # order 1: A=model_a, B=model_b
    v1 = _one_order(judge_model, persona_goal, text_a, text_b)
    # order 2: swap — A=model_b, B=model_a
    v2 = _one_order(judge_model, persona_goal, text_b, text_a)

    winner = None
    if v1 == "A" and v2 == "B":
        winner = model_a  # both orders favor model_a
    elif v1 == "B" and v2 == "A":
        winner = model_b
    # any other combination (incl. ties / disagreement) → no counted winner
    return Verdict(persona_id=persona_id, a=model_a, b=model_b, winner=winner)
