"""jobfitr free-model bakeoff — a reviewable harness that empirically picks the
best FREE OpenRouter model for jobfitr's two AI jobs.

Two roles, two scoring methods (the core design decision):
  - applying  → structured JSON extraction into the config_from_dict contract.
                Has GROUND TRUTH → deterministic scorer (scoring.py). No LLM judge.
  - asking    → the chat interviewer. Subjective → user-simulator + both-order
                LLM judge + Bradley-Terry ranking + Cohen's kappa (rank.py).

Everything runs against OpenRouter's OpenAI-compatible endpoint via client.py,
iterating the candidate slugs in models.yaml. The harness imports the PRODUCTION
prompt + tool from jobfitr.chat so the eval measures the real task, not a proxy.

See PLAN.md for the full build plan and README.md for the methodology.
"""

from __future__ import annotations

__all__ = ["client", "scoring"]
