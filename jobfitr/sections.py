"""Job-posting body → labelled sections. The map from an employer's HTML to a fixed vocabulary.

WHY THIS EXISTS: 65% of the 65,291 stored bodies are raw HTML and 60% carry <h*>/<strong>
headers. Those headers are formatting, not schema — 57,189 distinct strings, 71% appearing
exactly once — so there is nothing to parse into columns directly. But the header TYPES do
recur: measured across the whole pool, 37.7% of all jobs have a responsibilities section and
35.9% have a requirements section, which is better fill than `seniority` (28%) or `category`
(12%). This maps the many names onto the few types.

THE REAL HOME IS job-radar's extraction. jobfitr is cleaning up after its dependency, so this
module is deliberately self-contained — no jobfitr imports — and can move upstream unchanged.

TWO USES, and the second is the bigger one:
  KEEP  responsibilities + requirements = what the job actually is
  DROP  benefits, compensation, about_company, eeo_legal, fraud_warning, apply_cta, metadata
        = boilerplate that is near-identical across thousands of postings, which does not just
        waste embedding budget but actively pulls unrelated jobs toward each other.
"""

from __future__ import annotations

import re

# Curly apostrophes are NOT optional: "what we're looking for" appears 792 times in the pool
# with U+2019 and zero times with an ASCII quote. Matching only ' silently loses all of them.
_APOS = "['’ʼ]"


def _p(*alts: str) -> re.Pattern:
    return re.compile("|".join(alts))


# Ordered: the first pattern that matches wins, so put the specific before the generic.
SECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("fraud_warning", _p(
        r"no financial request", r"verify communication", r"unsolicited outreach",
        r"suspect fraud", r"recruit\w* (fraud|scam)", r"beware of", r"fraudulent",
        r"impersonat", r"never ask you (for|to)")),
    ("eeo_legal", _p(
        r"equal (employment )?opportunit", r"\beeo\b", r"diversity", r"inclusion",
        r"accommodat", r"e-verify", r"privacy", r"\bitar\b", r"background check",
        r"export control", r"sponsorship", r"visa", r"commitment to", r"disclaimer",
        r"reasonable adjust")),
    ("compensation", _p(
        r"compensation", r"pay range", r"pay transparen", r"\bsalary\b", r"total reward",
        rf"what you{_APOS}?ll earn", r"\bpay and\b", r"base pay", r"equity")),
    ("benefits", _p(
        r"benefit", r"what we offer", r"\bperks?\b", r"why join", r"why work",
        r"our offer", r"wellness", r"culture (&|and) reward", r"time off")),
    ("apply_cta", _p(
        r"apply (today|now)", r"how to (get started|apply)", r"next steps",
        r"click here", r"ready to (apply|join|make)", r"interview process",
        r"hiring process", r"application process")),
    ("metadata", _p(
        r"^reports to", r"^employment type", r"^job title", r"^job id", r"^req(uisition)? ",
        r"^department\b", r"^job type", r"^work (type|schedule)")),
    ("location_travel", _p(
        r"^location", r"\btravel\b", r"\bremote\b", r"on-?site", r"work environment",
        r"physical (demand|require)", r"^schedule", r"^hours", r"relocation")),
    ("about_company", _p(
        r"about (us|the (team|company|organi))", r"who we are", r"our (mission|story|values|team)",
        r"company overview", r"^why [a-z]+\?*$", r"life at ",
        # The generic "^about <word>" used to live here and it was too greedy: it claimed
        # "About the Role", which is a RESPONSIBILITIES header, because about_company is
        # ordered first. Anchor it to an actual company name instead — i.e. anything that is
        # not one of the role words.
        r"^about (?!the role|this role|the job|the position|the opportunity|the work)[a-z]")),
    ("requirements", _p(
        r"qualification", r"requirement", rf"what we{_APOS}?re looking for",
        r"what we (are|look) for", r"who you are", r"\bskills?\b", r"experience you",
        rf"what you{_APOS}?ll bring", r"what you (bring|need|have)", r"must[- ]have",
        r"nice[- ]to[- ]have", r"basic qual", r"preferred", r"about you", r"competenc",
        r"^experience$", r"^education", r"^required", r"bonus points", r"^you are\b",
        r"minimum qual", r"technical stack", r"tech stack", r"^clearance", r"security clearance",
        r"certification", r"^tools?\b", r"^technolog")),
    ("responsibilities", _p(
        r"responsibilit", rf"what you{_APOS}?ll (do|be doing)", r"what you will do",
        r"^the role", r"about (the|this) role", r"about the (job|position|opportunity|work)",
        r"your role", r"your impact", r"the impact", r"day[- ]to[- ]day",
        r"in this role", r"^you will\b", r"job summary", r"position summary",
        r"role overview", r"^overview$", r"^summary$", r"what the job", r"duties",
        r"scope of", r"^the (job|opportunity)$", r"job description", r"key activities",
        r"a day in the life")),
]

KEEP = ("responsibilities", "requirements")
DROP = ("benefits", "compensation", "about_company", "eeo_legal",
        "fraud_warning", "apply_cta", "metadata", "location_travel")

_TAG = re.compile(r"<[^>]+>")
_HDR = re.compile(r"<(h[1-6])[^>]*>(.*?)</\1>|<(strong|b)[^>]*>(.*?)</\3>", re.I | re.S)
_ENT = (("&nbsp;", " "), ("&amp;", "&"), ("&#39;", "'"), ("&rsquo;", "’"),
        ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">"), ("&ndash;", "–"), ("&mdash;", "—"))


def _detag(s: str) -> str:
    for a, b in _ENT:
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", _TAG.sub(" ", s or "")).strip()


def classify(header: str) -> str | None:
    """A header string → one of the fixed types, or None when it is employer prose.

    None is the honest answer for most of the tail: 71% of distinct headers in the pool
    appear exactly once ("Monthly family dinner night", "Turn your love of community into a
    career"). Those are marketing, not sections.
    """
    h = _detag(header).lower().strip().strip(":").strip()
    if not h or len(h) > 70:
        return None
    for name, pat in SECTION_PATTERNS:
        if pat.search(h):
            return name
    return None


def split_sections(body: str) -> list[tuple[str | None, str, str]]:
    """-> [(type|None, header_text, section_text)] in document order.

    Text before the first header is emitted with header "" — it is usually the intro, and
    for a posting with no headers at all it is the whole body, so a caller never has to
    special-case the 40% that carry no structure.
    """
    if not body:
        return []
    marks = [(m.start(), m.end(), _detag(m.group(2) or m.group(4) or "")) for m in _HDR.finditer(body)]
    marks = [m for m in marks if m[2]]
    if not marks:
        return [(None, "", _detag(body))]
    out = []
    pre = _detag(body[: marks[0][0]])
    if pre:
        out.append((None, "", pre))
    for i, (_s, e, head) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        text = _detag(body[e:end])
        if text or head:
            out.append((classify(head), head, text))
    return out


def signal_text(body: str, fallback_chars: int = 1800) -> str:
    """The parts of a posting worth embedding: the intro plus responsibilities/requirements.

    Falls back to the cleaned head of the body when a posting yields no KEEP section, which
    is the majority case — so this degrades to today's behaviour rather than returning "".
    """
    parts = split_sections(body)
    keep = [t for kind, _h, t in parts if kind in KEEP and t]
    intro = next((t for kind, h, t in parts if kind is None and not h and t), "")
    if not keep:
        return (intro or _detag(body))[:fallback_chars]
    return " ".join([intro[:600]] + keep).strip()
