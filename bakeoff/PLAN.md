# Build plan — jobfitr free-model bakeoff

**Date:** 2026-07-13 · **Gate:** ✅ `ok:true`, 0 hard issues · **Grounds on:** the Oracle run `~/.claude/library/oracle/2026-07-13-jobfitr-free-model-bakeoff.md`

> **Historical record — superseded 2026-07-14.** Production `jobfitr/chat.py` has since replaced `SET_CONFIG_TOOL`/`SYSTEM_PROMPT` with the structured-output `TURN_SCHEMA`/`TURN_SYSTEM_PROMPT` and trimmed the chat-collected contract to 6 fields (dropping `max_age_days`/`min_score`, now set deterministically downstream). The bakeoff was realigned to that live contract; references below to the old names describe the original plan, not the current code. See `README.md` for the current state.

## What this is (one sentence)

A self-contained, committed `bakeoff/` package that empirically picks the best **free** OpenRouter model for jobfitr's two AI jobs — the **asking** model (the chat interviewer) and the **applying** model (structured JSON extraction) — with a methodology a skeptic can re-run.

## Context — what already exists

The production chat path is **already built**: `jobfitr/chat.py` + `/api/chat` in `server.py`, covered by `tests/test_chat.py`. It streams from OpenRouter, tool-calls a single `set_config` tool whose schema **is** the `config_from_dict` contract, and gates cost (turn cap, per-IP limit, daily ceiling → form fallback). It currently runs on the **paid** `openai/gpt-4o-mini` via a `CHAT_MODEL` env override.

So the bakeoff does **not** build the chat — it finds the best free model to drop into `CHAT_MODEL` (asking) and, if the two roles split, the best free extractor (applying). It is **purely additive**: no production logic changes except one optional-dependency line in `pyproject.toml`.

The key design decision, forced by a live spike: **the two roles need different scoring methods.**

- **Applying has ground truth** → a deterministic scorer, no LLM judge. Runs today, zero new deps.
- **Asking is subjective** → a user-simulator + both-order LLM judge + Bradley-Terry ranking + Cohen's kappa (the Chatbot Arena method, from the Arcanum `experimental-design-evaluation-methodology` page).

The spike proof: `openai/gpt-oss-20b:free` — which advertises structured output — **ignored the strict JSON schema**, invented its own keys, corrupted a field name with a stray Cyrillic token, and burned 428 reasoning tokens on a trivial extraction. You cannot trust advertised capability; the scorer must **measure conformance**.

## Grounding confirmed (fresh reads this session)

- `jobfitr/chat.py` — `SYSTEM_PROMPT`, `SET_CONFIG_TOOL` (the config contract as a tool), `CONFIG_FIELDS`, `merge_config`, `_has_titles`, `_stream_openrouter`. The bakeoff **imports these** so the eval mirrors production.
- `jobfitr/config_builder.py:19-30` — the 8-field JSON contract + `_clean_list` (reused for score normalization).
- `job_radar/llm.py` — the stdlib-`urllib` OpenAI-compatible call pattern the client mirrors.
- `job_radar/config.py` — the `Config` dataclass the extracted JSON must round-trip through.
- `tests/test_chat.py` — the zero-network monkeypatch discipline the bakeoff tests copy.
- `pyproject.toml` — `pyyaml` already core; `httpx` in web/dev; `choix` is the only new dep (asking-side only).

## Recommended approach

| Concern        | Decision                                                                                                                                                  |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Transport      | Reuse the `job_radar/llm.py` pattern: stdlib `urllib`, `base_url=https://openrouter.ai/api/v1`, `Authorization: Bearer $OPENROUTER_API_KEY` from `.env`   |
| Pluggability   | Candidate `:free` slugs live in `bakeoff/models.yaml`; the client iterates — one edit point                                                               |
| Rate limits    | `is_free_tier:false` → ~1000/day (fine); ~20 rpm burst → client does exp-backoff + spacing + honors `Retry-After`                                         |
| Applying score | Deterministic: schema-valid rate, per-field set-F1 (lists) + exact-match (scalars), hallucination rate, latency/tokens. **No judge.**                     |
| Asking score   | User-simulator drives interviews → both-order pairwise judge → Bradley-Terry (`choix`) + Cohen's-kappa judge validation + objective completion-rate proxy |
| Fidelity       | Import `chat.SYSTEM_PROMPT` + `chat.SET_CONFIG_TOOL` + `merge_config` — never re-author the prompt/tool                                                   |
| Deliverable    | `results/*.md` + `*.json` committed; `README.md` carries methodology + both ranking tables                                                                |

## Decisions locked (the gate's contract)

**Must add:** reuse the `llm.py` urllib/OpenRouter pattern · applying scorer reuses `config_builder._clean_list` · client exp-backoff + spacing + `Retry-After` · model-agnostic via `models.yaml` · applying = deterministic scorer, no judge · asking = user-sim + both-order judge + Bradley-Terry + kappa · import production `SYSTEM_PROMPT`/`SET_CONFIG_TOOL` · reproducible + committed with a methodology README · zero-network monkeypatch tests.

**Must not:** touch the `/api/score` zero-external-call plane or production `chat.py` logic · commit `.env`/secrets · hardcode a single model · require paid models for the free-model bakeoff.

## Sequence

_All paths are in the new `bakeoff/` package unless marked. Every file is an `add` except the one `pyproject.toml` edit._

**Phase 1 — Applying (runnable TODAY, zero new deps)**

1. `bakeoff/__init__.py` (add) — package marker.
2. `bakeoff/client.py` (add) — model-agnostic OpenRouter caller mirroring `job_radar/llm.py`: stdlib `urllib`, Bearer key, a tiny `.env` loader; **exp-backoff + inter-call spacing + `Retry-After` on 429**, retry 5xx; captures latency + `usage.total_tokens`; supports `tools` and `response_format`.
3. `bakeoff/models.yaml` (add) — `applying[]`, `asking[]`, fixed `judge`, fixed `user_simulator` slugs (all `:free`). The one place you edit to change the field.
4. `bakeoff/cases/applying/README.md` (add) — documents the gold shape `{transcript, expected}` + labeling rubric.
5. `bakeoff/cases/applying/NNN-*.json` (add) — ~12–15 hand-labeled gold cases to start (expand to ~30), spanning tech + non-tech + vague/voice-to-text.
6. `bakeoff/scoring.py` (add) — deterministic: `schema_valid`, per-field set-F1 + exact-match, hallucination rate; reuses `config_builder._clean_list`.
7. `bakeoff/run_applying.py` (add) — models × cases via client, extraction prompt from `chat.SYSTEM_PROMPT` + `chat.SET_CONFIG_TOOL` (tried as `response_format` **and** as prompt, to measure conformance both ways) → score → `results/applying-<ET-date>.{json,md}` ranked table.

**Phase 2 — Asking (adds `choix`)**

8. `bakeoff/cases/asking/NNN-*.yaml` (add) — personas + required-field checklist (zookeeper, remote React dev, vague career-switcher, voice rambler).
9. `bakeoff/user_sim.py` (add) — user-simulator: fixed sim model role-plays a persona while a candidate runs the **real** `chat.SYSTEM_PROMPT` + `SET_CONFIG_TOOL`; reuses `merge_config`/`_has_titles` to detect completion; returns transcript + turns-to-complete + fields-missed.
10. `bakeoff/judge.py` (add) — both-order pairwise judge (A/B and B/A) on a short rubric via the fixed free judge → verdicts.
11. `bakeoff/rank.py` (add) — `choix` Bradley-Terry + bootstrap CIs + hand-rolled Cohen's kappa (stdlib) vs a labeled slice; folds in the completion proxy.
12. `bakeoff/run_asking.py` (add) — orchestrate user_sim × personas → judge → rank → `results/asking-<ET-date>.{json,md}`.

**Phase 3 — deps + tests + docs**

13. `tests/test_bakeoff.py` (add) — zero-network suite (monkeypatched, like `test_chat`): scorer math, client backoff/parse, user*sim loop, judge both-order, kappa, Bradley-Terry, both `run*\*` end-to-end.
14. `pyproject.toml` (edit) — add `[project.optional-dependencies] bakeoff = ["choix>=0.3.5"]` (pulls numpy/scipy). Applying slice needs none of this.
15. `bakeoff/README.md` (add) — the reviewable artifact: methodology, how-to-run, rate-limit note, both results tables. Run through `legible-doc` before final.

## Critical files

`bakeoff/client.py` (the transport + rate-limit discipline), `bakeoff/scoring.py` (the objective applying verdict), `bakeoff/rank.py` (the statistically-sound asking verdict), `bakeoff/README.md` (the thing reviewers read).

## Risks

- **~20 rpm burst 429s** (observed live) → client backoff + spacing; runs are small.
- **Eval drift from production** → import `chat.SYSTEM_PROMPT`/`SET_CONFIG_TOOL`, never re-author.
- **Judge measures its own bias** → both-order + kappa; judge ≠ any entrant; report kappa honestly.
- **`choix` pulls numpy/scipy** → isolated in the optional `[bakeoff]` extra.

## Open questions

- Gold-case count: start ~12–15, expand to ~30 before trusting the asking ranking (applying separates earlier).
- Judge + user-sim model: a strong free model NOT in the entrant pool (candidate: `qwen3-next-80b` or `llama-3.3-70b`) — settle at build from a quick smoke.
- Emit the objective completion-rate table standalone (judge-free signal)? Recommend yes.

## Verification

- `pytest tests/test_bakeoff.py` green, incl. zero-network assertions.
- `ruff check bakeoff/` clean.
- **Live applying (today):** `python -m bakeoff.run_applying` → `results/applying-<date>.md` with a ranked table across the free models.
- **Live asking:** `uv pip install -e .[bakeoff]` then `python -m bakeoff.run_asking` → `results/asking-<date>.md` with Bradley-Terry ranking + reported kappa.
