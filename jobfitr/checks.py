"""What the server checks about a pick before anyone sees it.

WHY THIS IS CODE AND NOT PROMPT. Four tester runs on 2026-11-11 (as the owner, a company
president, an implementation manager and a CTO) found the same thing four ways: the model
was TOLD the person's dealbreakers and recommended through them. A "remote only" ask got
a posting whose body says onsite five days a week; a $200K floor got a job topping out at
$135K; "no big consulting" got an Accenture subsidiary; every caveat field came back empty.
The prompt already said "do not tell them what they want to hear". This repo has measured
that ceiling before: four rounds of prompt rewording in one morning each fixed what they
named and broke something else.

So the model no longer decides whether a constraint was met. It states the constraints
once, as data (`brief`), and every pick is tested against them here. A pick that breaks a
hard rule is moved to the rejected list WITH THE REASON AND THE QUOTE, so nothing is
hidden silently: a false rejection is visible and the person can overrule it by clicking.

Everything here is a pure function of (row, brief) so it is testable without a model.
"""

from __future__ import annotations

import re

from . import store, vocab
from .match import has_term, norm_key

# ── the brief: what the person said, as data ─────────────────────────────────


def clean_brief(raw: dict | None) -> dict:
    """The model's statement of the person's constraints, coerced to known types.

    Unknown keys are dropped and wrong types become "not stated", because a brief the
    checks cannot read must degrade to FEWER rules, never to a crash mid-delivery.
    """
    raw = raw if isinstance(raw, dict) else {}

    def _strs(v, cap=12):
        return [str(x).strip() for x in (v if isinstance(v, list) else []) if str(x).strip()][:cap]

    floor = raw.get("salary_floor")
    try:
        floor = int(floor) if floor else None
    except (TypeError, ValueError):
        floor = None
    # People say "200" and mean $200,000. A floor under ANNUAL_FLOOR cannot be a yearly
    # salary, and reading it literally would make the check pass everything.
    if floor and floor < 1000:
        floor *= 1000
    groups = []
    for g in raw.get("must_mention") if isinstance(raw.get("must_mention"), list) else []:
        if isinstance(g, dict):
            words = _strs(g.get("any_of"))
            if words:
                groups.append({"label": str(g.get("label") or words[0]).strip(), "any_of": words})
    years = raw.get("experience_years")
    try:
        years = int(years) if years is not None and years != "" else None
    except (TypeError, ValueError):
        years = None
    return {
        "experience_years": years,
        "remote_only": bool(raw.get("remote_only")),
        "location": str(raw.get("location") or "").strip(),
        "will_relocate": bool(raw.get("will_relocate")),
        "salary_floor": floor,
        "exclude": _strs(raw.get("exclude"), 20),
        "no_agencies": bool(raw.get("no_agencies")),
        "must_mention": groups[:6],
    }


# ── place ────────────────────────────────────────────────────────────────────
_ANYWHERE = {"", "remote", "anywhere", "any", "us", "usa", "united states", "nationwide"}


def place_filter(location: str) -> tuple[str, str] | None:
    """A requested place -> (city_lower, STATE). None means anywhere.

    THE STATE GOES THROUGH `vocab.us_state`. It used to be `parts[1].upper()[:2]`, which
    reads "Minneapolis, Minnesota" as MI — Michigan — and "Louisville, Kentucky" as KE,
    which is nowhere. A person who spells their state out got another state's jobs or
    none, with nothing on screen to say why.
    """
    loc = (location or "").strip()
    if loc.lower() in _ANYWHERE:
        return None
    parts = [x.strip() for x in loc.split(",")]
    city = parts[0].lower()
    state = (vocab.us_state(parts[1]) or "") if len(parts) > 1 else ""
    if not state and len(parts) == 1:
        # "Kentucky" alone is a state, not a city called Kentucky.
        only = vocab.us_state(parts[0])
        if only:
            return ("", only)
    return (city, state)


def physically_in(row: dict, place: tuple[str, str]) -> bool:
    """Does the row itself name this place? Says nothing about remote."""
    city, state = place
    text = f"{row.get('location') or ''} {row.get('city') or ''}".lower()
    if city and city in text:
        return True
    if state and (row.get("state") or "").upper() == state:
        # Same state counts as "in or near" only when no city was asked for, or the row
        # names no city of its own. Louisville and Paducah are both KY and 220 miles apart.
        return not city or not (row.get("city") or "").strip()
    return False


def in_place(row: dict, place: tuple[str, str]) -> bool:
    """Is this row AVAILABLE to someone in `place`? Remote rows are, by definition."""
    if (row.get("remote") or "") == "remote":
        return True
    city, state = place
    if state and (row.get("state") or "").upper() == state:
        return True
    return physically_in(row, place)


# ── pay ──────────────────────────────────────────────────────────────────────
_K = re.compile(r"([\d,]+(?:\.\d+)?)\s*[kK]\b")
_PLAIN = re.compile(r"[\d,]{3,}")


_NOT_ANNUAL_USD = re.compile(
    r"[€£¥₹]|\b(?:CAD|EUR|GBP|INR|JPY|AUD|CHF)\b|/\s*(?:h|mo|wk|week|day)|\b(?:per|an?)\s+(?:hour|month|week|day)\b"
    r"|\b(?:hourly|monthly|weekly|daily)\b", re.I)


def _money(n: float) -> str:
    return f"${int(round(n)):,}"


def _structured(row: dict) -> bool:
    return isinstance(row.get("salary_min"), (int, float)) and row["salary_min"] > 0


def _figures(s: str) -> list[float]:
    figs = [float(m.group(1).replace(",", "")) * 1000 for m in _K.finditer(s)]
    return figs + [float(n.replace(",", "")) for n in _PLAIN.findall(_K.sub(" ", s))]


def pay_estimate(row: dict) -> tuple[float, float] | None:
    """Adzuna's PREDICTED salary as (low, high), or None when the pay is the employer's.

    The engine keeps the prediction in `salary_estimated_*` and leaves `salary_min` empty,
    but still writes the display string from it as one figure twice ("$61,630–$61,630").
    Read as stated pay, a model's guess passed or failed a person's floor.
    """
    if _structured(row):
        return None
    lo, hi = row.get("salary_estimated_min"), row.get("salary_estimated_max")
    if isinstance(lo, (int, float)) and lo > 0:
        return (float(lo), float(hi) if isinstance(hi, (int, float)) and hi >= lo else float(lo))
    # Older rows lack the estimate columns: Adzuna plus a one-figure string is the same tell.
    figs = set(_figures(str(row.get("salary") or "")))
    if str(row.get("source") or "").lower() == "adzuna" and len(figs) == 1:
        return (figs.pop(),) * 2
    return None


# Hourly only. Weekly, monthly and other currencies stay "not stated" (`_NOT_ANNUAL_USD`).
_HOURLY = re.compile(r"/\s*(?:hr|hour|h)\b|\b(?:per|an?)\s+hour\b|\bhourly\b", re.I)
_RATE = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{1,2})?)(?![\d,%])")
HOURS_A_YEAR = 2080


def hourly_pay(row: dict) -> tuple[float, float] | None:
    """("$18 - $20 an hour") -> (18.0, 20.0), or None when the string is not plainly hourly USD."""
    s = str(row.get("salary") or "")
    if not _HOURLY.search(s) or _NOT_ANNUAL_USD.search(_HOURLY.sub(" ", s)):
        return None
    rates = [float(x) for x in _RATE.findall(s)]
    # Anything outside a plausible hourly wage is odd, and odd stays "not stated".
    if not rates or not all(5 <= r <= 500 for r in rates):
        return None
    return (min(rates), max(rates))


def _rate(x: float) -> str:
    return f"${x:,.2f}".removesuffix(".00")


def pay_range(row: dict) -> tuple[float | None, float | None]:
    """(low, high) as annual USD, or (None, None) when no trustworthy figure is stated.

    `store.annual_salary` is the source of truth for the LOW end and for whether the
    figure can be trusted at all (currency, period). The high end rides on the same
    period, and falls back to the display string's largest figure.
    """
    if pay_estimate(row):
        return (None, None)
    lo = store.annual_salary(row)
    hr = hourly_pay(row) if lo is None and not _structured(row) else None
    if hr:
        return (hr[0] * HOURS_A_YEAR, hr[1] * HOURS_A_YEAR)
    if lo is None:
        # THE DISPLAY STRING, when the structured figure is absent. Measured on the first
        # acceptance runs (2026-11-11): 9 of 12 picks that PRINTED a salary carried no
        # `salary_min`, so cards read "$199,000 - $235,000" with "No salary listed"
        # directly underneath. Live-lane rows often arrive with only the string.
        # Only a string that is plainly annual US dollars is read; anything hourly,
        # monthly or in another currency stays "not stated" rather than being guessed.
        raw = str(row.get("salary") or "")
        if raw and not _structured(row) and not _NOT_ANNUAL_USD.search(raw):
            first = store._first_figure(raw)
            if first and first >= store.ANNUAL_FLOOR:
                lo = float(first)
    if lo is None:
        return (None, None)
    hi = None
    raw_lo, raw_hi = row.get("salary_min"), row.get("salary_max")
    if isinstance(raw_hi, (int, float)) and isinstance(raw_lo, (int, float)) and raw_lo > 0 and raw_hi >= raw_lo:
        hi = float(raw_hi) * (lo / float(raw_lo))
    else:
        figs = [f for f in _figures(str(row.get("salary") or "")) if f >= lo]
        # Only trust a string-derived top when it is the same order of magnitude; an
        # hourly "$33–$41" beside an annualised low would otherwise read as nonsense.
        if figs and max(figs) <= lo * 4:
            hi = max(figs)
    return (lo, hi if hi is not None else lo)


def pay_label(row: dict) -> str:
    """What the card prints. Never "$61,630–$61,630" — a range of one number is a number."""
    est = pay_estimate(row)
    if est:
        fig = _money(est[0]) if round(est[0]) == round(est[1]) else f"{_money(est[0])}–{_money(est[1])}"
        return f"about {fig} (Adzuna's estimate)"
    s = " ".join(str(row.get("salary") or "").split())
    if s:
        m = re.match(r"^(\$?[\d,.]+\s*[kK]?)\s*(?:-|–|—|to)\s*(\$?[\d,.]+\s*[kK]?)(.*)$", s)
        if m and m.group(1).replace("$", "").strip().lower() == m.group(2).replace("$", "").strip().lower():
            s = f"{m.group(1).strip()} {m.group(3).strip()}".strip()
        return s
    lo, hi = pay_range(row)
    if lo is None:
        return ""
    return _money(lo) if not hi or round(hi) == round(lo) else f"{_money(lo)} – {_money(hi)}"


# ── what the posting's own text says ─────────────────────────────────────────
_NUM = r"(?:\d|one|two|three|four|five)"
_SITE = r"(?:on-?site|in[- ]office|in the office|in[- ]person|at (?:the|our) office)"
_ONSITE = [
    re.compile(rf"\b{_SITE}\b[^.;\n]{{0,50}}\b{_NUM}\s*\+?\s*(?:days?|x)\b", re.I),
    re.compile(rf"\b{_NUM}\s*\+?\s*days?\s*(?:a|per|each|/)\s*week\b[^.;\n]{{0,30}}\b{_SITE}\b", re.I),
    re.compile(rf"\b(?:fully|100%|full[- ]time)\s*{_SITE}\b", re.I),
    re.compile(r"\b(?:is|are)\s+not\s+(?:a\s+)?remote\b", re.I),
    re.compile(r"\b(?:no|without|not eligible for)\s+remote\s+(?:options?|work|working)?\b", re.I),
    re.compile(rf"\bthis\s+(?:is\s+an?|role\s+is|position\s+is|job\s+is)\s+(?:an?\s+)?(?:{_SITE}|hybrid)\b", re.I),
    re.compile(rf"\b(?:must|required to|expected to|need to)\s+(?:be\s+able\s+to\s+)?(?:work|commute|report|be)\s+{_SITE}\b", re.I),
    re.compile(r"\b(?:on-?site|in[- ]person|in[- ]office)\s+(?:role|position|job)\b", re.I),
    re.compile(r"\bhybrid\s+(?:role|position|schedule|work\s+model)\b", re.I),
]
# Phrases that contain "on-site" and are NOT a statement that the job is on-site. Without
# these, "remote, with occasional on-site customer visits" reads as an office job. The bare
# words "customer" and "client" are NOT here: they excused "on-site 5 days a week at our
# client office". "visit" and "travel" carry the real excuse.
_ONSITE_NOT = re.compile(
    r"visit|travel|optional|not required|no requirement|if you (?:like|prefer|want)"
    r"|welcome to|on-?site (?:gym|parking|cafeteria|childcare|interview)", re.I)

_AGENCY = re.compile(
    # No `'s`: "our client's success is our success" is a vendor, not an agency. And no `\b`
    # after the comma, which made "Our client, a leading manufacturer, needs…" never match.
    r"\bon behalf of (?:our|a|its) client\b|\bour client(?:,|\s(?:is|has|seeks|needs)\b)"
    r"|\bstaffing (?:agency|firm|company)\b|\brecruit(?:ing|ment) (?:agency|firm)\b"
    r"|\bclient is (?:seeking|looking|hiring)\b", re.I)

# Large US staffing firms, matched against the WHOLE employer name (case, punctuation and a
# trailing Inc/LLC ignored). A posting from one often reads like any other employer's.
STAFFING_FIRMS = frozenset((
    "insight global", "teksystems", "robert half", "aerotek", "randstad", "adecco", "kforce",
    "kelly services", "manpower", "manpowergroup", "jobot", "cybercoders", "actalent",
    "apex systems", "beacon hill", "beacon hill staffing group", "collabera", "motion recruitment",
    "aston carter", "experis", "akkodis", "addison group", "creative circle", "spherion",
    "express employment professionals", "peopleready", "staffmark",
))


def staffing_firm(company: str) -> bool:
    name = re.sub(r"[^a-z0-9]+", " ", (company or "").lower()).strip()
    return re.sub(r"\s+(?:inc|llc)$", "", name) in STAFFING_FIRMS

_YEARS = re.compile(r"\b(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)\b", re.I)


def _sentence(text: str, start: int, end: int, cap: int = 170) -> str:
    """The sentence around a match, for quoting. A flag without its quote is an opinion."""
    a = max(text.rfind(". ", 0, start), text.rfind("\n", 0, start), text.rfind("; ", 0, start))
    a = 0 if a < 0 else a + 1
    stops = [i for i in (text.find(". ", end), text.find("\n", end), text.find("; ", end)) if i >= 0]
    b = min(stops) + 1 if stops else len(text)
    s = " ".join(text[a:b].split())
    if len(s) > cap:
        # Centre the window on the match rather than keeping a long preamble.
        rel = max(0, start - a - cap // 3)
        s = ("…" if rel else "") + " ".join(text[a + rel:a + rel + cap].split()) + "…"
    return s


def onsite_language(body: str) -> str:
    """A quote from the posting saying the job is on-site or hybrid, or "".

    The store's `remote` column comes from the source's own tag, and the tag is wrong
    often enough to matter: the Lavendo posting was tagged Remote at "San Francisco
    (Remote)" and its body required the office five days a week. The body is the
    employer's actual words; the tag is someone's dropdown.
    """
    for rx in _ONSITE:
        for m in rx.finditer(body or ""):
            q = _sentence(body, m.start(), m.end())
            if not _ONSITE_NOT.search(q):
                return q
    return ""


def agency_language(body: str) -> str:
    m = _AGENCY.search(body or "")
    return _sentence(body, m.start(), m.end()) if m else ""


def years_asked(body: str) -> tuple[int, str]:
    """(n, quote) for the largest "N+ years" the posting asks for, or (0, "")."""
    best, quote = 0, ""
    for m in _YEARS.finditer(body or ""):
        n = int(m.group(1))
        if 1 <= n <= 25 and n > best:
            q = _sentence(body, m.start(), m.end(), 140)
            # "for over 20 years we have…" is the company's age, not a requirement.
            if re.search(r"experience|background|work(?:ing)? (?:in|with|as)|minimum|at least|required|\+", q, re.I):
                best, quote = n, q
    return best, quote


def quote_in(quote: str, text: str) -> bool:
    """Is `quote` really in `text`? Whitespace, case and curly punctuation forgiven.

    This is the anti-flattery check. A reason has to point at words the employer wrote;
    "aligns well with your background" points at nothing and cannot pass.
    """
    def n(s: str) -> str:
        s = (s or "").lower().translate(str.maketrans("‘’“”–—", "''\"\"--"))
        return re.sub(r"[^a-z0-9]+", " ", s).strip()

    q = n(quote)
    return len(q) >= 12 and q in n(text)


# Reasons that say nothing. Each one was on a card in the 2026-11-11 runs, attached to a
# job that did not fit: "aligns well with your B2B SaaS experience" on a hardware role.
FLATTERY = re.compile(
    r"\baligns? (?:well|directly|perfectly|closely|nicely)\b|\b(?:great|perfect|excellent|ideal|strong) (?:fit|match)\b"
    r"|\bextremely relevant\b|\byour unique\b|\bmaking it a\b|\bleverag(?:e|ing) your\b"
    r"|\bbackground and aspirations\b|\bwell[- ]suited\b", re.I)


def company_name(row: dict) -> str:
    """The employer's name as a person would write it.

    Several ATS adapters only know a board SLUG, so cards read `bjakcareer`, `unanet`,
    `eliza`. A slug is recognisable: one lowercase token. Title-casing it and dropping the
    board suffix is right far more often than printing the slug, and it never touches a
    name that already carries a capital or a space.
    """
    c = str(row.get("company") or "").strip()
    if c and c == c.lower() and " " not in c and c.replace("-", "").replace("_", "").isalnum():
        base = re.sub(r"(?:[-_]?(?:careers?|jobs|hq|hiring|inc|llc))+$", "", c) or c
        base = base if len(base) >= 3 else c
        return " ".join(w.capitalize() for w in re.split(r"[-_]", base) if w)
    return c


def age_days(row: dict) -> int | None:
    try:
        from job_radar.util import age_int

        return age_int(row.get("posted"))
    except Exception:  # noqa: BLE001 — an unparseable date is "unknown", not a fault
        return None


# ── the verdict on one pick ──────────────────────────────────────────────────
STALE_DAYS = 45


def check_pick(row: dict, brief: dict) -> dict:
    """Test one posting against the brief.

    Returns {"reject": str | "", "flags": [str], "tag": str, "asks": str}.

    `reject` is a HARD rule broken, written for the person to read. `flags` are things
    they must know before clicking that do not disqualify. Both quote the posting where
    they can, so the check is auditable rather than another opinion.
    """
    body = store.plain(row.get("body") or "")
    title = str(row.get("title") or "")
    company = str(row.get("company") or "")
    tag = (row.get("remote") or "").lower()
    flags: list[str] = []
    reject = ""

    onsite_q = onsite_language(body)
    if onsite_q and tag in ("remote", "hybrid", ""):
        # The tag and the text disagree, or the tag is silent. The text wins and the tag
        # is not printed: a "Remote" chip above "five days a week in office" is the card
        # contradicting itself, which one tester saw.
        hybrid = bool(re.search(r"hybrid", onsite_q, re.I))
        tag = ""
        if brief["remote_only"]:
            reject = f"You asked for remote only, and the posting says: “{onsite_q}”"
        else:
            flags.append(f"{'Hybrid' if hybrid else 'On-site'} per the posting: “{onsite_q}”")
    elif brief["remote_only"] and tag in ("onsite", "hybrid"):
        reject = f"You asked for remote only; this is listed as {tag}."
    elif brief["remote_only"] and tag != "remote":
        flags.append("The posting does not say whether it is remote.")

    # PLACE ONLY EVER WARNS. The store holds place NAMES and no distances, so this cannot
    # tell New Albany, IN (five miles from Louisville) from Indianapolis. A check that
    # removed on the name would delete the Louisville search's own best results: the
    # documented one is led by Louisville, Watson, New Albany and Shepherdsville.
    place = place_filter(brief["location"])
    if not reject and place and not brief["remote_only"]:
        where = brief["location"]
        if not physically_in(row, place):
            if tag == "remote":
                flags.append(f"Remote, not based in {where}.")
            elif (row.get("location") or "").strip():
                # Worded as what the posting says, not as a verdict: "Strathmoor Manor,
                # Jefferson County" is inside Louisville, and the store cannot know that.
                flags.append(f"Listed in {row.get('location')}; you asked for {where}.")
            else:
                flags.append(f"The posting does not say where it is; you asked for {where}.")

    floor = brief["salary_floor"]
    lo, hi = pay_range(row)
    if floor and not reject:
        hr = hourly_pay(row)
        if lo is None and pay_estimate(row):
            flags.append(f"The pay shown is Adzuna's estimate; the employer stated none — your floor is {_money(floor)}.")
        elif lo is None:
            flags.append(f"No salary listed — your floor is {_money(floor)}.")
        elif hi is not None and hi < floor and hr and hi == hr[1] * HOURS_A_YEAR:
            reject = (f"It pays up to {_rate(hr[1])} an hour, about {_money(hi)} a year, "
                      f"under your {_money(floor)} floor.")
        elif hi is not None and hi < floor:
            reject = f"It pays up to {_money(hi)}, under your {_money(floor)} floor."
        elif lo < floor:
            flags.append(f"The range starts at {_money(lo)}, below your {_money(floor)} floor.")

    if not reject:
        head = f"{company} {title}"
        for term in brief["exclude"]:
            if has_term(term, head):
                reject = f"You ruled out “{term}”, and it is in the employer or title."
                break
            if has_term(term, body):
                # A subsidiary names its parent in a recognisable way ("NeuraFlash, Part
                # of Accenture" was the 2026-11-11 case). That removes. Any other mention
                # ("you will work alongside Accenture teams") only warns: a partner or a
                # customer is not the employer.
                # "Our founders were part of Accenture teams" is history, hence the lookahead.
                own = re.search(
                    r"(?:part of|a division of|a subsidiary of|owned by|acquired by|a member of"
                    r"|parent company,?(?:\s+is)?|an?)\s+(?:the\s+)?" + re.escape(term)
                    + r"(?:\s+(?:company|group|family|business|subsidiary|division))?(?!\s+teams?\b)",
                    body, re.I)
                if own and re.search(r"part of|division|subsidiary|owned|acquired|member|company|group|family|business",
                                     own.group(0), re.I):
                    q = _sentence(body, own.start(), own.end(), 140)
                    reject = f"You ruled out “{term}”, and the posting says: “{q}”"
                    break
                # Any OTHER mention says nothing about who the employer is. It warned at
                # first, and the first real run printed "Mentions Block, which you ruled
                # out" on a card whose posting listed where its engineers used to work.
                # The model apologised for the warning in its closing message.

    if brief["no_agencies"] and not reject:
        aq = agency_language(body)
        if staffing_firm(company):
            reject = f"You ruled out agencies, and {company} is a staffing firm."
        elif aq:
            reject = f"You ruled out agencies, and this reads as one: “{aq}”"

    text = f"{title} {body}"
    for g in brief["must_mention"]:
        if not any(has_term(w, text) for w in g["any_of"]):
            flags.append(f"The posting never mentions {g['label']}.")

    age = age_days(row)
    if age is not None and age > STALE_DAYS:
        flags.append(f"Posted {age} days ago — it may already be filled.")

    # SENIORITY IS A WARNING, sized to the person. An Oracle role asking 8+ years went to
    # a production manager as "aligns well" (2026-11-11). With no stated experience only
    # a large ask is worth a line; "5+ years" on every card of a senior search is noise.
    n, yq = years_asked(body)
    have = brief.get("experience_years")
    worth = n >= 3 and n > have if have is not None else n >= 8
    return {"reject": reject, "flags": flags, "tag": tag,
            "asks": f"Asks for {n}+ years: “{yq}”" if worth else ""}


def link_kind(row: dict) -> str:
    """"employer" when the link is the employer's own posting, else the middleman's host.

    The front page promises every card says which it is before the click. Until
    2026-11-11 every card read "Apply on the employer's site" including the Adzuna ones.
    """
    from urllib.parse import urlparse

    host = (urlparse(str(row.get("url") or "")).hostname or "").lower()
    for a in store.CARRIED_AGGREGATORS:
        if host == a or host.endswith("." + a):
            return a.split(".")[0]
    return "employer"


def brief_lines(brief: dict) -> list[str]:
    """The brief as short phrases a person can read, for the search steps.

    The brief is the model's reading of what the person said, and every removal follows
    from it. Showing it is what lets them catch a wrong reading.
    """
    out = []
    if brief["remote_only"]:
        out.append("remote only")
    if brief["location"]:
        out.append(("based in " if brief["remote_only"] else "in or near ") + brief["location"])
    if brief["salary_floor"]:
        out.append(f"{_money(brief['salary_floor'])} or more")
    if brief["exclude"]:
        out.append("not " + ", ".join(brief["exclude"]))
    if brief["no_agencies"]:
        out.append("no agencies")
    for g in brief["must_mention"]:
        out.append("should involve " + g["label"])
    return out


def same_job(a: dict, b: dict) -> bool:
    """Two rows that are one job posted twice (two urls, two cities, one role)."""
    return (norm_key(a.get("company")), norm_key(a.get("title"))) == (
        norm_key(b.get("company")), norm_key(b.get("title")))


# ── is the posting still up? ─────────────────────────────────────────────────
_DEAD_STATUSES = frozenset({404, 410})
_DEAD_TEXT = re.compile(
    r"job not found|no longer (?:available|accepting|open)|position (?:has been|is) (?:filled|closed)"
    r"|posting (?:has|is) (?:expired|closed)|this job (?:has|is) (?:expired|closed|no longer)", re.I)
LIVE_TIMEOUT = 6.0


def dead_urls(urls: list[str]) -> set[str]:
    """Which of these postings are GONE, asked directly, in parallel.

    Only positive evidence counts, the same rule `store.verify_unpolled` keeps: a 404/410,
    a redirect that lands on a board's error page, or a closed-posting sentence in a small
    page. A timeout, a 403 or a 5xx is the site declining to answer, never proof the job
    closed. Being wrong in that direction would hide a live job.

    Run on the handful being recommended, not the pool: an Ashby pick on 2026-11-11 was the
    TOP recommendation and opened on "Job not found".
    """
    import httpx
    from concurrent.futures import ThreadPoolExecutor

    def check(url: str) -> bool:
        try:
            with httpx.Client(timeout=LIVE_TIMEOUT, follow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0 (compatible; jobfitr-liveness/1.0)"}) as c:
                r = c.get(url)
            if r.status_code in _DEAD_STATUSES:
                return True
            if r.status_code >= 400:
                return False
            final = str(r.url)
            if "error=true" in final and "error=true" not in url:
                return True  # greenhouse bounces a withdrawn job to <board>?error=true
            head = r.text[:80000]
            # Ashby serves its app shell for a closed posting; the embedded state says so.
            # Measured 2026-11-11: a live page carries `"posting":{"id"`, a gone one null.
            if "ashbyhq.com" in final and '"posting":null' in head:
                return True
            # A closed-posting notice is short. A long page that happens to contain the
            # words (a FAQ, a footer) is not treated as evidence.
            return bool(_DEAD_TEXT.search(head)) and len(store.plain(head)) < 4000
        except Exception:  # noqa: BLE001 — a network fault is not evidence about the job
            return False

    urls = [u for u in urls if u]
    if not urls:
        return set()
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        return {u for u, dead in zip(urls, pool.map(check, urls)) if dead}
