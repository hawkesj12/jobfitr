"""User-simulator — drives a reproducible interview so the ASKING role can be
benchmarked without a human in the loop.

A fixed, cheap sim model role-plays a persona (from cases/asking/*.yaml) while the
candidate ASKING model runs the REAL production interview: jobfitr.chat's
SYSTEM_PROMPT + SET_CONFIG_TOOL, with tool-call args merged via chat.merge_config
exactly as the live endpoint does. So the transcript reflects the true task, and
we get two objective signals for free alongside the judged quality:
  - turns_to_complete : how few turns to fill the required fields (lower = better)
  - fields_missed     : required fields still empty when the interview ended

The candidate uses non-streaming completions (the bakeoff doesn't need SSE); the
merged config is assembled from tool calls the same way stream_chat does.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bakeoff import client
from jobfitr.chat import SET_CONFIG_TOOL, SYSTEM_PROMPT, merge_config

MAX_TURNS = 6  # matches the production CHAT_MAX_TURNS default


@dataclass
class Interview:
    model: str
    persona_id: str
    transcript: list = field(default_factory=list)  # [{role, content}]
    config: dict = field(default_factory=dict)
    turns_to_complete: int | None = None  # None = never completed
    fields_missed: list = field(default_factory=list)
    error: str | None = None

    def as_text(self) -> str:
        who = {"user": "Candidate", "assistant": "Assistant"}
        return "\n".join(
            f"{who.get(m['role'], m['role'])}: {m['content']}" for m in self.transcript
        )


# ═══════════════════════════════════════════════════════════════
# _candidate_turn()
# ═══════════════════════════════════════════════════════════════
# One assistant turn from the candidate ASKING model: it sees the production
# system prompt + the running conversation, may emit text AND/OR a set_config
# tool call. Returns (assistant_text, config_delta).
# ═══════════════════════════════════════════════════════════════
def _candidate_turn(model: str, convo: list) -> tuple[str, dict]:
    r = client.call(
        model,
        [{"role": "system", "content": SYSTEM_PROMPT}, *convo],
        tools=[SET_CONFIG_TOOL],
        tool_choice="auto",
        max_tokens=320,
    )
    if not r.ok:
        return f"[error: {r.error}]", {}
    delta = {k: v for k, v in r.tool_args().items() if v is not None}
    text = r.content or ("Got it." if delta else "Could you tell me the role you want?")
    return text, delta


# ═══════════════════════════════════════════════════════════════
# _user_turn()
# ═══════════════════════════════════════════════════════════════
# One user turn from the sim model, role-playing the persona. It sees the persona
# brief as its system prompt and the conversation SO FAR (with roles swapped, so
# from its POV the candidate's questions are the 'user' it answers). Kept short
# and in-character; it must not volunteer everything at once (realism).
# ═══════════════════════════════════════════════════════════════
def _user_turn(sim_model: str, persona: str, convo: list, opening: bool) -> str:
    if opening:
        instruction = (
            "Open the conversation with a brief, natural first message stating the job "
            "you want, the way a real person would — do not list every preference at once."
        )
        swapped = []
    else:
        instruction = (
            "Reply in character to the assistant's last message. Answer only what was "
            "asked, briefly and naturally. Reveal preferences gradually, not all at once. "
            "If the assistant has clearly captured your search, say a short confirmation."
        )
        swapped = [
            {
                "role": "assistant" if m["role"] == "user" else "user",
                "content": m["content"],
            }
            for m in convo
        ]
    r = client.call(
        sim_model,
        [{"role": "system", "content": persona + "\n\n" + instruction}, *swapped],
        max_tokens=120,
    )
    return (r.content or "").strip() if r.ok else "..."


# ═══════════════════════════════════════════════════════════════
# run_interview()
# ═══════════════════════════════════════════════════════════════
# Simulate a full interview between the candidate ASKING model and the persona.
# Alternates user_sim → candidate for up to MAX_TURNS, merging tool-call config
# each turn. Marks completion once titles are present AND every required field is
# filled. Returns an Interview with transcript + the two objective signals.
# ═══════════════════════════════════════════════════════════════
def run_interview(model: str, sim_model: str, persona: dict) -> Interview:
    convo: list = []
    cfg: dict = {}
    required = persona.get("required_fields", ["titles"])
    persona_brief = persona["persona"]
    iv = Interview(model=model, persona_id=persona["id"])

    for turn in range(1, MAX_TURNS + 1):
        user_msg = _user_turn(sim_model, persona_brief, convo, opening=(turn == 1))
        convo.append({"role": "user", "content": user_msg})

        text, delta = _candidate_turn(model, convo)
        convo.append({"role": "assistant", "content": text})
        cfg = merge_config(cfg, delta)

        missed = [f for f in required if not cfg.get(f)]
        if not missed and cfg.get("titles"):
            iv.turns_to_complete = turn
            break

    iv.transcript = convo
    iv.config = cfg
    iv.fields_missed = [f for f in required if not cfg.get(f)]
    return iv
