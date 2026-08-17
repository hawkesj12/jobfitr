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
import re
import unicodedata
from functools import lru_cache

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


# ── reading a place out of free-text location ────────────────────────────────
# `servable_in_us` could only ever test the `country` COLUMN, and 14,616 of 31,790 rows
# arrive with it blank — so "we don't know" and "this is fine" were the same answer, and
# ~5,800 foreign jobs reached a board the README calls US-only. On a real remote user's
# 200 cards that is 20-27%; on the owner's own live search, 11 of the top 20.
#
# The engine is not at fault and must not be changed: job_radar's `split_place()` returns
# all-None for a comma-less string and refuses a two-letter tail, deliberately — "a None
# here costs a filter; a wrong city is a permanently wrong row". It says "I don't know".
# Reading the raw text is jobfitr's job, because US-only is jobfitr's opinion.
#
# COMPOSITION of those 14,616, measured on frozen-8k.db with the engine's own 60 curated
# country names — no list invented here:
#
#     3,516  name a FOREIGN country only              -> foreign
#     2,564  name the US only                         -> US, and that is affirmative
#       157  name BOTH ("United States or Canada")    -> US WINS, keep
#     8,379  name no country at all
#              ...of which 2,435 name a foreign CITY
#
# TWO TRAPS, both found by running code rather than reading it:
#
#   1. A BARE TWO-LETTER CODE IS NOT EVIDENCE. Seven of the engine's country codes are
#      also USPS state codes: AR CA CO DE ID IL IN. A prototype "rescued" `Berlin, DE;
#      Hamburg, DE; Munich, DE` as American because DE is Delaware. Codes are read ONLY
#      through us_state(), never as country evidence.
#
#   2. SUBSTRING MATCHING ON COUNTRY NAMES IS WORSE. That same prototype built its US
#      token set by splitting "united states", so the bare token `united` matched
#      `United Kingdom` — 488 rows silently kept. Every name here is matched WHOLE-WORD
#      and longest-first.
#
# US-AFFIRMATIVE WINS. Measured: of 2,435 rows naming a foreign city, 110 also carry a US
# signal, and all 110 are correct keeps — `Cambridge, MA USA` x47, `Manchester, NH`,
# `Vienna, Virginia`, `Hybrid - San Francisco, New York City, London, Berlin`. The
# Dublin-CA / Berlin-CT / Toronto-OH collision fear is fully absorbed by that precedence
# rule; it is not a separate risk the city list introduces.
_LOC_SCAN_CHARS = 120
# `location` is not always a location: 1,027 rows exceed 60 chars, 394 exceed 100, and the
# longest is 2,158 — greenhouse and HN rows where the whole job body landed in the field
# (one is a 900-word pitch). A bag-of-words scan over that matches any place mentioned in
# passing, so the scan is bounded to the head, where a real location lives.


@lru_cache(maxsize=2048)
def _loc_tokens(text: str) -> str:
    """Lowercased, accent-folded, punctuation-flattened, bounded head of a location.

    ACCENTS ARE FOLDED because the vocabularies below are ASCII and the sources are not:
    `Türkiye, Remote` read as unknown until this existed, since `ü` is alnum and survived
    the punctuation pass while `turkiye` is what the list holds. Same class as `Bogotá`,
    `São Paulo`, `Zürich`, `Malmö` — NFKD splits the accent into a combining mark and the
    category test drops it, leaving the ASCII letter behind.
    """
    head = unicodedata.normalize("NFKD", (text or "")[:_LOC_SCAN_CHARS])
    head = "".join(ch for ch in head if not unicodedata.combining(ch))
    return (
        " "
        + " ".join("".join(ch if ch.isalnum() else " " for ch in head).lower().split())
        + " "
    )


# Country NAMES come from the ENGINE'S OWN curated map, not a list invented here — 60
# entries, maintained upstream by the same people who maintain `split_place`, so it does
# not rot on jobfitr's side. Longest-first so "united states" is tested before "united
# kingdom" can be reached by a shorter alternative.
def _country_names():
    from job_radar.vocab import _COUNTRY_CODES

    return sorted(_COUNTRY_CODES.items(), key=lambda kv: -len(kv[0]))


# Foreign CITIES are the one list jobfitr owns, and it is deliberately separate from the
# country lane so it can be reverted on its own. It covers 2,435 rows the country names
# miss (London 213, Madrid 130, Amsterdam 51, Munich 49, Dublin 44, Bangalore 40).
#
# A list like this rots — but it rots SAFELY, and that direction matters. The comparable
# regex in _private/before-after/rank_test.py needed three patches in one day, every one
# of them because it MISSED a place (Utrecht, then LatAm, then Costa Rica). Missing is a
# recall failure, and a recall failure here is the status quo: a foreign job leaks
# through. It does not drift toward deleting American jobs.
_FOREIGN_CITIES = frozenset("""
london madrid amsterdam munich dublin bangalore bengaluru warsaw toronto vancouver
montreal ottawa calgary edmonton winnipeg quebec mississauga
paris lyon marseille berlin hamburg frankfurt cologne stuttgart dusseldorf
barcelona valencia seville lisbon porto milan rome turin naples florence
vienna zurich geneva bern brussels antwerp rotterdam utrecht eindhoven hague
copenhagen stockholm gothenburg oslo helsinki reykjavik
prague brno budapest bucharest sofia zagreb ljubljana bratislava krakow wroclaw gdansk
athens istanbul ankara kyiv kiev lviv minsk moscow petersburg
dubai doha riyadh jeddah kuwait manama muscat
jerusalem haifa cairo casablanca nairobi lagos accra johannesburg
durban pretoria
mumbai delhi hyderabad chennai kolkata pune gurugram gurgaon noida ahmedabad
kochi jaipur chandigarh mohali
singapore bangkok jakarta manila hanoi
tokyo osaka kyoto yokohama seoul busan taipei
beijing shanghai shenzhen guangzhou macau
sydney melbourne brisbane perth adelaide canberra auckland wellington christchurch
rio brasilia curitiba recife
santiago bogota medellin lima quito caracas montevideo asuncion
guadalajara monterrey queretaro
""".split())


# ═══════════════════════════════════════════════════════════════
# place_evidence()
# ═══════════════════════════════════════════════════════════════
# What a raw location STRING says about whether a job is American:
# "us" | "foreign" | None (it said nothing either way).
#
# US wins outright — a posting listing "United States or Canada"
# is one a US worker can take, and 110 of 110 measured rows where
# a US signal sits beside a foreign city are correct keeps.
# ═══════════════════════════════════════════════════════════════
_AMBIGUOUS_CODES = frozenset("AR CA CO DE ID IL IN".split())

# ISO 3166-1 alpha-2, for reading a country code off the TAIL of a location. Closed-world
# and ~250 entries that change roughly once a decade — the opposite maintenance profile
# from a city list. Seeded from the engine's own map and completed here, because
# job_radar's `_COUNTRY_CODES` is a curated 62 covering the names its sources SEND, not
# the full standard.
_ISO_COUNTRY_CODES = frozenset("""
AD AE AF AG AL AM AO AR AT AU AZ BA BB BD BE BF BG BH BI BJ BN BO BR BS BT BW BY BZ
CA CD CF CG CH CI CL CM CN CO CR CU CV CY CZ DE DJ DK DM DO DZ EC EE EG ER ES ET
FI FJ FM FR GA GB GD GE GH GM GN GQ GR GT GW GY HK HN HR HT HU ID IE IL IN IQ IR IS IT
JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY
MA MC MD ME MG MH MK ML MM MN MO MR MT MU MV MW MX MY MZ NA NE NG NI NL NO NP NR NZ
OM PA PE PG PH PK PL PT PW PY QA RO RS RU RW SA SB SC SD SE SG SI SK SL SM SN SO SR SS SV SY SZ
TD TG TH TJ TL TM TN TO TR TT TV TW TZ UA UG UK UY UZ VC VE VN VU WS YE ZA ZM ZW
""".split())


def _comma_segments(text) -> list[str]:
    """The comma/semicolon/slash-delimited pieces of a location, accent-folded.

    A US state arrives as the tail of `City, ST` — never as a loose word in a sentence.
    Reading segments instead of tokens is what stops the English word `or` from being
    Oregon, and it costs nothing: `Austin, TX` still yields `tx`.
    """
    t = _loc_tokens(text)  # folded + lowercased, but commas are gone by then
    raw = unicodedata.normalize("NFKD", str(text or "")[:_LOC_SCAN_CHARS])
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    out = []
    for part in re.split(r"[,;/|]|\s+-\s+", raw):
        part = " ".join(
            "".join(ch if ch.isalnum() else " " for ch in part).lower().split()
        )
        if part:
            out.append(part)
    del t
    return out

# The countries the ENGINE'S map does not carry. job_radar's `_COUNTRY_CODES` is a
# curated 60 (62 in 0.8.2, which adds only bulgaria and dominican republic) — deliberately
# the ones its sources actually send, not all ~195. Measured, the gap is real and it is
# not closed by upgrading: a random 30 of the rows this detector called "unknown" held
# Slovenia, Slovakia, Albania, Bulgaria and Türkiye, all foreign, all invisible to the
# engine's list. Completing it here catches 381 rows.
#
# A country list is CLOSED-WORLD and barely moves — a new country appears roughly once a
# decade. That is a completely different rot profile from the city list below, and the
# reason this one is safe to own.
_MORE_COUNTRIES = frozenset("""
afghanistan albania algeria andorra angola armenia azerbaijan bahrain bangladesh belarus
belize benin bhutan bolivia bosnia botswana brunei burundi cambodia cameroon chad congo
croatia cuba cyprus czechia estonia ethiopia fiji gabon gambia ghana guatemala guinea
guyana haiti honduras iceland iran iraq jamaica jordan kazakhstan kenya kosovo kuwait
kyrgyzstan laos latvia lebanon liberia libya liechtenstein lithuania luxembourg
madagascar malawi maldives mali malta mauritius moldova monaco mongolia montenegro
morocco mozambique myanmar namibia nepal nicaragua niger oman panama papua paraguay
qatar rwanda senegal serbia seychelles slovakia slovenia somalia sudan suriname syria
tajikistan tanzania togo trinidad tunisia turkmenistan turkiye uganda ukraine uruguay
uzbekistan venezuela yemen zambia zimbabwe
""".split())
_MORE_COUNTRY_PHRASES = (
    "costa rica", "ivory coast", "north macedonia", "sri lanka", "sierra leone",
)

# MULTI-WORD CITIES, as phrases. These were first written concatenated
# (`kualalumpur`, `buenosaires`, `telaviv`) into the token set above, where they could
# NEVER match: the tokenizer splits on whitespace, so the text yields `kuala` and
# `lumpur` and the joined form is unreachable. Nine entries were dead on arrival, and
# they were most of what still leaked — `Kuala Lumpur` x37, `Buenos Aires` x28,
# `Tel Aviv` x16. A silent no-op, found by measuring what the filter still let through
# rather than by reading the list.
_FOREIGN_CITY_PHRASES = (
    "kuala lumpur", "buenos aires", "tel aviv", "ho chi minh", "sao paulo",
    "cape town", "abu dhabi", "new delhi", "st petersburg", "rio de janeiro",
    "mexico city", "hong kong", "san jose costa rica", "port of spain",
)

# MULTI-COUNTRY REGIONS. A posting bounded to one of these is not a US job, and 232 rows
# say so plainly: `LatAm (Remote)` x34, `Latin America` x35, `Remote - Europe` x15.
# job-radar 0.8.1 reached the same conclusion independently and added asia/africa/oceania
# to its own default exclusions.
# `european union` is listed SEPARATELY from `europe` and that is not redundancy: the
# match is whole-word, and the token in `European Union (Remote)` is `european`, which
# `europe` never equals. 37 EU-bounded rows were being served on a US-only board — an
# ACTIVE leak, not a latent one, and invisible because the row simply looked unresolved.
_FOREIGN_REGIONS = (
    "europe", "european union", "european economic area", "eea",
    "latin america", "latam", "emea", "apac", "apj", "middle east",
    "caribbean", "nordics", "benelux", "asia pacific", "sub saharan",
)


def place_evidence(location) -> str | None:
    t = _loc_tokens(location)
    if not t.strip():
        return None

    # ── US evidence, graded, because not all of it is equally good ───────────
    # STRONG: the country spelled out, or a subdivision that is unambiguously American.
    # Matched whole-word — the bare token `united` matches "United Kingdom", which is how
    # a prototype kept 488 British rows as American.
    # `U.S.` flattens to the two tokens `u s`, and the test never looked for that form —
    # `Remote, U.S.` read as no evidence at all on 127 rows. Latent today because unknown
    # passes, but it is the prerequisite for ever inverting that default.
    strong_us = (
        " united states " in t
        or " usa " in t
        or " u s a " in t
        or " us " in t
        or " u s " in t
    )
    if not strong_us:
        # A two-letter code counts ONLY where a state actually appears — after a comma,
        # in `City, ST` form. Scanning every token instead read the English words that
        # collide with USPS codes as geography: `or` is Oregon, so `Italy or France or
        # Germany` was AMERICAN, and 130 rows in the corpus were kept on exactly that.
        # `in` `me` `hi` `ok` `de` `la` `co` `id` `ma` `pa` `oh` are all the same shape.
        # This is trap #1 in the comment block above, re-entered from the other side.
        # The FIRST TOKEN of a segment, not the whole segment. `Lima, OH (Onsite Yard)`
        # yields the segment `oh onsite yard`, and requiring an exact match meant OHIO was
        # never seen — the row was dropped as Lima, Peru. Same for `Naples, FL (10106)`
        # and `810 Olympic Dr., Athens, GA 30601`.
        #
        # AND THIS IS STRONG, not weak, even for the seven codes that are also country
        # codes. `<name>, <US state code>` is American address FORMATTING, and the
        # structure is the evidence — `Lebanon, IN` is Indiana whatever Lebanon is
        # elsewhere. Treating it as weak let the country name win and dropped real US
        # jobs in Lebanon IN, Panama City FL, Athens GA and Jordan Creek IA.
        #
        # Residue, accepted and measured: `Berlin, DE; Hamburg, DE` reads American on the
        # same rule, because DE is Delaware. One row shape, no country name to break the
        # tie, and unresolvable from the string alone.
        for seg in _comma_segments(location):
            # BOTH forms, because each catches what the other misses. The WHOLE segment
            # is how a spelled-out state arrives (`Los Alamos, New Mexico` -> `new
            # mexico`), and a first-token-only test fails it because `us_state('new')` is
            # nothing. The FIRST TOKEN is how a code arrives with trailing noise (`Lima,
            # OH (Onsite Yard)` -> `oh onsite yard`), which a whole-segment test fails.
            #
            # `New Mexico` is also why the country lane cannot be trusted alone here:
            # the substring ` mexico ` sits inside ` new mexico `, so 15 Los Alamos and
            # Albuquerque rows were being dropped as Mexican.
            toks = seg.split()
            # `Olathe KS`, `Atlanta GA`, `Denver CO` — a state code as the LAST token of a
            # segment with no comma before it. 279 rows carried no US evidence at all.
            # Safe because it is the tail: an English word that is also a code (`or`,
            # `in`, `me`) does not end a location string, and the uppercase guard below
            # applies here too.
            if (
                len(toks) > 1
                and len(toks[-1]) == 2
                and us_state(toks[-1])
                and re.search(rf"\b{toks[-1].upper()}\b", str(location or ""))
            ):
                strong_us = True
                break
            # A BARE TWO-LETTER CODE MUST BE UPPERCASE IN THE RAW STRING. The first-token
            # rule above re-armed the `or`-is-Oregon trap from the other direction:
            # `Paris, or Lyon` segments to `or lyon`, whose first token is `or`. Measured,
            # comma-tail two-letter codes in the corpus run 12,505 uppercase against 170
            # lowercase, and the lowercase ones are overwhelmingly the conjunction. Case
            # is the cheap discriminator, and it still keeps
            # `San Francisco, CA, New York, NY, Portland, OR, or Remote` — which carries
            # both spellings and is American.
            head = toks[0] if toks else ""
            code_ok = (
                len(head) == 2
                and us_state(head)
                and re.search(rf"\b{head.upper()}\b", str(location or ""))
            )
            # THE WHOLE-SEGMENT TEST MUST OBEY THE SAME CASE RULE. It did not, and that
            # bypass was worth 116 rows: `bangalore, in` segments to `in`, `us_state("in")`
            # returns Indiana, and the row read AMERICAN. Same for `Dresden, SN, de` and
            # `Toronto, ON, CA`. A two-letter segment is only a state when the raw string
            # spelled it in capitals; a longer one (`new mexico`, `ohio`) is unambiguous
            # and needs no such guard.
            seg_ok = us_state(seg) and (
                len(seg) > 2 or re.search(rf"\b{seg.upper()}\b", str(location or ""))
            )
            if seg_ok or code_ok or (len(head) > 2 and us_state(head)):
                strong_us = True
                break
    if strong_us:
        return "us"

    # MULTI-WORD STATE NAMES as a substring, because they arrive embedded rather than as
    # a clean segment: `New Mexico-Remote`, `Chicago, Montreal, New York City`,
    # `South Jordan Utah`. The 12 entries in _STATE_NAMES carrying a space are never
    # English filler the way a bare code is, so a substring test is safe here where it
    # would be reckless there. Measured on the corpus: rescues 8 rows, drops 0.
    if not strong_us and any(
        f" {name} " in t for name in _STATE_NAMES if " " in name
    ):
        strong_us = True
    if strong_us:
        return "us"

    # ── an ISO COUNTRY CODE in the tail — the single highest-yield foreign rule ──
    # `bangalore, in` x39 · `Dresden, SN, de` · `Toronto, ON, CA` · `Changzhou, cn` ·
    # `Braga, pt` · `Campinas, SP, br`. Measured on 8,558 engine-labelled rows, 303 of
    # 342 foreign jobs that leaked through everything else (89%) end in `, <2-letter>`.
    # The code rule existed already — it just only ever knew about US STATES.
    #
    # CASE IS THE DISCRIMINATOR, the same way it is on the US side. Lowercase `in` is
    # India; uppercase `IN` is Indiana. So:
    #   lowercase  -> a country, unless it is `us` (61 US rows end in `, us`)
    #   uppercase  -> a country ONLY if it is not also a USPS code, which leaves `, CA`
    #                 ambiguous between California and Canada and therefore untouched.
    raw = str(location or "").strip()
    m = re.search(r",\s*([A-Za-z]{2})\s*$", raw)
    if m:
        code = m.group(1)
        up = code.upper()
        if up != "US" and up in _ISO_COUNTRY_CODES:
            if code.islower() or up not in US_STATES:
                return "foreign"

    # ── foreign evidence, also graded ────────────────────────────────────────
    toks = set(t.split())
    named_country = (
        any(code != "US" and f" {name} " in t for name, code in _country_names())
        or bool(toks & _MORE_COUNTRIES)
        or any(f" {ph} " in t for ph in _MORE_COUNTRY_PHRASES)
        or any(f" {rg} " in t for rg in _FOREIGN_REGIONS)
    )
    named_city = any(tok in _FOREIGN_CITIES for tok in t.split()) or any(
        f" {ph} " in t for ph in _FOREIGN_CITY_PHRASES
    )

    # A spelled-out foreign COUNTRY outranks a weak two-letter code: measured, 42 rows
    # pair the two and they read `Anywhere in France, Belgium, Spain` (x10) and `Berlin
    # Office; Munich Office; Remotely in Germany` (x5). A foreign CITY deliberately does
    # NOT outrank it — that is precisely `Dublin, CA` and `Berlin, CT`, which are American.
    # `weak_us` used to sit between these two, holding the case where the only US signal
    # was one of the seven codes that are also country codes. It is gone: a `City, ST`
    # tail is now STRONG evidence on its own, because the FORMATTING is the signal —
    # `Lebanon, IN` is Indiana whatever Lebanon is elsewhere. Grading it weak let the
    # country name win and dropped real US jobs.
    if named_country:
        return "foreign"
    if named_city:
        return "foreign"
    return None
