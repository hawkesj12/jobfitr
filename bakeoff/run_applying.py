"""Run the APPLYING bakeoff: each free model extracts the config from every gold
transcript; the deterministic scorer grades it; results land in results/ as JSON
+ a ranked markdown table. Runnable today with zero new dependencies.

Fidelity: the extraction fields come straight from the live production contract
(jobfitr.chat.TURN_SCHEMA, narrowed to jobfitr.chat.CONFIG_FIELDS) plus a system
prompt derived from the production one — so we measure the real task and the eval
can't silently drift from the app. We try each model TWO
ways and take the better, because free models honor different mechanisms:
  - tool-call   : the model calls set_config (what production actually does)
  - json_object : the model returns a bare JSON object (fallback for models
                  whose tool-calling is weak but JSON-in-prose is fine)

Usage:
    python -m bakeoff.run_applying                 # all applying models
    python -m bakeoff.run_applying --models a,b    # just these slugs
    python -m bakeoff.run_applying --limit 3       # first 3 cases (a quick smoke)
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from bakeoff import client, scoring
from bakeoff.prompts import EXTRACT_PROMPT
from jobfitr.chat import CONFIG_FIELDS, TURN_SCHEMA

_ET = ZoneInfo("America/New_York")
_HERE = Path(__file__).resolve().parent
_CASE_DIR = _HERE / "cases" / "applying"
_RESULTS = _HERE / "results"

# The SAME canonical prompt every lane uses (bakeoff/prompts.py) — self-contained,
# no case-specific hints, so interpreting the transcript is the model's job. The
# tool path adds only "call set_config once"; the JSON fallback adds "return JSON".
_EXTRACT_SYSTEM = EXTRACT_PROMPT + "\n\nCall the set_config tool exactly once."

# The APPLYING tool. Its parameters are the CONFIG fields the production chat
# actually collects (jobfitr.chat.CONFIG_FIELDS), with each field's schema pulled
# straight from the live jobfitr.chat.TURN_SCHEMA — so the extractor is graded on
# the app's real contract and can't drift from it. (TURN_SCHEMA is the turn
# structured-output schema; we take only its config-field properties, dropping the
# chat-only reply/ready/chips.) A complete-extraction description replaces the
# incremental "this turn" framing, since applying is a one-shot task.
_TURN_PROPS = TURN_SCHEMA["json_schema"]["schema"]["properties"]
_EXTRACT_TOOL = {
    "type": "function",
    "function": {
        "name": "set_config",
        "description": (
            "Record the user's COMPLETE job search from the transcript. Fill every "
            "field the transcript supports; omit only fields the user never mentioned."
        ),
        "parameters": {
            "type": "object",
            "properties": {f: _TURN_PROPS[f] for f in CONFIG_FIELDS},
        },
    },
}


def load_cases(limit: int | None = None) -> list[dict]:
    files = sorted(_CASE_DIR.glob("*.json"))
    cases = [json.loads(f.read_text()) for f in files]
    return cases[:limit] if limit else cases


def load_models() -> dict:
    return yaml.safe_load((_HERE / "models.yaml").read_text())


# ═══════════════════════════════════════════════════════════════
# extract_one()
# ═══════════════════════════════════════════════════════════════
# One model's best attempt at one transcript. Tries the tool-call path first
# (production-faithful); if it yields no usable config, falls back to the
# json_object path. Returns a 5-tuple:
#   (config_dict, latency, tokens, responded, cost)
# where LATENCY is the response time of the SINGLE call that produced the result
# (what a user waits, not the sum of both attempts), RESPONDED is whether the
# endpoint gave a real answer at all (False = infra miss, not a quality zero), and
# COST is the USD spent across the attempt(s). config may be {} on total failure.
# ═══════════════════════════════════════════════════════════════
def extract_one(model: str, transcript: str) -> tuple[dict, float, int, bool, float]:
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user", "content": transcript},
    ]
    # 1) tool-call path (production-faithful contract, complete-extraction framing).
    # tool_choice='auto' (NOT forced): forcing a named tool makes some models emit a
    # degenerate minimal call; auto lets them fill the whole config. The system
    # prompt already tells them to call set_config, so refusal is rare.
    r = client.call(
        model,
        messages,
        tools=[_EXTRACT_TOOL],
        tool_choice="auto",
        max_tokens=512,
    )
    if r.ok:
        cfg = scoring.strip_unknown(r.tool_args())
        if cfg:
            # responded=True; response time + cost = the single tool call
            return cfg, r.latency, r.tokens, True, r.cost

    # 2) json_object fallback
    r2 = client.call(
        model,
        [
            {
                "role": "system",
                "content": _EXTRACT_SYSTEM + " Respond with ONLY a JSON object.",
            },
            {"role": "user", "content": transcript},
        ],
        response_format={"type": "json_object"},
        max_tokens=512,
    )
    # responded = did the endpoint give a REAL answer (any content or tool call)?
    # A 200 with empty content AND no tool call is a degraded/rate-limited endpoint
    # returning nothing — that's an infra (RELIABILITY) miss, not the model failing
    # the task, so it gets retried rather than scored as a quality zero. A response
    # that HAS content but is malformed (e.g. gemma-26b's garbage tool args) stays a
    # genuine QUALITY miss (responded=True, schema-invalid).
    responded = bool((r.ok and (r.tool_args() or r.content)) or (r2.ok and r2.content))
    tok = (r.tokens or 0) + (r2.tokens or 0)
    cost = (r.cost or 0) + (r2.cost or 0)  # both attempts count toward spend
    if r2.ok and r2.content:
        try:
            parsed = json.loads(r2.content)
            if isinstance(parsed, dict):
                return scoring.strip_unknown(parsed), (r2.latency or 0), tok, True, cost
        except json.JSONDecodeError:
            pass
    return {}, (r2.latency or r.latency or 0), tok, responded, cost


# Max attempts to get a GENUINE response for one case before we give up on that
# case (a congested free endpoint). Kept modest so a truly-dead model doesn't stall.
MAX_CASE_TRIES = int(os.environ.get("BAKEOFF_MAX_CASE_TRIES", "4"))


# ═══════════════════════════════════════════════════════════════
# preflight()
# ═══════════════════════════════════════════════════════════════
# Cheap health check: does this model's free endpoint actually answer right now?
# We only spend timing/quality measurement on a connection that works — a model
# whose endpoint is congested/down is set aside as "unavailable at run time", NOT
# scored as a slow or low-quality model. Two quick tries before giving up.
# ═══════════════════════════════════════════════════════════════
def preflight(model: str) -> bool:
    for _ in range(2):
        r = client.call(
            model,
            [{"role": "user", "content": "Reply with the word: ok"}],
            max_tokens=8,
        )
        if r.ok and (r.content or r.tool_calls):
            return True
    return False


# ═══════════════════════════════════════════════════════════════
# score_case_live()
# ═══════════════════════════════════════════════════════════════
# Get a GENUINE response for one case, retrying past free-tier empties, then score
# it. Timing + quality are recorded ONLY from a real response — never from a
# failed/empty call. Returns None if no real response came after MAX_CASE_TRIES
# (that case is skipped, not scored as a punitive zero).
# ═══════════════════════════════════════════════════════════════
def score_case_live(model: str, case: dict) -> scoring.CaseScore | None:
    for _ in range(MAX_CASE_TRIES):
        cfg, lat, tok, responded, cost = extract_one(model, case["transcript"])
        if responded:
            return scoring.score_case(
                case_id=case["id"],
                predicted=cfg,
                expected=case["expected"],
                transcript=case["transcript"],
                latency=lat,
                tokens=tok,
                responded=True,
                cost=cost,
            )
    return None


# ═══════════════════════════════════════════════════════════════
# run()
# ═══════════════════════════════════════════════════════════════
# Score every REACHABLE model over every case and rank purely on MODEL QUALITY —
# field accuracy, then schema validity, then speed — all measured only on genuine
# responses. Free-tier congestion cannot change the ranking: an unreachable model
# is set aside (returned separately), and a case that won't respond is skipped,
# never scored as a quality zero. Returns (ranked_reports, unavailable_models).
# ═══════════════════════════════════════════════════════════════
def run(
    models: list[str], cases: list[dict]
) -> tuple[list[scoring.ModelReport], list[str]]:
    reports: list[scoring.ModelReport] = []
    unavailable: list[str] = []
    for model in models:
        if not preflight(model):
            unavailable.append(model)
            print(
                f"  {model:<44} — UNAVAILABLE at run time (endpoint not responding); set aside"
            )
            continue

        case_scores: list[scoring.CaseScore] = []
        for case in cases:
            s = score_case_live(model, case)
            if s is None:  # no genuine response after retries → skip, don't zero it
                print(
                    f"  {model:<44} {case['id']:<26} skipped (no response after retries)"
                )
                continue
            case_scores.append(s)
            print(
                f"  {model:<44} {case['id']:<26} "
                f"schema={'ok ' if s.schema_ok else 'bad'} "
                f"field={s.overall:.2f} hallux={s.hallucination:.2f} {s.latency:.1f}s"
            )
        if case_scores:
            reports.append(scoring.aggregate(model, case_scores))
        else:
            unavailable.append(model)  # reachable but never produced a scorable case

    # rank purely on model quality — free-tier weather is NOT a factor here
    reports.sort(
        key=lambda r: (r.mean_field, r.schema_valid_rate, -r.p50_latency), reverse=True
    )
    return reports, unavailable


def _pct_bar(x: float) -> str:
    filled = round(x * 5)
    return "█" * filled + "░" * (5 - filled)


def write_results(
    reports: list[scoring.ModelReport],
    n_cases: int,
    unavailable: list[str] | None = None,
) -> Path:
    _RESULTS.mkdir(exist_ok=True)
    date = datetime.now(_ET).date().isoformat()
    unavailable = unavailable or []

    payload = {
        "role": "applying",
        "date": date,
        "n_cases": n_cases,
        "unavailable_at_run_time": unavailable,
        "reports": [
            {
                "model": r.model,
                "cases_scored": r.n,
                "schema_valid_rate": round(r.schema_valid_rate, 4),
                "mean_field": round(r.mean_field, 4),
                "mean_hallucination": round(r.mean_hallucination, 4),
                "p50_latency": round(r.p50_latency, 2),
                "p95_latency": round(r.p95_latency, 2),
                "mean_tokens": round(r.mean_tokens, 1),
                "total_cost_usd": round(r.total_cost, 6),
                "mean_cost_usd": round(r.mean_cost, 6),
                "cost_per_1k_extractions_usd": round(r.mean_cost * 1000, 4),
                "per_field": {k: round(v, 3) for k, v in r.per_field.items()},
                # Raw per-case extractions, so this lane is re-scorable OFFLINE after a
                # scorer change — no need to re-hit the API to re-derive a number.
                "predictions": {s.case_id: s.predicted for s in r.cases},
            }
            for r in reports
        ],
    }
    (_RESULTS / f"applying-{date}.json").write_text(
        json.dumps(payload, indent=2) + "\n"
    )

    lines = [
        f"# Applying bakeoff — {date}",
        "",
        f"**The task:** read a chat transcript and extract the user's job-search "
        f"preferences into jobfitr's structured config. Each reachable model was tested "
        f"on {n_cases} hand-labeled transcripts whose correct config is known, so the "
        "scoring is exact — no AI judge. Ranked by field accuracy, then schema validity, "
        "then speed.",
        "",
        "> **This ranking measures the model, not the free tier.** OpenRouter's free "
        "endpoints get rate-limited, so a call sometimes returns nothing. Free-tier "
        "congestion is deliberately kept OUT of the score: each case is retried until a "
        "**genuine** response comes back, and quality + timing are measured only on that "
        "real response. A model whose endpoint isn't answering at all is set aside as "
        "_unavailable at run time_ (listed below), never scored as slow or low-quality. "
        "So congestion can delay a run or sideline a model — it cannot change the ranking.",
        "",
        "## How to read this",
        "",
        "Scores are 0–100% (higher is better) unless noted. All measured on genuine responses only.",
        "",
        "| Column | What it means |",
        "| --- | --- |",
        "| **Scored** | How many of the gold cases produced a genuine response and were scored (a couple may be skipped if the endpoint kept returning empty). |",
        "| **Schema-valid** | How often the returned config fit jobfitr's contract (right keys, right types). Catches models that respond but emit malformed JSON. |",
        "| **Field accuracy** | Of the fields that mattered per case, how many the model got right (exact for single values, overlap-F1 for lists). **The headline quality number.** |",
        "| **Hallucination** | Share of list items the model invented that the user never said (lower is better). These silently corrupt someone's search. |",
        "| **Response p50 / p95** | How long a genuine extraction took, in seconds. p50 = typical, p95 = worst-case (19 of 20 are faster). The wait a user would feel. |",
        "| **~tokens** | Average tokens per extraction — a rough cost/speed proxy; reasoning models spend far more. |",
        "| **$/1k extractions** | Real spend, projected to 1,000 one-shot extractions, from OpenRouter's reported per-call cost. Free models are \\$0. NOTE: a full multi-turn user chat costs several extractions' worth, so this is a floor, not a per-user cost. |",
        "",
        "## Ranking",
        "",
        "| Rank | Model | Scored | Schema-valid | Field accuracy | Hallucination | Response p50 | Response p95 | ~tokens | $/1k extractions |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for i, r in enumerate(reports, 1):
        per_1k = r.mean_cost * 1000
        cost_cell = "free" if per_1k == 0 else f"${per_1k:,.2f}"
        lines.append(
            f"| {i} | `{r.model}` | {r.n}/{n_cases} "
            f"| {r.schema_valid_rate:.0%} {_pct_bar(r.schema_valid_rate)} "
            f"| {r.mean_field:.0%} {_pct_bar(r.mean_field)} | {r.mean_hallucination:.0%} "
            f"| {r.p50_latency:.1f}s | {r.p95_latency:.1f}s | {r.mean_tokens:.0f} | {cost_cell} |"
        )
    if unavailable:
        lines += [
            "",
            "### Unavailable at run time",
            "",
            "These models' free endpoints weren't responding during this run (OpenRouter "
            "congestion), so they were set aside rather than scored. Re-run off-peak to "
            "test them — this is a free-tier availability note, **not** a quality verdict.",
            "",
            *[f"- `{m}`" for m in unavailable],
        ]
    # per-field breakdown + a glossary of what each field is
    lines += [
        "",
        "## Per-field accuracy",
        "",
        "How well each model extracted each individual field (0–1). Blank = no case tested that field.",
        "",
        "| Model | " + " | ".join(scoring.ALL_FIELDS) + " |",
        "| --- | " + " | ".join("---:" for _ in scoring.ALL_FIELDS) + " |",
    ]
    for r in reports:
        cells = " | ".join(
            f"{r.per_field.get(f, float('nan')):.2f}" if f in r.per_field else "—"
            for f in scoring.ALL_FIELDS
        )
        lines.append(f"| `{r.model}` | {cells} |")
    lines += [
        "",
        "### What each field means",
        "",
        "| Field | Meaning |",
        "| --- | --- |",
        "| **titles** | The roles the user wants (e.g. `zookeeper`, `animal keeper`). |",
        "| **boosts** | Signals that should rank a job _higher_ — skills, tools, a nearby city. |",
        "| **exclude** | Title words that should _hide_ a job entirely (e.g. `intern`, `volunteer`). |",
        "| **rank_down** | Signals that should _sink_ a job but not hide it (e.g. `staffing`, `agency`). |",
        "| **location** | A place (`Louisville, KY`), or `remote`, or `anywhere`. |",
        "| **remote_only** | True if the user only wants remote roles. |",
        "",
        "_`max_age_days` (recency) and `min_score` (pickiness) are no longer part of the "
        "extraction contract — the chat stopped asking for them; they're set "
        "deterministically downstream — so they aren't scored here._",
        "",
        "_Generated by `bakeoff/run_applying.py` — see `bakeoff/README.md` for the methodology._",
        "",
    ]
    out = _RESULTS / f"applying-{date}.md"
    out.write_text("\n".join(lines))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the jobfitr applying-model bakeoff.")
    ap.add_argument(
        "--models", help="comma-separated slugs (default: models.yaml applying list)"
    )
    ap.add_argument("--limit", type=int, help="only the first N cases (quick smoke)")
    args = ap.parse_args(argv)

    client.load_dotenv()
    if not client.api_key():
        print("ERROR: no OPENROUTER_API_KEY (put it in jobfitr/.env)")
        return 1

    cfg = load_models()
    models = args.models.split(",") if args.models else cfg["applying"]
    cases = load_cases(args.limit)
    print(f"Applying bakeoff: {len(models)} models × {len(cases)} cases\n")

    reports, unavailable = run(models, cases)
    out = write_results(reports, len(cases), unavailable)
    print(f"\nWrote {out}")
    print("\nRanking (model quality — field accuracy, then schema, then speed):")
    for i, r in enumerate(reports, 1):
        per_1k = r.mean_cost * 1000
        cost = "free" if per_1k == 0 else f"${per_1k:,.2f}/1k extractions"
        print(
            f"  {i}. {r.model:<44} field={r.mean_field:.0%} schema={r.schema_valid_rate:.0%} "
            f"p50={r.p50_latency:.1f}s {cost}  (scored {r.n}/{len(cases)})"
        )
    total_spend = sum(r.total_cost for r in reports)
    print(f"\nTotal OpenRouter spend this run: ${total_spend:.4f}")
    if unavailable:
        print("\nUnavailable at run time (free-tier congestion, not scored):")
        for m in unavailable:
            print(f"  - {m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
