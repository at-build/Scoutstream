"""Golden-output tests: each existing mode's console output and CSVs, from fixed fake MoltSets replies.

The goldens were captured from the single-file script before the package split (UPDATE_GOLDEN=1).
After the split, the same runs must reproduce them exactly.
"""

from conftest import check_golden, page, run_cli, snapshot, write

NOT_FOUND = ("not_found", {"results": [], "total": 0})
FOUNDER_TITLES = ["Founder", "CEO", "Chief Executive"]
ACME = {"name": "Acme Robotics", "domain": "acme.example"}

ASHA = {"full_name": "Asha Rao", "title": "Head of Procurement", "seniority": "Head", "city": "Bengaluru",
        "country": "India", "linkedin_url": "https://www.linkedin.com/in/example_asha_rao", "business_email": "asha@acme.example",
        "business_email_risk_score": "A", "business_email_validated_at": "2026-10-01", "company": ACME, "_score": 5.0}
RAVI = {"full_name": "Ravi Kumar", "title": "Sourcing Coordinator", "seniority": "C Suite", "city": "Pune",
        "country": "India", "linkedin_url": "https://www.linkedin.com/in/example_ravi_kumar", "company": ACME, "_score": 4.0}
ASHA_DUPLICATE = {**ASHA, "business_email": None, "linkedin_url": "https://www.linkedin.com/in/example_asha_rao_2"}
MEERA = {"full_name": "Meera Iyer", "title": "Co-Founder & CEO", "seniority": "Founder", "country": "United States",
         "linkedin_url": "https://www.linkedin.com/in/example_meera_iyer", "business_email": "meera@gmail.example",
         "business_email_risk_score": "A", "company": ACME, "_score": 3.0}
WRONG_COMPANY = {"full_name": "Acme Robotics Turkey", "title": "Founder", "country": "Turkey",
                 "linkedin_url": "https://www.linkedin.com/in/example_acme_tr",
                 "company": {"name": "Acme TR", "domain": "acme-tr.example"}, "_score": 1.0}


def test_prospect_companies_file(moltsets, capsys, monkeypatch):
    write("targets.csv", "company,website,location\nAcme Robotics,acme.example,India\n"
                         "Example Beta,https://www.examplebeta.example,\nAcme Again,acme.example,\nGamma,not a site,\n")

    def people(p):
        if p["company_domain"] != "acme.example":
            return NOT_FOUND
        if p.get("title") == FOUNDER_TITLES:
            return page([ASHA_DUPLICATE, MEERA, WRONG_COMPANY], p)
        if "seniority" in p:
            return page([ASHA, RAVI], p)
        return NOT_FOUND

    moltsets.on("search_people", people)
    moltsets.on("linkedin_to_best_email", lambda p: ("ok", {"email": "ravi@acme.example", "risk_score": "B",
                                                            "last_validated_at": "2026-09-01", "type": "business"}))
    moltsets.on("search_companies", lambda p: page([{"name": "Example Beta", "domain": "examplebeta.example",
                                                     "employee_range": "11-20", "country": "United States"}], p))
    run_cli(monkeypatch, "prospect", "--companies-file", "targets.csv", "--max-results", "2", "--founders", "3",
            "--ignore-location-column")
    check_golden("prospect", snapshot(capsys, "targets_with_contacts.csv"))


def test_verify_two_providers(moltsets, capsys, monkeypatch):
    write("lusha_examplemotors_test_leads.csv",
          "name,company,job_title,email,linkedin_url\n"
          "Asha Rao,Example Motors,Head of Procurement,asha@examplemotors.example,\n"
          "Vikram Shah,Example Motors,Purchase Manager,vikram@examplemotors.example,\n"
          "Neha Iyer,Example Motors,Supply Chain Lead,,https://www.linkedin.com/in/example_neha_iyer\n"
          "Old Row,Example Motors,Buyer,,\n")
    write("crustdata_exampleelectric_test.csv", "name,linkedin_url,title\nRavi K,https://linkedin.com/in/example_ravi_k,Purchase Head\n")
    emails = {
        "asha@examplemotors.example": {"first_name": "Asha", "last_name": "Rao", "title": "Head of Procurement",
                                 "current_company": "Example Motors", "current_company_url": "https://www.examplemotors.example",
                                 "work_email_confirmed": "asha@examplemotors.example", "work_email_confirmed_risk_score": "A",
                                 "work_email_confirmed_status": "2026-09-20",
                                 "linkedinurl": "https://www.linkedin.com/in/example_asha_rao"},
        "vikram@examplemotors.example": {"first_name": "Vikram", "last_name": "Shah", "title": "Category Manager",
                                   "current_company": "Example Foods", "current_company_url": "https://www.examplefoods.example",
                                   "work_email_confirmed": "vikram@examplefoods.example", "work_email_confirmed_risk_score": "A"},
    }
    profiles = {
        "https://www.linkedin.com/in/example_neha_iyer": {
            "full_name": "Neha Iyer", "title": "Head of Supply Chain",
            "company": {"name": "Example Motors", "website_url": "https://examplemotors.example"},
            "business_email": "neha@examplemotors.example", "business_email_risk_score": "A",
            "current_role_start_date": "2025-04-01"},
        "https://www.linkedin.com/in/example_ravi_k": {
            "full_name": "Ravi K", "title": "Head of Purchase",
            "company": {"name": "Example Electric India Ltd", "website_url": "https://exampleelectric.example"},
            "business_email": "ravi.k@exampleelectric.example", "business_email_risk_score": "B"},
    }
    moltsets.on("reverse_email_lookup", lambda p: ("ok", emails[p["email"]]) if p["email"] in emails
                else ("not_found", {}))
    moltsets.on("reverse_linkedin_lookup", lambda p: ("ok", profiles[p["linkedin_url"]])
                if p["linkedin_url"] in profiles else ("not_found", {}))
    run_cli(monkeypatch, "verify", "lusha_examplemotors_test_leads.csv", "crustdata_exampleelectric_test.csv")
    check_golden("verify", snapshot(capsys, "lusha_examplemotors_test_leads_moltsets_verified.csv",
                                    "crustdata_exampleelectric_test_moltsets_verified.csv", "moltsets_provider_comparison.csv"))


def test_enrich_prospects_file(moltsets, capsys, monkeypatch):
    write("prospects.csv", "name,company_domain,linkedin_url,email\nJane Doe,acme.example,,\n"
                           ",,https://www.linkedin.com/in/example_john_roe,\n,,,inbound@examplebeta.example\nNobody Here,zzz.example,,\n")
    profiles = {
        "https://www.linkedin.com/in/example_jane_doe": {
            "full_name": "Jane Doe", "title": "VP Supply Chain", "seniority": "VP", "country": "India", "city": "Pune",
            "company": {"name": "Acme", "website_url": "https://acme.example", "industry": "Manufacturing",
                        "employee_range": "51-200"},
            "business_email": "jane@acme.example", "business_email_risk_score": "A",
            "personal_email": "jane.doe@gmail.example", "personal_email_risk_score": "B"},
        "https://www.linkedin.com/in/example_john_roe": {
            "full_name": "John Roe", "title": "Procurement Manager", "country": "India",
            "company": {"name": "NewCo", "website_url": "https://newco.example"}},
    }
    moltsets.on("search_linkedin_profile", lambda p: ("ok", {"count": 2}) if p["name"] == "Jane Doe"
                else ("not_found", {"count": 0}))
    moltsets.on("search_business_email_by_name", lambda p: ("ok", {
        "email": "jane@acme.example", "risk_score": "A", "last_validated_at": "2026-10-01",
        "linkedin_url": "https://www.linkedin.com/in/example_jane_doe"}))
    moltsets.on("reverse_linkedin_lookup", lambda p: ("ok", profiles[p["linkedin_url"]]))
    moltsets.on("linkedin_to_best_email", lambda p: ("ok", {"email": "john@oldco.example", "risk_score": "A",
                                                            "last_validated_at": "2026-08-01", "type": "business"}))
    moltsets.on("reverse_email_lookup", lambda p: ("not_found", {}))
    run_cli(monkeypatch, "enrich", "--prospects-file", "prospects.csv")
    check_golden("enrich", snapshot(capsys, "prospects_moltsets_contacts.csv"))


def test_accounts_filters(moltsets, capsys, monkeypatch):
    space_co = {"name": "Example Space", "domain": "examplespace.example", "industry": "Government and Public Administration",
              "linkedin_industry": ["Space Research and Technology"], "naics_titles": ["Satellite Telecommunications"],
              "employee_range": "201-500", "employee_count": "320", "revenue_range": "$5M - $10M",
              "city": "Bengaluru", "state": "Karnataka", "country": "India",
              "linkedin_url": "https://www.linkedin.com/company/example_space", "_score": 7.5}
    machines_co = {"name": "Example Machines", "domain": "examplemachines.example", "industry": "Manufacturing",
                "employee_range": "51-200", "city": "Bengaluru", "country": "India", "_score": 6.0}
    moltsets.on("search_companies", lambda p: page([space_co, machines_co], p))
    run_cli(monkeypatch, "accounts", "--industry", "Manufacturing", "--employee-range", "51-200,201-500",
            "--country", "India", "--exclude-domain", "known.example", "--max-results", "2")
    check_golden("accounts", snapshot(capsys, "moltsets_accounts.csv"))
    assert moltsets.calls_to("search_companies")[0]["exclude_domain"] == ["known.example"]
