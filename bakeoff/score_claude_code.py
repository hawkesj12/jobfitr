"""Score the CLAUDE CODE variant of the applying bakeoff.

Instead of OpenRouter, the extractions here are produced by Claude models
(Sonnet, Opus) run LOCALLY through Claude Code subagents — the flat-rate
subscription path, no OpenRouter, no per-token metering. The task, gold cases,
and deterministic scorer are IDENTICAL to run_applying.py, so the numbers sit on
the same scale as the free-model table (bakeoff/results/applying-<date>.md).

Input: bakeoff/claude_code/<model>.json files, each shaped
    {"model": "claude-sonnet-...", "outputs": [{"id": "001-...", "config": {...}}]}
Output: bakeoff/results/applying-claude-code-<date>.md + .json

Note on latency: response time is NOT reported for this variant — a Claude Code
subagent round-trip isn't an apples-to-apples single-API-call latency the way the
OpenRouter path is. This variant measures QUALITY (accuracy + schema), which is
the point of asking "how would the frontier models rank on this task."
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bakeoff import scoring
from bakeoff.run_applying import load_cases

_ET = ZoneInfo("America/New_York")
_HERE = Path(__file__).resolve().parent
_IN = _HERE / "claude_code"
_RESULTS = _HERE / "results"


def _score_model(
    model: str, outputs: list[dict], cases_by_id: dict
) -> scoring.ModelReport:
    by_id = {o["id"]: o.get("config") or {} for o in outputs}
    case_scores: list[scoring.CaseScore] = []
    for cid, case in cases_by_id.items():
        # strip_unknown identically to the OpenRouter lane, so schema-validity is
        # scored on the same envelope in both lanes (the code reviewer's parity fix).
        cfg = scoring.strip_unknown(by_id.get(cid) or {})
        s = scoring.score_case(
            case_id=cid,
            predicted=cfg,
            expected=case["expected"],
            transcript=case["transcript"],
            latency=0.0,  # not measured for this variant (see module docstring)
            tokens=0,
            responded=cid in by_id,
        )
        case_scores.append(s)
    return scoring.aggregate(model, case_scores)


def _pct_bar(x: float) -> str:
    filled = round(x * 5)
    return "█" * filled + "░" * (5 - filled)


def main() -> int:
    cases = load_cases()
    cases_by_id = {c["id"]: c for c in cases}
    files = sorted(_IN.glob("*.json"))
    if not files:
        print(f"No model outputs in {_IN} (expected <model>.json files)")
        return 1

    reports = []
    for f in files:
        doc = json.loads(f.read_text())
        reports.append(_score_model(doc["model"], doc["outputs"], cases_by_id))
    reports.sort(key=lambda r: (r.mean_field, r.schema_valid_rate), reverse=True)

    date = datetime.now(_ET).date().isoformat()
    _RESULTS.mkdir(exist_ok=True)
    payload = {
        "role": "applying",
        "variant": "claude-code",
        "date": date,
        "n_cases": len(cases),
        "reports": [
            {
                "model": r.model,
                "cases_scored": r.n,
                "schema_valid_rate": round(r.schema_valid_rate, 4),
                "mean_field": round(r.mean_field, 4),
                "mean_hallucination": round(r.mean_hallucination, 4),
                "per_field": {k: round(v, 3) for k, v in r.per_field.items()},
            }
            for r in reports
        ],
    }
    (_RESULTS / f"applying-claude-code-{date}.json").write_text(
        json.dumps(payload, indent=2) + "\n"
    )

    lines = [
        f"# Applying bakeoff — Claude Code variant — {date}",
        "",
        "**Same task, same 12 gold cases, same deterministic scorer as the free-model "
        "run** — but the extractions here come from Claude models run LOCALLY through "
        "Claude Code subagents (the flat-rate subscription path), **not OpenRouter**. "
        "This answers: how would the frontier models rank on jobfitr's extraction task? "
        "Compare against the free-model table in the sibling `applying-<date>.md`.",
        "",
        "_Response time is omitted here — a Claude Code subagent round-trip isn't a "
        "single-API-call latency comparable to the OpenRouter path. This variant "
        "measures quality (accuracy + schema validity)._",
        "",
        "## Ranking",
        "",
        "| Rank | Model | Scored | Schema-valid | Field accuracy | Hallucination |",
        "| ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for i, r in enumerate(reports, 1):
        lines.append(
            f"| {i} | `{r.model}` | {r.n}/{len(cases)} "
            f"| {r.schema_valid_rate:.0%} {_pct_bar(r.schema_valid_rate)} "
            f"| {r.mean_field:.0%} {_pct_bar(r.mean_field)} | {r.mean_hallucination:.0%} |"
        )
    lines += [
        "",
        "## Per-field accuracy",
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
    lines.append("")
    out = _RESULTS / f"applying-claude-code-{date}.md"
    out.write_text("\n".join(lines))
    print(f"Wrote {out}")
    for i, r in enumerate(reports, 1):
        print(
            f"  {i}. {r.model:<28} field={r.mean_field:.0%} schema={r.schema_valid_rate:.0%}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
