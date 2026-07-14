"""Deterministic scorer for the APPLYING role — no LLM judge, because the task
has ground truth: a hand-labeled config is the correct answer.

Four things get measured per (model, case), because they are the four ways a
free extractor actually fails (the spike showed all four are live):
  - schema_valid  : did it emit the config contract at all, with right types?
  - field score   : per-field set-F1 for the list fields, exact-match for scalars.
  - hallucination : fraction of emitted list items NOT supported by the transcript
                    (the failure that silently corrupts a user's search).
  - cost          : latency + tokens (carried from the client, ranked separately).

Normalization reuses jobfitr.config_builder._clean_list so we score the MODEL,
not casing/whitespace/format — the same coercion the real app applies before the
value reaches scoring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobfitr.chat import CONFIG_FIELDS
from jobfitr.config_builder import _clean_list

# Score exactly the fields the production CHAT collects, sourced from
# jobfitr.chat.CONFIG_FIELDS so the eval can't silently drift from the app again.
# The chat stopped collecting max_age_days / min_score — they're set deterministically
# downstream now (jobfitr.server's RESULT_LADDER + config_builder defaults), so they
# are no longer an extraction target and aren't part of the contract an extractor is
# graded against.
_LISTY = ("titles", "boosts", "exclude", "rank_down")
LIST_FIELDS = tuple(f for f in CONFIG_FIELDS if f in _LISTY)
SCALAR_FIELDS = tuple(f for f in CONFIG_FIELDS if f not in _LISTY)
ALL_FIELDS = LIST_FIELDS + SCALAR_FIELDS


def strip_unknown(cfg: dict) -> dict:
    """Keep only the config-contract fields (jobfitr.chat.CONFIG_FIELDS) — the same
    narrowing jobfitr.chat.merge_config does in production. Applied identically in
    EVERY lane so schema-validity is scored on the same envelope (an extra key can't
    pass in one lane and fail in another)."""
    return {k: v for k, v in (cfg or {}).items() if k in ALL_FIELDS}


# ═══════════════════════════════════════════════════════════════
# schema_valid()
# ═══════════════════════════════════════════════════════════════
# True only if `out` is a dict whose present keys are all in the contract and
# carry the right coarse type (list / str / bool / int). Extra keys or a wrong
# type = invalid — this is the conformance signal the spike proved you can't
# assume. Absent optional keys are fine (the app defaults them).
# ═══════════════════════════════════════════════════════════════
def schema_valid(out) -> bool:
    if not isinstance(out, dict) or not out:
        return False
    for k, v in out.items():
        if k not in ALL_FIELDS:
            return False
        if k in LIST_FIELDS and not isinstance(v, (list, tuple, str)):
            return False
        if k == "location" and not isinstance(v, str):
            return False
        if k == "remote_only" and not isinstance(v, bool):
            return False
        if k == "max_age_days" and (
            isinstance(v, bool) or not isinstance(v, (int, float))
        ):
            return False
        if k == "min_score" and not isinstance(v, (str, int, float)):
            return False
    return True


def _f1(pred: set, gold: set) -> float:
    if not pred and not gold:
        return 1.0
    if not pred or not gold:
        return 0.0
    tp = len(pred & gold)
    if tp == 0:
        return 0.0
    precision = tp / len(pred)
    recall = tp / len(gold)
    return 2 * precision * recall / (precision + recall)


# US state name -> USPS abbreviation, so "Austin, Texas" scores == gold "Austin, TX".
_STATES = {
    "alabama": "al",
    "alaska": "ak",
    "arizona": "az",
    "arkansas": "ar",
    "california": "ca",
    "colorado": "co",
    "connecticut": "ct",
    "delaware": "de",
    "florida": "fl",
    "georgia": "ga",
    "hawaii": "hi",
    "idaho": "id",
    "illinois": "il",
    "indiana": "in",
    "iowa": "ia",
    "kansas": "ks",
    "kentucky": "ky",
    "louisiana": "la",
    "maine": "me",
    "maryland": "md",
    "massachusetts": "ma",
    "michigan": "mi",
    "minnesota": "mn",
    "mississippi": "ms",
    "missouri": "mo",
    "montana": "mt",
    "nebraska": "ne",
    "nevada": "nv",
    "hampshire": "nh",
    "jersey": "nj",
    "mexico": "nm",
    "york": "ny",
    "carolina": "nc",
    "dakota": "nd",
    "ohio": "oh",
    "oklahoma": "ok",
    "oregon": "or",
    "pennsylvania": "pa",
    "rhode": "ri",
    "tennessee": "tn",
    "texas": "tx",
    "utah": "ut",
    "vermont": "vt",
    "virginia": "va",
    "washington": "wa",
    "wisconsin": "wi",
    "wyoming": "wy",
}


def _scalar_match(field_name: str, pred, gold) -> float:
    """1.0 if the predicted scalar matches gold after light normalization."""
    if field_name == "location":
        # punctuation-insensitive + state name<->abbr aware, so "Austin, Texas",
        # "Austin, TX", and "austin tx" all match (same place, not a model error).
        def _norm(s):
            toks = re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower()).split()
            return [_STATES.get(t, t) for t in toks]

        return 1.0 if _norm(pred) == _norm(gold) else 0.0
    if field_name == "remote_only":
        # A model that OMITS remote_only gets no credit — bool(None)==False would
        # otherwise auto-award every case whose gold is False, measuring the default
        # rather than extraction. Omission is scored as a miss, like the other scalars.
        if pred is None:
            return 0.0
        return 1.0 if bool(pred) == bool(gold) else 0.0
    if field_name == "max_age_days":
        try:
            return 1.0 if int(pred) == int(gold) else 0.0
        except (TypeError, ValueError):
            return 0.0
    if field_name == "min_score":  # keyword or int, compare as string
        return 1.0 if str(pred).strip().lower() == str(gold).strip().lower() else 0.0
    return 0.0


@dataclass
class CaseScore:
    case_id: str
    schema_ok: bool
    responded: bool = (
        True  # False = the model's endpoint never returned (infra, not quality)
    )
    field_scores: dict = field(default_factory=dict)  # per-field 0..1
    hallucination: float = 0.0
    latency: float = 0.0
    tokens: int = 0
    cost: float = (
        0.0  # USD for this case (0 for free models / Claude Code subscription)
    )
    overall: float = 0.0  # mean field score, gated by schema validity
    predicted: dict = field(
        default_factory=dict
    )  # the config scored, for offline re-scoring


# ═══════════════════════════════════════════════════════════════
# score_case()
# ═══════════════════════════════════════════════════════════════
# Score one model's extracted config against the gold for a case. Only fields the
# gold specifies are scored (the gold IS the spec of what mattered for that case).
# Overall = mean of scored fields, but ZEROED if the output isn't schema-valid —
# an unparseable/garbage config is worthless no matter how many tokens overlap.
# ═══════════════════════════════════════════════════════════════
def score_case(
    case_id: str,
    predicted: dict | None,
    expected: dict,
    transcript: str = "",
    latency: float = 0.0,
    tokens: int = 0,
    responded: bool = True,
    cost: float = 0.0,
) -> CaseScore:
    predicted = predicted if isinstance(predicted, dict) else {}
    ok = schema_valid(predicted)

    field_scores: dict[str, float] = {}
    for f in LIST_FIELDS:
        if f in expected:
            field_scores[f] = _f1(
                set(_clean_list(predicted.get(f))), set(_clean_list(expected.get(f)))
            )
    for f in SCALAR_FIELDS:
        if f in expected:
            field_scores[f] = _scalar_match(f, predicted.get(f), expected.get(f))

    hallux = _hallucination_rate(predicted, transcript)

    # Field accuracy is scored on WHAT WAS RETURNED, not gated by schema validity —
    # config_from_dict is tolerant (it coerces messy input via _clean_list), so a
    # model earns credit for the fields it got right even if the envelope has a type
    # quirk. Schema-validity is reported as its OWN separate signal (schema_ok), not
    # folded into accuracy. Garbage values score low on their own (they don't match
    # the gold after normalization), so no punitive gate is needed.
    overall = (sum(field_scores.values()) / len(field_scores)) if field_scores else 0.0

    return CaseScore(
        case_id=case_id,
        schema_ok=ok,
        responded=responded,
        field_scores=field_scores,
        hallucination=hallux,
        latency=latency,
        tokens=tokens,
        cost=cost,
        overall=overall,
        predicted=predicted,
    )


# ═══════════════════════════════════════════════════════════════
# _hallucination_rate()
# ═══════════════════════════════════════════════════════════════
# Fraction of emitted list-field items with NO support in the transcript — a
# proxy for the model inventing search terms the user never said. An item counts
# as supported if any of its words stem-matches the transcript (first 4 chars as
# a substring), which tolerates the legitimate normalization the app does:
# "agency"→"agencies", "reptiles"→"reptiles.", "animal keeper"→"...animal keeper".
# A heuristic, deliberately lenient so it flags real invention, not paraphrase.
# 0 when nothing was emitted.
# ═══════════════════════════════════════════════════════════════
def _hallucination_rate(predicted: dict, transcript: str) -> float:
    if not transcript:
        return 0.0
    clean = re.sub(r"[^a-z0-9 ]+", " ", transcript.lower())

    def supported(word: str) -> bool:
        if len(word) < 3:
            return f" {word} " in f" {clean} "
        return word[:4] in clean  # stem substring: agency→"agen"⊂"agencies"

    emitted, unsupported = 0, 0
    for f in LIST_FIELDS:
        for item in _clean_list(predicted.get(f)):
            emitted += 1
            words = [w for w in re.split(r"[^a-z0-9]+", item) if w]
            if not any(supported(w) for w in words):
                unsupported += 1
    return (unsupported / emitted) if emitted else 0.0


@dataclass
class ModelReport:
    model: str
    n: int = 0  # total cases attempted
    n_responded: int = 0  # cases where the endpoint actually returned
    reliability_rate: float = 0.0  # n_responded / n (free-tier endpoint health)
    schema_valid_rate: float = 0.0  # over RESPONDED cases only
    mean_field: float = 0.0  # over RESPONDED cases only
    mean_hallucination: float = 0.0
    p50_latency: float = 0.0
    p95_latency: float = 0.0
    mean_tokens: float = 0.0
    total_cost: float = 0.0  # USD to run all scored cases (0 for free / subscription)
    mean_cost: float = 0.0  # USD per extraction
    per_field: dict = field(default_factory=dict)
    cases: list = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# aggregate()
# ═══════════════════════════════════════════════════════════════
# Roll per-case scores into the ranked report row. CRITICAL: quality metrics
# (schema-valid, field accuracy) are computed over calls that ACTUALLY RESPONDED
# — a free-tier call that never returned is a RELIABILITY event, not a quality
# failure, and is reported in its own reliability_rate column instead of silently
# tanking the model's accuracy. This is what keeps the bakeoff measuring the
# model, not which endpoint happened to be congested during the run.
# ═══════════════════════════════════════════════════════════════
def aggregate(model: str, scores: list[CaseScore]) -> ModelReport:
    if not scores:
        return ModelReport(model=model)
    n = len(scores)
    responded = [s for s in scores if s.responded]
    nr = len(responded)
    reliability = nr / n

    schema_rate = (sum(1 for s in responded if s.schema_ok) / nr) if nr else 0.0
    mean_field = (sum(s.overall for s in responded) / nr) if nr else 0.0
    mean_hallux = (sum(s.hallucination for s in responded) / nr) if nr else 0.0
    # Response-time percentiles over responded calls only.
    latencies = sorted(s.latency for s in responded if s.latency > 0)
    if latencies:
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[
            min(len(latencies) - 1, int(round(0.95 * (len(latencies) - 1))))
        ]
    else:
        p50 = p95 = 0.0
    mean_tok = (sum(s.tokens for s in responded) / nr) if nr else 0.0
    total_cost = sum(s.cost for s in responded)
    mean_cost = (total_cost / nr) if nr else 0.0

    per_field: dict[str, list] = {}
    for s in responded:
        for f, v in s.field_scores.items():
            per_field.setdefault(f, []).append(v)
    per_field_mean = {f: sum(vs) / len(vs) for f, vs in per_field.items()}

    return ModelReport(
        model=model,
        n=n,
        n_responded=nr,
        reliability_rate=reliability,
        schema_valid_rate=schema_rate,
        mean_field=mean_field,
        mean_hallucination=mean_hallux,
        p50_latency=p50,
        p95_latency=p95,
        mean_tokens=mean_tok,
        total_cost=total_cost,
        mean_cost=mean_cost,
        per_field=per_field_mean,
        cases=scores,
    )
