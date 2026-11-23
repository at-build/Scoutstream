from conftest import page

from moltsets.companies import company_row, lookup_company

SPACE_CO = {"name": "Example Space", "domain": "examplespace.example", "industry": "Government and Public Administration",
          "linkedin_industry": ["Space Research and Technology"],
          "naics_titles": ["Space Research and Technology", "Satellite Telecommunications"],
          "employee_range": "201-500", "employee_count": "320", "revenue_range": "$5M - $10M", "city": "Bengaluru",
          "state": "Karnataka", "country": "India", "linkedin_url": "https://www.linkedin.com/company/example_space",
          "_score": 7.5}


def test_company_row_is_the_accounts_format():
    assert company_row(SPACE_CO, "India") == {
        "company": "Example Space", "website": "https://examplespace.example", "location": "India", "domain": "examplespace.example",
        "industry": "Government and Public Administration", "linkedin_industry": "Space Research and Technology",
        "naics": "Space Research and Technology; Satellite Telecommunications", "employee_range": "201-500",
        "employee_count": "320", "revenue_range": "$5M - $10M", "city": "Bengaluru", "state": "Karnataka",
        "country": "India", "linkedin_url": "https://www.linkedin.com/company/example_space", "moltsets_score": 7.5,
    }
    assert company_row({}, "")["website"] == ""


def test_lookup_company_found_and_missing(moltsets):
    moltsets.on("search_companies", lambda p: page([SPACE_CO], p) if p["domain"] == "examplespace.example"
                else ("not_found", {"results": [], "total": 0}))
    assert lookup_company("examplespace.example")["name"] == "Example Space"
    assert lookup_company("nowhere.example") is None
    assert moltsets.calls_to("search_companies") == [{"domain": "examplespace.example", "limit": 1},
                                                      {"domain": "nowhere.example", "limit": 1}]
