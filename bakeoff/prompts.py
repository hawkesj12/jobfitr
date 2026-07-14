"""The ONE canonical extraction prompt — shared verbatim by every lane.

Fairness is the whole point: the OpenRouter lane and the Claude Code lane MUST
give every model the exact same instructions, or a cross-lane comparison is
meaningless. An earlier version derived this by string-splitting the production
chat prompt, which silently grabbed the multi-turn INTERVIEW instructions (a
self-contradictory prompt for a one-shot task) — and the Claude Code lane used a
different, cleaner prompt that happened to spell out the exact phrase->value
mappings the gold cases probe (teaching to the test). Both bugs are fixed by
having a single, self-contained prompt here that:
  - describes the fields plainly,
  - gives ONLY general guidance (no case-specific 1:1 mappings), so interpreting
    "show me lots" or "open to remote too" is left to the model — that IS the test.
"""

from __future__ import annotations

# The field contract = the fields the production chat collects (jobfitr.chat.CONFIG_FIELDS).
# max_age_days (recency) and min_score (pickiness) are NOT here: the chat stopped asking
# for them and they're set deterministically downstream, so they aren't extraction targets.
EXTRACT_PROMPT = (
    "You extract a job seeker's search preferences from a COMPLETE chat transcript "
    "into a structured config. The whole transcript is provided at once, so extract "
    "EVERY field the transcript supports in a single pass.\n\n"
    "The config fields (include only fields the transcript actually supports; omit "
    "any the user never mentioned — never invent a value):\n"
    "- titles: the roles the user wants (list of strings)\n"
    "- boosts: signals that should rank a job HIGHER — skills, tools, industry, a nearby city (list)\n"
    "- exclude: title words that should HIDE a job entirely, e.g. intern/volunteer (list)\n"
    "- rank_down: signals that should SINK a job but not hide it, e.g. staffing/agency (list)\n"
    "- location: a place string, or 'remote', or 'anywhere'\n"
    "- remote_only: boolean — true only if the user wants remote-only roles\n\n"
    "Use the user's own words for the list items. Interpret the user's intent "
    "yourself; do not expect explicit keywords."
)
