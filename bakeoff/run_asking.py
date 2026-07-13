"""Run the ASKING bakeoff: simulate every candidate model interviewing every
persona, then judge the interviews pairwise (both orders) and rank with
Bradley-Terry. Also emits the judge-free objective signals (completion rate,
turns-to-complete) so there's a cheap check that doesn't depend on the judge.

Needs the [bakeoff] extra for the Bradley-Terry fit (choix); without it, rank.py
falls back to win-rate and says so.

Usage:
    python -m bakeoff.run_asking                 # all asking models × all personas
    python -m bakeoff.run_asking --models a,b
    python -m bakeoff.run_asking --personas 001-remote-react-dev
"""

from __future__ import annotations

import argparse
import itertools
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from bakeoff import client, judge, rank, user_sim

_ET = ZoneInfo("America/New_York")
_HERE = Path(__file__).resolve().parent
_PERSONA_DIR = _HERE / "cases" / "asking"
_RESULTS = _HERE / "results"


def load_models() -> dict:
    return yaml.safe_load((_HERE / "models.yaml").read_text())


def load_personas(only: list[str] | None = None) -> list[dict]:
    personas = [
        yaml.safe_load(f.read_text()) for f in sorted(_PERSONA_DIR.glob("*.yaml"))
    ]
    if only:
        personas = [p for p in personas if p["id"] in only]
    return personas


# ═══════════════════════════════════════════════════════════════
# run()
# ═══════════════════════════════════════════════════════════════
# Phase 1: every (model, persona) interview via the user-simulator → transcripts
# + objective completion signals. Phase 2: for each persona, judge every model
# PAIR in both orders → verdicts. Phase 3: Bradley-Terry rank + report. Returns
# (ranking, interviews) so tests can assert without disk.
# ═══════════════════════════════════════════════════════════════
def run(models: list[str], personas: list[dict], judge_model: str, sim_model: str):
    # Phase 1 — simulate interviews
    interviews: dict[tuple[str, str], user_sim.Interview] = {}
    for persona in personas:
        for model in models:
            iv = user_sim.run_interview(model, sim_model, persona)
            interviews[(model, persona["id"])] = iv
            done = iv.turns_to_complete if iv.turns_to_complete else "—"
            print(
                f"  sim {model:<44} {persona['id']:<26} turns={done} missed={iv.fields_missed}"
            )

    # Phase 2 — pairwise both-order judging, per persona
    verdicts: list[tuple[str, str]] = []  # (winner, loser)
    n_ties = 0
    for persona in personas:
        goal = persona["persona"]
        for a, b in itertools.combinations(models, 2):
            va = interviews[(a, persona["id"])].as_text()
            vb = interviews[(b, persona["id"])].as_text()
            verdict = judge.judge_pair(judge_model, goal, a, va, b, vb, persona["id"])
            if verdict.winner:
                loser = b if verdict.winner == a else a
                verdicts.append((verdict.winner, loser))
                print(
                    f"  judge {persona['id']:<26} {a} vs {b} → {verdict.winner.split('/')[-1]}"
                )
            else:
                n_ties += 1
                print(f"  judge {persona['id']:<26} {a} vs {b} → tie/position-bias")

    # Phase 3 — rank
    ranking = rank.bradley_terry(models, verdicts)
    ranking.n_ties = n_ties
    return ranking, interviews


def _completion_table(models, personas, interviews) -> dict:
    """Judge-free objective signal: completion rate + mean turns per model."""
    out = {}
    for m in models:
        ivs = [interviews[(m, p["id"])] for p in personas]
        completed = [iv for iv in ivs if iv.turns_to_complete]
        rate = len(completed) / len(ivs) if ivs else 0.0
        mean_turns = (
            sum(iv.turns_to_complete for iv in completed) / len(completed)
            if completed
            else None
        )
        out[m] = {"completion_rate": round(rate, 3), "mean_turns": mean_turns}
    return out


def write_results(ranking: rank.Ranking, completion: dict, n_personas: int) -> Path:
    _RESULTS.mkdir(exist_ok=True)
    date = datetime.now(_ET).date().isoformat()
    payload = {
        "role": "asking",
        "date": date,
        "n_personas": n_personas,
        "method": ranking.method,
        "kappa": ranking.kappa,
        "n_comparisons": ranking.n_comparisons,
        "n_ties": ranking.n_ties,
        "ranking": ranking.order,
        "completion": completion,
    }
    (_RESULTS / f"asking-{date}.json").write_text(json.dumps(payload, indent=2) + "\n")

    lines = [
        f"# Asking bakeoff — {date}",
        "",
        f"Interview quality across {n_personas} simulated personas. Ranking method: "
        f"**{ranking.method}** from {ranking.n_comparisons} both-order pairwise verdicts "
        f"({ranking.n_ties} ties/position-bias dropped).",
        "Judge–human agreement (Cohen's κ): "
        + (f"{ranking.kappa:.2f}" if ranking.kappa is not None else "_not yet labeled_")
        + ".",
        "",
        "| Rank | Model | Strength | 90% CI | Wins | Losses |",
        "| ---: | --- | ---: | :---: | ---: | ---: |",
    ]
    for i, row in enumerate(ranking.order, 1):
        ci = (
            f"[{row['ci_low']}, {row['ci_high']}]"
            if row.get("ci_low") is not None
            else "—"
        )
        lines.append(
            f"| {i} | `{row['model']}` | {row['strength']} | {ci} | {row['wins']} | {row['losses']} |"
        )
    lines += [
        "",
        "## Objective signal (judge-free): interview completion",
        "",
        "| Model | Completion rate | Mean turns to complete |",
        "| --- | ---: | ---: |",
    ]
    for m, c in sorted(
        completion.items(), key=lambda kv: kv[1]["completion_rate"], reverse=True
    ):
        mt = f"{c['mean_turns']:.1f}" if c["mean_turns"] else "—"
        lines.append(f"| `{m}` | {c['completion_rate']:.0%} | {mt} |")
    lines.append("")
    out = _RESULTS / f"asking-{date}.md"
    out.write_text("\n".join(lines))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the jobfitr asking-model bakeoff.")
    ap.add_argument(
        "--models", help="comma-separated slugs (default: models.yaml asking list)"
    )
    ap.add_argument("--personas", help="comma-separated persona ids (default: all)")
    args = ap.parse_args(argv)

    client.load_dotenv()
    if not client.api_key():
        print("ERROR: no OPENROUTER_API_KEY (put it in jobfitr/.env)")
        return 1

    cfg = load_models()
    models = args.models.split(",") if args.models else cfg["asking"]
    personas = load_personas(args.personas.split(",") if args.personas else None)
    print(f"Asking bakeoff: {len(models)} models × {len(personas)} personas")
    print(f"judge={cfg['judge']}  user_sim={cfg['user_simulator']}\n")

    ranking, interviews = run(models, personas, cfg["judge"], cfg["user_simulator"])
    completion = _completion_table(models, personas, interviews)
    out = write_results(ranking, completion, len(personas))
    print(f"\nWrote {out}  (method: {ranking.method})")
    for i, row in enumerate(ranking.order, 1):
        print(f"  {i}. {row['model']:<44} strength={row['strength']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
