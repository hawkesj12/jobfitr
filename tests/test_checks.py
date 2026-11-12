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
    assert any("New Albany, IN, not Louisville, KY" in f for f in v["flags"])


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
    assert v["reject"] == "" and any("No salary listed" in f for f in v["flags"])
    assert checks.check_pick(_row(), _brief())["flags"] == []


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


def test_working_beside_an_excluded_firm_only_warns():
    row = _row(body="You will work alongside Accenture and Deloitte teams at the customer.")
    v = checks.check_pick(row, _brief(exclude=["Accenture"]))
    assert v["reject"] == "" and any("Accenture" in f for f in v["flags"])


def test_exclusion_is_whole_word():
    # The substring form hid "Software Engineer, Internal Systems" under "intern".
    v = checks.check_pick(_row(company="Bainbridge Robotics"), _brief(exclude=["Bain"]))
    assert v["reject"] == ""


def test_an_agency_is_removed_only_when_agencies_were_ruled_out():
    body = "Our client is seeking a senior engineer. This role is on behalf of our client."
    assert "agencies" in checks.check_pick(_row(body=body), _brief(no_agencies=True))["reject"]
    assert checks.check_pick(_row(body=body), _brief())["reject"] == ""


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
