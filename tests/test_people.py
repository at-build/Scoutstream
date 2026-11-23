import argparse

import pytest
from conftest import page

from moltsets.people import prospect_company, resolve_seniority, resolve_titles

ARGS = argparse.Namespace(max_results=3, founders=2, no_email_fallback=True, phones=False)
NOT_FOUND = ("not_found", {"results": [], "total": 0})
ACME = {"name": "Acme Robotics", "domain": "acme.example"}


def acme():
    return {"name": "Acme Robotics", "domain": "acme.example", "countries": [], "hq_countries": [], "line": None}


def person(name, title, email=None, **extra):
    return {"full_name": name, "title": title, "country": "India", "city": "Pune",
            "linkedin_url": f"https://www.linkedin.com/in/{name.lower().replace(' ', '-')}",
            "business_email": email, "business_email_risk_score": "A" if email else None, "company": ACME, **extra}


def test_prospect_company_ranks_founders_first_and_sets_status(moltsets):
    head = person("Asha Rao", "Head of Procurement", "asha@acme.example", seniority="Head")
    ceo = person("Meera Iyer", "CEO", "meera@acme.example")

    def people(p):
        if p.get("title") == ["Founder", "CEO", "Chief Executive"]:
            return page([ceo], p)
        return page([head], p) if "seniority" in p else NOT_FOUND

    moltsets.on("search_people", people)
    titles, _ = resolve_titles("purchasing,supply chain")
    levels, _, _ = resolve_seniority("director+")
    company = acme()
    leads = prospect_company(company, titles, levels, ARGS)
    assert [lead["full_name"] for lead in leads] == ["Meera Iyer", "Asha Rao"]
    assert company["leads"] is leads
    assert company["status"] == "2 contacts found, 2 with email"


def test_prospect_company_failed_search_becomes_status(moltsets):
    moltsets.on("search_people", lambda p: {"error": {"code": "invalid_request", "message": "bad filter"}})
    company = acme()
    assert prospect_company(company, ["Procurement"], [], ARGS) == []
    assert company["status"] == "search failed: search_people: invalid_request (bad filter)"


def test_prospect_company_empty_result_explains_why(moltsets):
    moltsets.on("search_people", lambda p: NOT_FOUND)
    moltsets.on("search_companies", lambda p: NOT_FOUND)
    company = acme()
    prospect_company(company, ["Procurement"], [], ARGS)
    assert company["status"] == ("no contacts: acme.example isn't in MoltSets — check the website (another domain?) "
                                 "or use another provider")


def test_prospect_company_ctrl_c_propagates_with_leads_so_far(moltsets):
    def interrupt(p):
        raise KeyboardInterrupt

    moltsets.on("search_people", interrupt)
    company = acme()
    with pytest.raises(KeyboardInterrupt):
        prospect_company(company, ["Procurement"], [], ARGS)
    assert company["leads"] == []
