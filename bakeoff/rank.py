"""Turn noisy pairwise judge verdicts into a defensible ranking.

Two pieces, both straight from the Arcanum experimental-design page's winning
combination (LLM-judge both-orders + kappa + Bradley-Terry):

  - bradley_terry(): converts 'model X beat model Y on persona P' verdicts into
    one latent-strength score per model with bootstrap confidence intervals. This
    is the Chatbot Arena method — beating a STRONG model counts more than beating
    a weak one, and it handles 3+ models and intransitive verdicts, which raw
    win-rate averaging gets wrong.

  - cohen_kappa(): validates the judge against a slice of human labels. If kappa
    is low, the whole ranking is measuring judge bias, not interview quality —
    so we report it honestly rather than hide it.

choix (the Bradley-Terry fitter) is an optional dep ([bakeoff] extra). If it's
missing we fall back to win-rate and say so, so the harness still runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

try:
    import choix  # type: ignore
    import numpy as np  # choix pulls numpy

    _HAS_CHOIX = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _HAS_CHOIX = False


@dataclass
class Ranking:
    method: str  # "bradley-terry" | "win-rate"
    order: list = field(
        default_factory=list
    )  # [{model, strength, ci_low, ci_high, wins, losses}]
    kappa: float | None = None
    n_comparisons: int = 0
    n_ties: int = 0


# ═══════════════════════════════════════════════════════════════
# bradley_terry()
# ═══════════════════════════════════════════════════════════════
# `verdicts`: list of (winner_model, loser_model) — ties already dropped upstream
# in judge.py. Fits latent strengths via choix.ilsr_pairwise, then bootstraps
# (resample comparisons with replacement, refit) for 90% CIs. Falls back to
# win-rate if choix isn't installed. Higher strength = better.
# ═══════════════════════════════════════════════════════════════
def bradley_terry(
    models: list[str], verdicts: list[tuple[str, str]], n_boot: int = 200
) -> Ranking:
    idx = {m: i for i, m in enumerate(models)}
    wins = {m: 0 for m in models}
    losses = {m: 0 for m in models}
    for w, loser in verdicts:
        wins[w] += 1
        losses[loser] += 1

    if not _HAS_CHOIX or not verdicts:
        order = sorted(
            models,
            key=lambda m: (
                (wins[m] / (wins[m] + losses[m])) if (wins[m] + losses[m]) else 0.0
            ),
            reverse=True,
        )
        rows = [
            {
                "model": m,
                "strength": round(
                    (wins[m] / (wins[m] + losses[m])) if (wins[m] + losses[m]) else 0.0,
                    4,
                ),
                "ci_low": None,
                "ci_high": None,
                "wins": wins[m],
                "losses": losses[m],
            }
            for m in order
        ]
        method = (
            "win-rate" + ("" if verdicts else " (no verdicts)")
            if not _HAS_CHOIX
            else "win-rate"
        )
        return Ranking(method=method, order=rows, n_comparisons=len(verdicts))

    pairs = [(idx[w], idx[loser]) for w, loser in verdicts]
    params = choix.ilsr_pairwise(len(models), pairs, alpha=0.01)

    # bootstrap CIs
    boot = np.zeros((n_boot, len(models)))
    rng = np.random.default_rng(0)  # fixed seed → reproducible CIs
    for b in range(n_boot):
        sample = [pairs[i] for i in rng.integers(0, len(pairs), len(pairs))]
        try:
            boot[b] = choix.ilsr_pairwise(len(models), sample, alpha=0.01)
        except Exception:
            boot[b] = params
    lo = np.percentile(boot, 5, axis=0)
    hi = np.percentile(boot, 95, axis=0)

    ranked = sorted(range(len(models)), key=lambda i: params[i], reverse=True)
    rows = [
        {
            "model": models[i],
            "strength": round(float(params[i]), 4),
            "ci_low": round(float(lo[i]), 4),
            "ci_high": round(float(hi[i]), 4),
            "wins": wins[models[i]],
            "losses": losses[models[i]],
        }
        for i in ranked
    ]
    return Ranking(method="bradley-terry", order=rows, n_comparisons=len(verdicts))


# ═══════════════════════════════════════════════════════════════
# cohen_kappa()
# ═══════════════════════════════════════════════════════════════
# Agreement between the judge and human labels on the same pairs, corrected for
# chance. `pairs`: list of (judge_label, human_label) over the same categories
# (e.g. winning slug or 'tie'). kappa = (po - pe)/(1 - pe). Hand-rolled (stdlib)
# to avoid a scikit-learn dep. Returns None if there's nothing to compare.
#   >0.6 substantial · 0.4-0.6 moderate · <0.4 the judge is suspect.
# ═══════════════════════════════════════════════════════════════
def cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    if not pairs:
        return None
    n = len(pairs)
    po = sum(1 for a, b in pairs if a == b) / n
    labels = {x for pair in pairs for x in pair}
    pe = 0.0
    for lab in labels:
        pa = sum(1 for a, _ in pairs if a == lab) / n
        pb = sum(1 for _, b in pairs if b == lab) / n
        pe += pa * pb
    if pe >= 1.0:
        return 1.0
    return (po - pe) / (1 - pe)
