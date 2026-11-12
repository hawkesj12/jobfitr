"""The checks that stand between the model's picks and the person.

Every case here is one the four tester runs on 2026-11-11 actually produced, or the
false-positive that the obvious fix for it would cause. Pure functions of (row, brief):
no model, no network, no store.
"""

from __future__ import annotations

from datetime import date, timedelta

from jobfitr import checks


def _row(**kw):
    row = {"url": "https://boards.example/1", "title": "Forward Deployed Engineer",
           "company": "Acme", "location": "Remote", "remote": "remote", "body": "",
           "salary": "", "posted": (date.today() - timedelta(days=3)).isoformat()}
    row.update(kw)
    return row


def _brief(**kw):
    return checks.clean_brief(kw)


# ── the brief ────────────────────────────────────────────────────────────────
def test_a_floor_said_in_thousands_is_read_as_thousands():
    # "I'd like the floor to be 200." Read literally, every job on earth clears it.
    assert _brief(salary_floor=200)["salary_floor"] == 200_000
    assert _brief(salary_floor=65000)["salary_floor"] == 65_000


def test_a_brief_the_checks_cannot_read_degrades_to_no_rules():
    b = checks.clean_brief({"salary_floor": "lots", "exclude": "Accenture", "must_mention": [7]})
    assert b["salary_floor"] is None and b["exclude"] == [] and b["must_mention"] == []
    assert checks.clean_brief(None)["remote_only"] is False


# ── place ────────────────────────────────────────────────────────────────────
def test_a_spelled_out_state_is_not_another_state():
    # `parts[1].upper()[:2]` read Minnesota as MI, which is Michigan.
    assert checks.place_filter("Minneapolis, Minnesota") == ("minneapolis", "MN")
    assert checks.place_filter("Louisville, KY") == ("louisville", "KY")
    assert checks.place_filter("remote") is None


def test_the_wrong_town_warns_and_never_removes():
    # No distances in the store: New Albany, IN is five miles from Louisville.
    row = _row(location="New Albany, IN", state="IN", city="New Albany", remote="onsite")
    v = checks.check_pick(row, _brief(location="Louisville, KY"))
    assert v["reject"] == ""
    assert any("Listed in New Albany, IN; you asked for Louisville, KY" in f for f in v["flags"])


# ── remote ───────────────────────────────────────────────────────────────────
LAVENDO = ("About the role. This is an on-site role, 5 days a week in our San Francisco "
           "office. You will build the core product.")


def test_remote_only_removes_a_posting_whose_text_says_on_site():
    # Tagged "San Francisco (Remote)"; the body required the office five days a week.
    v = checks.check_pick(_row(body=LAVENDO), _brief(remote_only=True))
    assert "remote only" in v["reject"] and "5 days a week" in v["reject"]
    assert v["tag"] == "", "a Remote chip must not sit above text that says on-site"


def test_the_same_posting_only_warns_when_remote_was_not_required():
    v = checks.check_pick(_row(body=LAVENDO), _brief())
    assert v["reject"] == "" and any("On-site per the posting" in f for f in v["flags"])


def test_a_customer_visit_is_not_an_office_job():
    body = "Fully remote. Expect occasional on-site customer visits, 2 days a quarter."
    assert checks.onsite_language(body) == ""
    assert checks.check_pick(_row(body=body), _brief(remote_only=True))["reject"] == ""


def test_a_client_office_is_still_an_office():
    body = "You will be on-site 5 days a week at our client office."
    assert "remote only" in checks.check_pick(_row(body=body), _brief(remote_only=True))["reject"]


def test_a_posting_silent_on_remote_is_a_warning_not_a_removal():
    v = checks.check_pick(_row(remote="", body="You will build things."), _brief(remote_only=True))
    assert v["reject"] == ""
    assert "does not say whether it is remote" in " ".join(v["flags"])


# ── pay ──────────────────────────────────────────────────────────────────────
def test_a_range_that_tops_out_under_the_floor_is_removed():
    # A $200K floor got picks topping out at $135K and $145K.
    row = _row(salary="$110,000 - $135,000", salary_min=110000, salary_max=135000)
    v = checks.check_pick(row, _brief(salary_floor=200000))
    assert "up to $135,000" in v["reject"] and "$200,000" in v["reject"]


def test_a_range_that_straddles_the_floor_warns():
    row = _row(salary="$180,000 - $240,000", salary_min=180000, salary_max=240000)
    v = checks.check_pick(row, _brief(salary_floor=200000))
    assert v["reject"] == "" and any("starts at $180,000" in f for f in v["flags"])


def test_no_salary_against_a_floor_is_said_on_the_card():
    # 5 of 10 picks in one tester's runs had no salary and the cards were silent.
    v = checks.check_pick(_row(), _brief(salary_floor=55000))
    assert v["reject"] == "" and "The listing's pay field is empty — your floor is $55,000. " \
        "Check the posting for a figure." in v["flags"]
    assert checks.check_pick(_row(), _brief())["flags"] == []


def test_a_salary_that_is_only_in_the_display_string_still_counts():
    # 9 of 12 picks that printed a salary had no structured figure, so their cards read
    # "$199,000 - $235,000" with "No salary listed" underneath.
    row = _row(salary="$199,000 - $235,000")
    assert checks.pay_range(row) == (199000.0, 235000.0)
    assert checks.check_pick(row, _brief(salary_floor=180000))["flags"] == []
    assert "up to $130,000" in checks.check_pick(_row(salary="$110,000-130,000"),
                                                 _brief(salary_floor=200000))["reject"]
    assert checks.pay_range(_row(salary="$213,000 to $246,000")) == (213000.0, 246000.0)
    # Not plainly annual dollars: left as "not stated" rather than guessed.
    for s in ("$33–$41", "£60,000 - £70,000", "$6,000 per month", "CAD 120,000", "$900 a week"):
        assert checks.pay_range(_row(salary=s)) == (None, None), s


def test_hourly_pay_is_read_as_a_year_of_hours():
    assert checks.pay_range(_row(salary="$22 an hour")) == (45760.0, 45760.0)
    assert checks.pay_range(_row(salary="$18/hr")) == (37440.0, 37440.0)
    assert checks.pay_range(_row(salary="$18.50 per hour")) == (38480.0, 38480.0)
    v = checks.check_pick(_row(salary="$18 - $20 an hour"), _brief(salary_floor=55000))
    assert v["reject"] == "It pays up to $20 an hour, about $41,600 a year, under your $55,000 floor."
    assert checks.check_pick(_row(salary="$30 an hour"), _brief(salary_floor=55000))["reject"] == ""


def test_an_adzuna_estimate_is_not_the_employers_pay():
    # The engine writes Adzuna's prediction as one figure twice and leaves salary_min empty.
    est = _row(salary="$61,630–$61,630", source="adzuna",
               salary_estimated_min=61630, salary_estimated_max=61630)
    old = _row(salary="$61,630 - $61,630", source="adzuna")  # no estimate columns
    for row in (est, old):
        assert checks.pay_range(row) == (None, None)
        assert checks.pay_label(row) == "about $61,630 (Adzuna's estimate)"
        v = checks.check_pick(row, _brief(salary_floor=100000))
        assert v["reject"] == "" and any("Adzuna's estimate; the employer stated none" in f for f in v["flags"])
    # A stated figure on an Adzuna row is the employer's and still counts.
    stated = _row(salary="$60,000 - $70,000", source="adzuna", salary_min=60000, salary_max=70000)
    assert checks.pay_range(stated) == (60000.0, 70000.0)


def test_a_range_of_one_number_prints_as_a_number():
    assert checks.pay_label(_row(salary="$61,630–$61,630")) == "$61,630"
    assert checks.pay_label(_row(salary="$61,630 - $61,630 a year")) == "$61,630 a year"
    assert checks.pay_label(_row(salary="$60,000 - $70,000")) == "$60,000 - $70,000"


# ── employers ruled out ──────────────────────────────────────────────────────
def test_an_excluded_firm_in_the_employer_name_is_removed():
    v = checks.check_pick(_row(company="Accenture Federal Services"), _brief(exclude=["Accenture"]))
    assert "Accenture" in v["reject"]


def test_a_subsidiary_that_names_its_parent_is_removed():
    # "No big consulting" returned NeuraFlash, whose posting says it is part of Accenture.
    row = _row(company="NeuraFlash", body="NeuraFlash, Part of Accenture, is a leader in AI. You will build.")
    v = checks.check_pick(row, _brief(exclude=["Accenture"]))
    assert "Part of Accenture" in v["reject"]


def test_the_other_ways_a_posting_names_its_parent():
    b = _brief(exclude=["Accenture", "Deloitte"])
    for body in ("Our parent company, Deloitte, backs us.", "We are an Accenture subsidiary."):
        assert checks.check_pick(_row(body=body), b)["reject"], body
    assert checks.check_pick(_row(body="Our founders were part of Accenture teams."), b)["reject"] == ""


def test_a_passing_mention_of_an_excluded_name_says_nothing_about_the_employer():
    # "No crypto" became a list including Block; a posting naming where its engineers had
    # worked ("Meta, Palantir, Block, Affirm") got a warning the model then apologised for.
    row = _row(body="Our team brings experience from Meta, Palantir, Block, Affirm and Google.")
    v = checks.check_pick(row, _brief(exclude=["Block", "Accenture"]))
    assert v["reject"] == "" and v["flags"] == []


def test_exclusion_is_whole_word():
    # The substring form hid "Software Engineer, Internal Systems" under "intern".
    v = checks.check_pick(_row(company="Bainbridge Robotics"), _brief(exclude=["Bain"]))
    assert v["reject"] == ""


def test_an_agency_is_removed_only_when_agencies_were_ruled_out():
    body = "Our client is seeking a senior engineer. This role is on behalf of our client."
    assert "agencies" in checks.check_pick(_row(body=body), _brief(no_agencies=True))["reject"]
    assert checks.check_pick(_row(body=body), _brief())["reject"] == ""


def test_a_staffing_firm_is_known_by_its_name_and_the_agency_wording_is_fixed():
    b = _brief(no_agencies=True)
    for name in ("Insight Global", "TEKsystems, Inc.", "robert half"):
        assert "staffing firm" in checks.check_pick(_row(company=name), b)["reject"], name
    assert checks.check_pick(_row(company="Global Insight Partners"), b)["reject"] == ""
    assert checks.check_pick(_row(body="Our client, a leading manufacturer, needs a planner."), b)["reject"]
    assert checks.check_pick(_row(body="Our client's success is our success."), b)["reject"] == ""


# ── what the work should involve, and seniority ──────────────────────────────
def test_a_missing_industry_is_a_warning_on_the_card():
    # Manufacturing or distribution was ignored every time and nothing said so.
    b = _brief(must_mention=[{"label": "manufacturing or distribution",
                              "any_of": ["manufacturing", "distribution", "warehouse"]}])
    v = checks.check_pick(_row(body="An edtech finance platform for schools."), b)
    assert v["reject"] == "" and "never mentions manufacturing or distribution" in " ".join(v["flags"])
    ok = checks.check_pick(_row(body="We sell to manufacturing plants."), b)
    assert not any("never mentions" in f for f in ok["flags"])


def test_a_big_experience_ask_is_flagged_for_someone_changing_fields():
    # An Oracle role wanting 8+ years went to a production manager as "aligns well".
    body = "Requirements: 8+ years of experience as an Oracle Fusion consultant."
    assert "8+ years" in checks.check_pick(_row(body=body), _brief(experience_years=0))["asks"]
    assert checks.check_pick(_row(body=body), _brief(experience_years=12))["asks"] == ""
    # A company's age is not a requirement.
    assert checks.years_asked("For over 20 years we have built conveyors.")[0] == 0


def test_an_old_posting_is_flagged():
    old = (date.today() - timedelta(days=120)).isoformat()
    assert any("120 days ago" in f for f in checks.check_pick(_row(posted=old), _brief())["flags"])


# ── the quote, and the words that say nothing ────────────────────────────────
def test_a_quote_must_be_in_the_posting():
    text = "You will embed with customers, learn how their systems work, and build integrations."
    assert checks.quote_in("embed with customers, learn how their systems work", text)
    assert checks.quote_in("Embed with customers,  learn how their SYSTEMS work", text)
    assert not checks.quote_in("embed with clients and learn their systems", text)
    assert not checks.quote_in("customers", text), "too short to be evidence of anything"


def test_the_praise_words_from_the_tester_runs_are_recognised():
    for said in ("aligns well with your B2B SaaS experience", "aligns directly with your goals",
                 "a great fit for manufacturing environments"):
        assert checks.FLATTERY.search(said)
    assert not checks.FLATTERY.search("You would configure the ERP and train the plant's staff.")


# ── what the card prints ─────────────────────────────────────────────────────
def test_a_board_slug_is_printed_as_a_name():
    assert checks.company_name({"company": "bjakcareer"}) == "Bjak"
    assert checks.company_name({"company": "unanet"}) == "Unanet"
    assert checks.company_name({"company": "Kuehne+Nagel"}) == "Kuehne+Nagel"
    assert checks.company_name({"company": "eBay"}) == "eBay"


def test_a_link_through_a_middleman_is_labelled():
    assert checks.link_kind({"url": "https://www.adzuna.com/land/ad/123"}) == "adzuna"
    assert checks.link_kind({"url": "https://job-boards.greenhouse.io/acme/jobs/1"}) == "employer"


def test_one_job_posted_twice_is_one_job():
    a = _row(url="u1", company="KEEN", title="Warehouse Lead")
    assert checks.same_job(a, _row(url="u2", company="Keen", title="Warehouse Lead"))
    assert not checks.same_job(a, _row(url="u3", company="Keen", title="Warehouse Manager"))


def test_the_brief_reads_back_in_plain_words():
    b = _brief(remote_only=True, location="Louisville, KY", salary_floor=200, exclude=["Accenture"],
               no_agencies=True)
    assert checks.brief_lines(b) == ["remote only", "based in Louisville, KY", "$200,000 or more",
                                     "not Accenture", "no agencies"]
