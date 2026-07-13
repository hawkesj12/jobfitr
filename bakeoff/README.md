# jobfitr model bakeoff

**Which model should jobfitr use for its AI chat — and how do we know?**

jobfitr uses an LLM for two jobs. This harness benchmarks candidate models for the
extraction job, reproducibly, so the choice is evidence, not vibes. The candidates,
graded cases, scorer, and results all live in this directory so anyone can re-run it.

- **Asking** — the chat interviewer (`/api/chat`) that talks to a visitor and
  fills their search config. (Harness built in `run_asking.py`; not yet run.)
- **Applying** — turning what the visitor said into the structured config JSON the
  scorer needs (the `config_from_dict` contract: `titles, boosts, exclude,
rank_down, location, remote_only, max_age_days, min_score`). This is what the
  committed results below measure.

The production chat currently defaults to a **free** model (`jobfitr/chat.py`:
`DEFAULT_MODEL = meta-llama/llama-3.3-70b-instruct:free`, overridable via
`CHAT_MODEL`). The bakeoff exists to check that choice with real numbers.

It benchmarks three lanes on the **identical** prompt, gold cases, and scorer:

- **Free** OpenRouter models (`:free` slugs) — zero cost, but rate-limited.
- **Paid** OpenRouter models (`openai/gpt-4o-mini`) — metered, with real per-call
  dollar cost captured.
- **Claude Code** (Sonnet, Opus) run locally on the flat-rate subscription, no
  OpenRouter — to see where the frontier models land.

## The one idea that shapes everything

**The two jobs are graded differently, because one has a right answer and one
doesn't.**

- **Applying has ground truth.** For a given transcript there is a correct config.
  So we hand-label ~12–30 transcripts, and a **deterministic scorer** grades each
  model — no AI judge, nothing to argue with. This is the strong, cheap half.
- **Asking is subjective.** "Which interview was better?" has no key. So we use an
  **LLM judge**, but carefully: a fixed _user-simulator_ role-plays personas so the
  interviews are reproducible, the judge scores every pair **in both orders** to
  cancel its position bias, and we rank with **Bradley-Terry** (the Chatbot Arena
  method) validated by **Cohen's kappa** against a slice of human labels.

_Grounding: the asking methodology is the "LLM-judge both-orders + kappa +
Bradley-Terry" combination from the Arcanum `experimental-design-evaluation-methodology`
page. The whole plan and its evidence live in `PLAN.md`._

## The ranking measures the model, not the free tier

The hard-won lesson of this harness: free endpoints get rate-limited, and if you
let that leak into the score you end up measuring "how congested OpenRouter was at
3pm," not the model. So the applying harness quarantines infrastructure from
quality:

1. **Preflight** — each model gets a cheap health-ping first. If its endpoint
   isn't answering, it's set aside as _unavailable at run time_ and **never scored**
   as slow or low-quality (it just can't be tested until off-peak).
2. **Retry until a genuine response** — quality and timing are recorded **only**
   from a real answer. A free-tier empty (a fast 200 with no content) is retried
   away, not counted against the model.
3. **Reliability is not a ranking factor.** The table ranks purely on **field
   accuracy → schema validity → speed**, all on genuine responses. Congestion can
   delay a run or sideline a model into a footnote; it cannot move the ranking.

## Why you can't just trust a model's advertised features

A live spike during design: `openai/gpt-oss-20b:free`, which _advertises_
structured-output support, **ignored the JSON schema** — it invented its own keys,
corrupted a field name with a stray non-English token, and burned hundreds of
reasoning tokens on a trivial extraction. That is exactly why the applying scorer
**measures** schema conformance instead of assuming it, and reports it as its own
column separate from field accuracy.

## Cost observability

Every OpenRouter call's real dollar cost (`usage.cost`) is captured and rolled up:
a **`$/1k users`** column in the ranking, `total_cost_usd` in the JSON, and a
**"Total OpenRouter spend this run"** line printed at the end. Free models and the
Claude Code path show `free` / subscription. This makes the accuracy-vs-cost
trade-off explicit — the whole point of the paid-vs-free comparison.

## Layout

```
bakeoff/
  models.yaml            candidate slugs per role — the one edit point
  client.py              OpenRouter caller: backoff, rate-limit spacing, latency + tokens + cost
  scoring.py             deterministic applying scorer (schema, field-F1, hallucination)
  run_applying.py        run applying models × gold cases → results/applying-<date>.md
  score_claude_code.py   score Sonnet/Opus outputs (Claude Code lane) on the same cases
  claude_code/           the Claude Code lane: shared brief + each model's raw JSON
  user_sim.py            reproducible interview simulator (drives the REAL chat prompt/tool)
  judge.py               both-order pairwise LLM judge
  rank.py                Bradley-Terry (choix) + Cohen's kappa
  run_asking.py          run asking models × personas → results/asking-<date>.md
  cases/applying/*.json  hand-labeled transcript → expected-config gold
  cases/asking/*.yaml    user-simulator personas + required-field checklists
  results/               committed run outputs (json + markdown tables)
```

## Run it

The harness reads `OPENROUTER_API_KEY` from the repo's gitignored `.env`.

```bash
# APPLYING (free + paid via OpenRouter) — no extra install needed
python -m bakeoff.run_applying                        # all applying models in models.yaml
python -m bakeoff.run_applying --models openai/gpt-4o-mini   # one specific model (paid → cost shown)
python -m bakeoff.run_applying --limit 3              # quick smoke on 3 cases

# APPLYING (Claude Code lane) — dispatch Sonnet/Opus subagents (done by the agent
# driving Claude Code), save each model's JSON array into bakeoff/claude_code/<model>.json,
# then score on the identical gold + scorer:
python -m bakeoff.score_claude_code

# ASKING — needs the bakeoff extra (Bradley-Terry fitter)
uv pip install -e ".[bakeoff]"
python -m bakeoff.run_asking
```

Results land in `results/` as a JSON record and a ranked markdown table.

## Scope — what this does and does NOT settle

This measures **config extraction quality**: given a full transcript, how well does
a model produce jobfitr's `config_from_dict` structure? That's the "applying" job.

It does **not** by itself settle the production chat model. Production (`/api/chat`)
is a **streaming, multi-turn, tool-calling interviewer** — it interleaves text with
a `set_config` call, must withhold the call until it has enough, and accumulates
config across turns. The applying task is a **one-shot** proxy for the extraction
sub-skill only. The multi-turn "asking" harness (`run_asking.py`) is built but not
yet run; a production chat-model decision should include it.

## Prompt fairness (a bug we found and fixed)

Every lane must give every model the **identical** prompt, or a cross-lane
comparison is meaningless. Two fairness bugs were caught in review and fixed:
(1) the OpenRouter lane derived its prompt by string-splitting the production chat
prompt, which silently produced a self-contradictory _interview_ instruction for a
one-shot task; (2) the Claude Code lane used a different prompt that spelled out the
exact phrase→value mappings the gold cases probe (teaching to the test). Both are
gone: all lanes now share **one canonical prompt** (`bakeoff/prompts.py`,
`EXTRACT_PROMPT`) with general field definitions and **no** case-specific hints — so
interpreting "show me lots" or "open to remote too" is the model's job. Removing the
hints dropped the frontier models ~7 points, which is the honest number.

The applying tool reuses the production `SET_CONFIG_TOOL` _schema_ (the contract
can't drift), and `strip_unknown` is applied identically in every lane so schema
validity is scored on the same envelope.

## Rate limits (why free runs are paced)

OpenRouter free variants cap at roughly **20 requests/minute**; the daily cap
depends on lifetime credits (this account is past the threshold, ~1000/day — not a
constraint). `client.py` spaces requests and backs off on 429 honoring
`Retry-After` (with a `0`/absent header falling back to exponential backoff). Free
endpoints are also congested at US peak hours, so for a complete free-model table,
**run off-peak** — paid models (gpt-4o-mini) and the Claude Code lane are unaffected.

## Results (2026-07-13) — applying task, 12 gold cases, one shared prompt + scorer

Every number below is reproducible from committed inputs: the Claude Code rows from
`claude_code/*.json` via `score_claude_code.py`; the gpt-4o-mini row from
`results/applying-2026-07-13.json`.

| Model           | Lane            | Field acc | Schema-valid | Speed p50 |   $/1k users |
| --------------- | --------------- | --------: | -----------: | --------: | -----------: |
| Opus            | Claude Code     |       83% |         100% |         — | subscription |
| Sonnet          | Claude Code     |       81% |         100% |         — | subscription |
| **gpt-4o-mini** | OpenRouter paid |   **78%** |     **100%** |  **1.3s** |   **\$0.11** |

**Free models were congested/unavailable at this (US peak) run** — the harness set
them aside rather than fake a score. A full free-model table needs an **off-peak
re-run** (early AM / late PM ET). An earlier congested run of `gemma-4-26b` landed
around 30% field / 25% schema; don't cite that as final until it's re-run and
committed.

**Takeaway (scoped honestly):** on config **extraction**, `gpt-4o-mini` lands within
~5 points of the frontier models (78% vs 81–83%), is the only OpenRouter option that
hit 100% schema validity here, is fast (1.3s), and costs **\$0.11 per 1,000
extractions** (the whole benchmark cost \$0.0013). That makes a cheap paid model a
strong candidate for the extraction path. It does **not** yet prove the production
**chat** model — that's the multi-turn asking path, which hasn't been run.

## Caveats (stated honestly)

- **Applying is a one-shot proxy.** Production chat is streaming, multi-turn,
  tool-call-gated. Run `run_asking.py` before staking the production chat model.
- **Cost is one-shot.** \$0.11/1k is per single extraction. The asking path re-sends
  a growing transcript across up to 6 turns, so a real per-interview cost is several
  times higher and is not measured here.
- **Free-model rows are incomplete** at US peak hours (congestion). Re-run off-peak;
  paid + Claude Code rows are stable any time.
- **The OpenRouter lane doesn't yet persist raw per-case outputs**, so its table is
  reproducible by re-running the API, not byte-for-byte offline (the Claude Code lane
  is — its outputs are committed). Persisting raw OpenRouter outputs is a TODO.
- **n = 12, no confidence intervals.** The top spread (78 vs 81 vs 83) is within
  noise — read it as "indistinguishable at this n," not a strict ranking. Grow to
  ~30 cases before over-reading. Some ceiling is gold-label strictness (e.g.
  "staffing agencies" as one token vs `staffing`+`agency`).
- **Claude Code lane omits response-time** — a subagent round-trip isn't a
  single-API-call latency comparable to OpenRouter. It measures quality only.
- **The hallucination column reads 0% for every model** — the heuristic is lenient
  (it's a guardrail that these models didn't invent terms, not a sensitive metric).
- The asking ranking, once run, is only as good as its judge — always read the kappa.
