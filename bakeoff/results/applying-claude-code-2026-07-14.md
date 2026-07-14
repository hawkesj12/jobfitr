# Applying bakeoff — Claude Code variant — 2026-07-14

**Same task, same 12 gold cases, same deterministic scorer as the free-model run** — but the extractions here come from Claude models run LOCALLY through Claude Code subagents (the flat-rate subscription path), **not OpenRouter**. This answers: how would the frontier models rank on jobfitr's extraction task? Compare against the free-model table in the sibling `applying-<date>.md`.

_Response time is omitted here — a Claude Code subagent round-trip isn't a single-API-call latency comparable to the OpenRouter path. This variant measures quality (accuracy + schema validity)._

## Ranking

| Rank | Model | Scored | Schema-valid | Field accuracy | Hallucination |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | `claude-opus (via Claude Code)` | 12/12 | 100% █████ | 80% ████░ | 0% |
| 2 | `claude-sonnet (via Claude Code)` | 12/12 | 100% █████ | 78% ████░ | 0% |

## Per-field accuracy

| Model | titles | boosts | exclude | rank_down | location | remote_only |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `claude-opus (via Claude Code)` | 0.92 | 0.79 | 0.67 | 0.25 | 0.91 | 0.71 |
| `claude-sonnet (via Claude Code)` | 0.88 | 0.83 | 0.67 | 0.25 | 0.73 | 0.86 |
