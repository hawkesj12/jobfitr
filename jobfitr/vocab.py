"""jobfitr's controlled vocabularies — the opinion layer over job_radar's fidelity.

job_radar reports what each source said, faithfully and in that source's own words. That is
correct for an engine and useless for a facet: measured on a 21,495-row harvest, `category`
arrives as **487 distinct values** and `seniority` as **34**, because seven sources each speak
their own dialect and nobody reconciled them.

    Engineering · Engineering Jobs · Software Engineering · Science and Engineering
    Healthcare · Healthcare & Nursing Jobs
    senior · Senior · Senior Level        Mid Level · Mid-level · Midweight

A drawer offering 487 "Field" options is not a filter. This module is where jobfitr decides
what those values MEAN — which is an opinion, and therefore ours, not the engine's.

WHY THESE CANONICAL SETS. `category` is The Muse's published 20-value taxonomy PLUS two
additions of ours — Customer Service and Supply Chain, both of which several sources emit and
neither of which The Muse names — so CATEGORIES holds 22. Three reasons for starting there,
in order of weight: one of our sources already emits exactly it (1,958 rows need no mapping at
all); it is deliberately not tech-centric, covering Animal Care, Food and Hospitality, Retail and
Installation/Maintenance — the sectors this corpus is thin on; and adopting a published taxonomy
means every value is one another source can be mapped ONTO rather than one we invented and then
have to defend.

Every mapping below was decided by reading real titles under the raw value, never by the label:
`adzuna: IT Jobs` holds "iOS Software Engineer" and "Head of Engineering, AI" -> Software
Engineering, while `adzuna: Engineering Jobs` holds "Civil Engineer" and "Sr. Principal
Electrical Integration Engineer" -> Science and Engineering. The two would have been merged by
anyone mapping on the word "engineering".
"""

from __future__ import annotations

import html

# ── category ─────────────────────────────────────────────────────────────────
# The Muse's published taxonomy, verbatim. Do not extend casually: every value added
# here is one more option in the drawer, and the whole point is that the drawer is short.
CATEGORIES = (
    "Account Management",
    "Accounting and Finance",
    "Advertising and Marketing",
    "Animal Care",
    "Business Operations",
    "Customer Service",
    "Data and Analytics",
    "Design",
    "Education",
    "Food and Hospitality Services",
    "Healthcare",
    "Human Resources and Recruitment",
    "Installation, Maintenance, and Repairs",
    "Legal Services",
    "Management",
    "Product Management",
    "Project Management",
    "Retail",
    "Sales",
    "Science and Engineering",
    "Software Engineering",
    "Supply Chain",
)

# Raw value (lowercased) -> canonical. Sources noted where the choice was not obvious.
_CATEGORY_MAP = {
    # ── The Muse: already canonical, listed so the map is the single source of truth ──
    "account management": "Account Management",
    "accounting and finance": "Accounting and Finance",
    "advertising and marketing": "Advertising and Marketing",
    "animal care": "Animal Care",
    "business operations": "Business Operations",
    "data and analytics": "Data and Analytics",
    "education": "Education",
    "food and hospitality services": "Food and Hospitality Services",
    "healthcare": "Healthcare",
    "human resources and recruitment": "Human Resources and Recruitment",
    "installation, maintenance, and repairs": "Installation, Maintenance, and Repairs",
    "legal services": "Legal Services",
    "management": "Management",
    "product management": "Product Management",
    "project management": "Project Management",
    "retail": "Retail",
    "sales": "Sales",
    "science and engineering": "Science and Engineering",
    "software engineering": "Software Engineering",
    # ── adzuna: "<Field> Jobs". The suffix is the tell, the content is not. ──
    "it jobs": "Software Engineering",  # iOS Software Engineer, Head of Engineering AI
    "engineering jobs": "Science and Engineering",  # Civil, Electrical Integration, Field Service
    "teaching jobs": "Education",
    "healthcare & nursing jobs": "Healthcare",
    "accounting & finance jobs": "Accounting and Finance",
    "sales jobs": "Sales",
    "hr & recruitment jobs": "Human Resources and Recruitment",
    "logistics & warehouse jobs": "Supply Chain",
    "trade & construction jobs": "Installation, Maintenance, and Repairs",
    "hospitality & catering jobs": "Food and Hospitality Services",
    "retail jobs": "Retail",
    "legal jobs": "Legal Services",
    "customer services jobs": "Customer Service",
    "manufacturing jobs": "Installation, Maintenance, and Repairs",
    "social work jobs": "Healthcare",
    "admin jobs": "Business Operations",
    "creative & design jobs": "Design",
    "scientific & qa jobs": "Science and Engineering",
    "consultancy jobs": "Business Operations",
    "pr, advertising & marketing jobs": "Advertising and Marketing",
    "energy, oil & gas jobs": "Science and Engineering",
    "property jobs": "Business Operations",
    "travel jobs": "Food and Hospitality Services",
    "charity & voluntary jobs": "Business Operations",
    "domestic help & cleaning jobs": "Installation, Maintenance, and Repairs",
    "maintenance jobs": "Installation, Maintenance, and Repairs",
    "graduate jobs": "Unknown",  # a LEVEL, not a field
    "part time jobs": "Unknown",  # an arrangement, not a field
    "other/general jobs": "Unknown",
    # ── smartrecruiters: its own function.label ──
    # "Engineering" here is GENERAL engineering — sampled titles include HVAC, automotive
    # key account, and mechanical CAD/CFD alongside DevOps. Mapping it to Software
    # Engineering on the word alone would be wrong.
    "engineering": "Science and Engineering",
    "information technology": "Software Engineering",  # SAP consultant, IT Support Analyst
    "manufacturing": "Installation, Maintenance, and Repairs",
    "production": "Installation, Maintenance, and Repairs",
    "quality": "Science and Engineering",
    "purchasing": "Supply Chain",
    "supply chain": "Supply Chain",
    "logistics": "Supply Chain",
    "finance": "Accounting and Finance",
    "accounting": "Accounting and Finance",
    "marketing": "Advertising and Marketing",
    "human resources": "Human Resources and Recruitment",
    "customer service": "Customer Service",
    "customer support": "Customer Service",
    "administrative": "Business Operations",
    "operations": "Business Operations",
    "legal": "Legal Services",
    "research": "Science and Engineering",
    "design": "Design",
    "product": "Product Management",
    # ── himalayas / jobicy / remotive / braintrust ──
    "developer": "Software Engineering",  # Engineering Manager, Founding Engineer
    "data science": "Data and Analytics",
    "devops": "Software Engineering",
    "software development": "Software Engineering",
    "customer support &amp; success": "Customer Service",
    "customer support & success": "Customer Service",
    "sales &amp; marketing": "Sales",
    "sales & marketing": "Sales",
    "hr": "Human Resources and Recruitment",
    "it": "Software Engineering",
    # ── the measured tail: everything appearing 2+ times in the capture ──
    "accounting & auditing": "Accounting and Finance",
    "finance & accounting": "Accounting and Finance",
    "business development": "Sales",
    "marketing & sales": "Sales",
    "quality assurance": "Science and Engineering",
    "content creator": "Advertising and Marketing",
    "hardware engineer": "Science and Engineering",
    "technical support": "Customer Service",
    "analyst": "Data and Analytics",
    "data": "Data and Analytics",
    "consulting": "Business Operations",
    "general business": "Business Operations",
    "legal & compliance": "Legal Services",
    "project & program management": "Project Management",
    "product & operations": "Product Management",
    "healthcare & medical": "Healthcare",
    "healthcare & medicine": "Healthcare",
    "health care provider": "Healthcare",
    "science & research": "Science and Engineering",
    "distribution": "Supply Chain",
    "writing & editing": "Advertising and Marketing",
    "art & design": "Design",
    "training": "Education",
    # ── explicit unknowns: the source said nothing useful ──
    "unknown": "Unknown",
    "other": "Unknown",
    "general": "Unknown",
}

# ── seniority ────────────────────────────────────────────────────────────────
# An ORDERED ladder. Order matters: a multi-valued source ("Entry-level, Mid-level")
# maps to the LOWEST rung, so a search for the junior end still finds a job open to it.
# The opposite convention hides entry-level roles from the people who need them most.
SENIORITY_LEVELS = (
    "intern",
    "entry",
    "mid",
    "senior",
    "staff",
    "lead",
    "director",
    "executive",
)

_SENIORITY_MAP = {
    "internship": "intern",
    "intern": "intern",
    "entry level": "entry",
    "entry-level": "entry",
    "junior": "entry",
    "associate": "entry",  # the consulting/finance entry rung
    "mid level": "mid",
    "mid-level": "mid",
    "midweight": "mid",
    "intermediate": "mid",
    "senior": "senior",
    "senior level": "senior",
    "mid-senior level": "senior",
    "staff": "staff",
    "principal": "staff",
    "lead": "lead",
    "manager": "lead",
    "director": "director",
    "executive": "executive",
    "chief": "executive",
    "vp": "executive",
    "c-level": "executive",
}

_SENIORITY_RANK = {v: i for i, v in enumerate(SENIORITY_LEVELS)}


def _clean(raw) -> str:
    """Lowercase, unescape, and collapse the separators sources disagree about.

    `html.unescape` is not cosmetic: several sources send `&amp;` literally, so
    "Marketing &amp; Sales" and "Marketing & Sales" arrive as two distinct values and both
    miss a map keyed on either one. Measured: 5 such pairs in the capture."""
    s = html.unescape(str(raw or "")).strip().lower()
    return " ".join(s.replace("/", " & ").split())


def category(raw) -> str | None:
    """A source's category string -> one of CATEGORIES, or None.

    None means "we do not know what field this is", which is a real answer and better than
    a wrong one: an unmapped value in a drawer is a dead filter option that returns rows the
    user did not ask for. Callers should treat None as unfiltered, never as a category.
    """
    s = _clean(raw)
    if not s:
        return None
    hit = _CATEGORY_MAP.get(s)
    if hit == "Unknown":
        return None
    if hit:
        return hit
    # A comma-joined list (usajobs sends occupational series this way) -> first mappable part.
    if "," in s:
        for part in s.split(","):
            hit = _CATEGORY_MAP.get(part.strip())
            if hit and hit != "Unknown":
                return hit
    return None


def seniority(raw) -> str | None:
    """A source's level string -> one of SENIORITY_LEVELS, or None.

    Multi-valued input resolves to the LOWEST rung present — see SENIORITY_LEVELS.
    None means the source did not say, and must never be rendered as a level.
    """
    s = _clean(raw)
    if not s or s in ("not applicable", "any", "n/a"):
        return None
    hit = _SENIORITY_MAP.get(s)
    if hit:
        return hit
    found = [
        _SENIORITY_MAP[p.strip()] for p in s.split(",") if p.strip() in _SENIORITY_MAP
    ]
    return min(found, key=lambda x: _SENIORITY_RANK[x]) if found else None


# ── US state ─────────────────────────────────────────────────────────────────
# An ALLOWLIST, for the same reason `category` is one: the field is only useful if it
# holds a small closed set. job_radar emits `state` for 34% of rows and it arrives in
# three vocabularies at once. Measured on the live store, 6,382 rows carry a state
# across **126 distinct values** — the US has 50 plus DC:
#
#     6,159 rows   53 real USPS codes                        CA, NY, TX, …
#       ~120 rows  the same states SPELLED OUT               'Ohio', 'Mass', 'Washington, D.C.'
#       ~100 rows  not the US at all                         'Ontario', 'Berlin', 'Dubai', 'SP'
#
# Two jobs, therefore. Fold the spelled-out ones onto their code so the drawer offers
# "OH" once instead of "OH" and "Ohio" separately. And treat the third group as what it
# is: **positive evidence the posting is not American.** That matters beyond display —
# `servable_in_us` could previously only test `country`, and the leak it documents is
# exactly a row whose country is blank while its state says Berlin.
US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY "
    "DC PR VI GU AS MP".split()
)

# Full names -> code. Every entry below was READ OFF the live store, not copied from a
# gazetteer, so the list covers the spellings sources actually send (including 'Mass'
# and both punctuations of Washington DC) and nothing speculative.
_STATE_NAMES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "mass": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    # DC arrives punctuated three ways. There is no ordering here to rely on — this is a
    # dict, and lookup is exact. What makes it work is that us_state() strips ALL
    # punctuation before the lookup, so 'Washington, D.C.' and 'Washington, DC' both
    # normalise to the distinct key 'washington dc' and never collide with bare
    # 'washington' -> WA.
    "district of columbia": "DC",
    "washington d c": "DC",
    "washington dc": "DC",
    "puerto rico": "PR",
    "guam": "GU",
}


def us_state(raw) -> str | None:
    """A source's subdivision string -> a USPS code, or None when it is not a US state.

    None means "not a US state" — either the field was empty or it named somewhere
    else ('Ontario', 'Berlin', 'SP'). Those two cases are NOT distinguished, because
    the one caller that wanted the distinction turned out not to need it: a foreign
    subdivision never appears without a foreign country, so US-only intake already
    drops those rows on the country test alone (measured: 0 additional rows).
    """
    # Punctuation to spaces, not just commas: 'Washington, D.C.' and 'Washington, DC'
    # are the same place and both arrive in the live store. Stripping only the comma
    # left 'washington d.c.' unmatched and silently foreign — which then fed
    # is_foreign_state and would have DROPPED real DC jobs.
    s = " ".join("".join(ch if ch.isalnum() else " " for ch in _clean(raw)).split())
    if not s:
        return None
    up = s.upper()
    if up in US_STATES:
        return up
    return _STATE_NAMES.get(s)
