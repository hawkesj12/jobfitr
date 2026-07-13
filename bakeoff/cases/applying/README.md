# Applying gold cases

Each file is one hand-labeled extraction case: a chat `transcript` and the
`expected` config a correct model should extract from it. These are the **ground
truth** the deterministic scorer (`bakeoff/scoring.py`) grades against — no LLM
judge is involved, which is what makes the applying result reproducible and
hard to argue with.

## Format

```json
{
  "id": "001-zookeeper",
  "notes": "why this case exists / what it stresses",
  "transcript": "User: ...\nAssistant: ...\nUser: ...",
  "expected": {
    "titles": ["zookeeper", "animal keeper"],
    "boosts": ["reptiles", "biology degree"],
    "exclude": ["intern", "volunteer"],
    "rank_down": ["staffing", "agency"],
    "location": "Louisville, KY",
    "remote_only": false,
    "max_age_days": 30,
    "min_score": "plenty"
  }
}
```

## Labeling rules (so cases stay fair)

- **Only label fields the transcript actually supports.** The scorer grades only
  the fields present in `expected`; leave a field out if the user never gave it,
  rather than guessing a default. This avoids penalizing a model for not
  inventing something.
- **Lists hold the user's own words**, lightly normalized. The scorer runs both
  sides through `config_builder._clean_list` (lowercase, dedupe, split), so don't
  fuss over casing — capture the concepts.
- **`min_score`** is one of `plenty | balanced | strong` (the app's pickiness
  keywords). Map "show me lots" → plenty, "only the best" → strong.
- **`location`** is a place string, or `"remote"` / `"anywhere"`.
- **Span the audience.** The set must include non-tech roles (the app's whole
  premise), vague/underspecified answers, and voice-to-text-style rambling —
  not just clean tech requests. That's where free models diverge.

## Coverage target

Start ~12–15 cases (enough to separate the field on schema-validity), grow to
~30 before trusting fine-grained field rankings (see PLAN.md open questions).
