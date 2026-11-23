import argparse

from moltsets.modes.lookalikes import best_match, build_profile, similarity, tiers

MOTORS = {"name": "Example Motors", "domain": "examplemotors.example", "industry": "Automotive", "naics_codes": ["336991"],
         "naics_titles": ["Motorcycle, Bicycle, and Parts Manufacturing"],
         "linkedin_industry": ["Motor Vehicle Manufacturing"], "employee_range": "1001-5000",
         "revenue_range": "Above $50M", "country": "India"}
CYCLES = {"name": "Example Cycles", "domain": "examplecycles.example", "industry": "Automotive",
                 "naics_codes": ["336991"], "naics_titles": ["Motorcycle, Bicycle, and Parts Manufacturing"],
                 "linkedin_industry": ["Motor Vehicle Manufacturing"], "employee_range": "201-500",
                 "revenue_range": "$10M - $20M", "country": "India"}
WIDE_BANDS = ["51-200", "201-500", "501-1000", "1001-5000", "5001+"]
MOTORS_BANDS = ["501-1000", "1001-5000", "5001+"]


def profile_args(**overrides):
    values = {"naics": None, "linkedin_industry": None, "employee_range": None, "country": None, "any_country": False}
    return argparse.Namespace(**{**values, **overrides})


def test_build_profile_unions_seeds_and_widens_bands():
    profile, notes = build_profile([MOTORS, CYCLES], profile_args())
    assert profile["naics6"] == ["336991"]
    assert profile["naics4"] == ["3369"]
    assert profile["linkedin_industry"] == ["Motor Vehicle Manufacturing"]
    assert profile["employee_range"] == WIDE_BANDS
    assert profile["country"] == ["India"]
    assert profile["revenue_range"] == ["$10M - $20M", "Above $50M"]
    assert notes == []


def test_build_profile_band_at_the_edge():
    assert build_profile([{**MOTORS, "employee_range": "1-10"}], profile_args())[0]["employee_range"] == ["1-10", "11-20"]


def test_build_profile_overrides():
    profile, notes = build_profile([MOTORS], profile_args(
        naics="3364", employee_range="51-200,lots", country="usa",
        linkedin_industry="Aviation and Aerospace Component Manufacturing"))
    assert profile["naics6"] == ["3364"] and profile["naics4"] == ["3364"]
    assert profile["employee_range"] == ["51-200"]
    assert profile["country"] == ["United States"]
    assert profile["linkedin_industry"] == ["Aviation and Aerospace Component Manufacturing"]
    assert any("lots" in note for note in notes)
    assert build_profile([MOTORS], profile_args(any_country=True))[0]["country"] == []


def test_tiers_tight_then_wider():
    first, second = tiers(build_profile([MOTORS], profile_args())[0])
    assert first == {"employee_range": MOTORS_BANDS, "country": ["India"], "naics_code": ["336991"],
                     "linkedin_industry": ["Motor Vehicle Manufacturing"]}
    assert second == {"employee_range": MOTORS_BANDS, "country": ["India"], "naics_code": ["3369"]}


def test_tiers_fall_back_to_linkedin_then_industry():
    no_naics = {**MOTORS, "naics_codes": []}
    assert tiers(build_profile([no_naics], profile_args())[0]) == [
        {"employee_range": MOTORS_BANDS, "country": ["India"], "linkedin_industry": ["Motor Vehicle Manufacturing"]}]
    bare = {**no_naics, "linkedin_industry": []}
    assert tiers(build_profile([bare], profile_args())[0]) == [
        {"employee_range": MOTORS_BANDS, "country": ["India"], "industry": ["Automotive"]}]
    assert tiers(build_profile([{**bare, "industry": None}], profile_args())[0]) == []


def test_similarity_full_match_scores_100():
    points, reasons = similarity({**MOTORS, "domain": "exampleev.example"}, MOTORS)
    assert points == 100
    assert reasons == ["NAICS 336991 Motorcycle, Bicycle, and Parts Manufacturing",
                       "LinkedIn: Motor Vehicle Manufacturing", "1001-5000 employees", "revenue Above $50M", "India"]


def test_similarity_partial_and_adjacent_bands():
    candidate = {"naics_codes": ["336999"], "linkedin_industry": ["Renewable Energy Equipment Manufacturing"],
                 "employee_range": "201-500", "revenue_range": None, "country": "Germany"}
    assert similarity(candidate, CYCLES) == (45, ["NAICS 3369", "201-500 employees"])
    near = {**candidate, "employee_range": "501-1000", "revenue_range": "$20M - $50M"}
    assert similarity(near, CYCLES) == (43, ["NAICS 3369", "501-1000 employees (seed: 201-500)",
                                                    "revenue $20M - $50M (seed: $10M - $20M)"])


def test_best_match_picks_the_closest_seed():
    candidate = {"naics_codes": ["336991"], "employee_range": "201-500", "country": "India"}
    points, seed, _ = best_match(candidate, [MOTORS, CYCLES])
    assert (points, seed) == (65, "examplecycles.example")


import csv
import os

import pytest
from conftest import page, run_cli, write

from moltsets.modes.lookalikes import load_seeds

EV_CO = {**MOTORS, "name": "Example EV", "domain": "exampleev.example", "_score": 3.0}
BIKES = {**CYCLES, "name": "Example Bikes", "domain": "examplebikes.example", "_score": 2.0}
ENERGY_CO = {"name": "Example Energy", "domain": "exampleenergy.example", "naics_codes": ["336999"],
            "naics_titles": ["All Other Transportation Equipment Manufacturing"],
            "linkedin_industry": ["Renewable Energy Equipment Manufacturing"], "employee_range": "201-500",
            "country": "India", "_score": 1.0}


def fake_companies(moltsets):
    seeds = {"examplemotors.example": MOTORS, "examplecycles.example": CYCLES}

    def companies(p):
        if "domain" in p:
            return page([seeds[p["domain"]]], p) if p["domain"] in seeds else ("not_found", {"results": [], "total": 0})
        pool = [MOTORS, EV_CO, BIKES] if "linkedin_industry" in p else [ENERGY_CO]
        return page([c for c in pool if c["domain"] not in p.get("exclude_domain", [])], p)

    moltsets.on("search_companies", companies)


def test_load_seeds_dedupes_and_reports(moltsets):
    write("seeds.csv", "company,website,contact_full_name\nExample Motors,https://www.examplemotors.example,A\nExample Motors,examplemotors.example,B\n"
                       "Example Cycles,examplecycles.example,\nBad,not a domain,\n")
    domains, issues, blocking = load_seeds(argparse.Namespace(seed="examplemotors.example,  otherev.example"), "seeds.csv")
    assert domains == ["examplemotors.example", "examplecycles.example", "otherev.example"]
    assert issues == ["line 5: 'not a domain' isn't a valid domain — skipped."]
    assert not blocking
    assert load_seeds(argparse.Namespace(seed=None), None)[2] is True


def test_run_lookalikes_end_to_end(moltsets, monkeypatch):
    fake_companies(moltsets)
    write("known.csv", "website\nexamplebikes.example\n")
    run_cli(monkeypatch, "lookalikes", "--seed", "examplemotors.example,examplecycles.example", "--exclude-file", "known.csv")
    with open("lookalikes_examplemotors.csv") as f:
        rows = list(csv.DictReader(f))
    assert [(r["domain"], r["similarity_score"], r["most_similar_seed"], r["tier"]) for r in rows] == [
        ("exampleev.example", "100", "examplemotors.example", "1"), ("exampleenergy.example", "55", "examplecycles.example", "2")]
    assert list(rows[0])[-4:] == ["similarity_score", "most_similar_seed", "match_reasons", "tier"]
    assert rows[0]["location"] == "India"
    searches = [p for p in moltsets.calls_to("search_companies") if "domain" not in p]
    for p in searches:
        assert {"examplemotors.example", "examplecycles.example", "examplebikes.example"} <= set(p["exclude_domain"])
    assert "exampleev.example" in searches[-1]["exclude_domain"]  # tier 2 excludes what tier 1 found


def test_run_lookalikes_declined_after_test_search(moltsets, monkeypatch):
    fake_companies(moltsets)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    run_cli(monkeypatch, "lookalikes", "--seed", "examplemotors.example")
    assert not os.path.exists("lookalikes_examplemotors.csv")
    assert len([p for p in moltsets.calls_to("search_companies") if "domain" not in p]) == 1


def test_lookalikes_needs_seeds(moltsets, monkeypatch):
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "lookalikes")


def test_no_seed_in_moltsets(moltsets, monkeypatch):
    fake_companies(moltsets)
    run_cli(monkeypatch, "lookalikes", "--seed", "nowhere.example", "--output", "out.csv")
    assert not os.path.exists("out.csv")
    assert not [p for p in moltsets.calls_to("search_companies") if "domain" not in p]


def test_allowance_runs_out_mid_tier_keeps_found(moltsets, monkeypatch):
    def companies(p):
        if "domain" in p:
            return page([MOTORS], p)
        if "linkedin_industry" in p:
            return page([c for c in [EV_CO] if c["domain"] not in p.get("exclude_domain", [])], p)
        return {"error": {"code": "fair_use_limit_exceeded", "message": "used up"}}

    moltsets.on("search_companies", companies)
    run_cli(monkeypatch, "lookalikes", "--seed", "examplemotors.example", "--yes")
    with open("lookalikes_examplemotors.csv") as f:
        rows = list(csv.DictReader(f))
    assert [r["domain"] for r in rows] == ["exampleev.example"]


def test_yes_skips_prompt(moltsets, monkeypatch):
    fake_companies(moltsets)

    def no_prompt(prompt=""):
        raise AssertionError("prompted despite --yes")

    monkeypatch.setattr("builtins.input", no_prompt)
    run_cli(monkeypatch, "lookalikes", "--seed", "examplemotors.example", "--yes")
    assert os.path.exists("lookalikes_examplemotors.csv")


def test_exclusions_over_the_api_cap_are_filtered_locally(moltsets, monkeypatch):
    """MoltSets takes at most 100 exclude_domain values. The rest are filtered here, and a tier is fetched
    again when a locally excluded company used up the slot."""
    known = [f"k{i:03d}.example" for i in range(120)] + ["examplebikes.example"]
    write("known.csv", "website\n" + "\n".join(known) + "\n")

    def companies(p):
        if "domain" in p:
            return page([MOTORS], p) if p["domain"] == "examplemotors.example" else ("not_found", {"results": [], "total": 0})
        if len(p.get("exclude_domain", [])) > 100:
            return {"error": {"code": "invalid_input", "message": "Too many exclusion values for: exclude_domain"}}
        pool = [BIKES, EV_CO] if "linkedin_industry" in p else [ENERGY_CO]
        return page([c for c in pool if c["domain"] not in p["exclude_domain"]], p)

    moltsets.on("search_companies", companies)
    run_cli(monkeypatch, "lookalikes", "--seed", "examplemotors.example", "--exclude-file", "known.csv", "--max-results", "1",
            "--yes")
    with open("lookalikes_examplemotors.csv") as f:
        rows = list(csv.DictReader(f))
    assert [r["domain"] for r in rows] == ["exampleev.example"]  # Example Bikes was on the list: filtered here, slot refilled
    searches = [p for p in moltsets.calls_to("search_companies") if "domain" not in p]
    assert all(len(p["exclude_domain"]) <= 100 and "examplemotors.example" in p["exclude_domain"] for p in searches)
    assert "examplebikes.example" in searches[-1]["exclude_domain"]  # once returned, it's sent to the API, not re-fetched
