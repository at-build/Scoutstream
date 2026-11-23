import csv
import json
import os
from collections import Counter
from datetime import date, datetime, timezone

import pytest
from conftest import account, page, run_cli, write

from moltsets import api
from moltsets.modes import visitors as v

T0 = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)


def test_visitor_ip():
    assert v.visitor_ip("1.1.1.1") == ("1.1.1.1", None)
    assert v.visitor_ip("1.1.1.1, 172.16.0.1") == ("1.1.1.1", None)  # X-Forwarded-For
    assert v.visitor_ip("::ffff:1.1.1.1") == ("1.1.1.1", None)
    assert v.visitor_ip("10.0.0.5") == (None, "skipped: private or reserved address")
    assert v.visitor_ip("2001:db8::1") == (None, "skipped: IPv6 (MoltSets resolves IPv4 only)")
    assert v.visitor_ip("not-an-ip") == (None, "skipped: not an IP address")


def test_parse_time_formats():
    ts = T0.timestamp()
    for value in ("2026-10-15T09:00:00Z", "2026-10-15 09:00:00+00:00", "15/Oct/2026:14:30:00 +0530",
                  str(int(ts)), str(int(ts * 1000)), str(int(ts * 1_000_000_000))):
        assert abs((v.parse_time(value) - T0).total_seconds()) < 1, value
    assert v.parse_time("2026-10-15T09:00:00").tzinfo == timezone.utc
    assert v.parse_time("yesterday") is None
    assert v.parse_time("") is None


def test_page_path():
    assert v.page_path("https://example.com/pricing?utm=x") == "/pricing"
    assert v.page_path("/demo#form") == "/demo"
    assert v.page_path("https://example.com") == "/"
    assert v.page_path("") == ""
    assert v.page_path("https://[bad") == "/"  # urlparse raises ValueError on this
    assert v.page_path("https://[bad/demo?x=1#y") == "/demo"


def test_parse_log_keeps_a_row_with_a_malformed_url(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write("junk.csv", "ip,url\n1.1.1.1,https://[bad\n1.1.1.1,/pricing\n")
    visits, dropped, issues, blocking = v.parse_log("junk.csv")
    assert (dropped, issues, blocking) == ({}, [], False)
    assert visits["1.1.1.1"]["views"] == 2
    assert dict(visits["1.1.1.1"]["pages"]) == {"/": 1, "/pricing": 1}


def test_parse_log_cloudflare_headers_and_filters(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ns = int(T0.timestamp() * 1_000_000_000)
    write("cf.csv", "ClientIP,ClientRequestPath,ClientRequestUserAgent,EdgeStartTimestamp\n"
                    f"1.1.1.1,/pricing,Mozilla/5.0,{ns}\n"
                    f"1.1.1.1,/static/app.js,Mozilla/5.0,{ns + 60_000_000_000}\n"
                    f"10.0.0.5,/,Mozilla/5.0,{ns}\n"
                    f"2001:db8::1,/,Mozilla/5.0,{ns}\n"
                    f"66.249.66.1,/,Googlebot/2.1,{ns}\n"
                    f"not-an-ip,/,Mozilla/5.0,{ns}\n"
                    "8.8.8.8,/careers,Mozilla/5.0,soon\n")
    visits, dropped, issues, blocking = v.parse_log("cf.csv")
    assert (issues, blocking) == ([], False)
    assert set(visits) == {"1.1.1.1", "8.8.8.8"}
    acme = visits["1.1.1.1"]
    assert acme["views"] == 1 and dict(acme["pages"]) == {"/pricing": 1}  # the .js request isn't a page view
    assert round((acme["last"] - acme["first"]).total_seconds()) == 60
    assert dropped == {"skipped: private or reserved address": 1, "skipped: IPv6 (MoltSets resolves IPv4 only)": 1,
                       "skipped: bot user agent": 1, "skipped: not an IP address": 1,
                       "kept: timestamp not understood": 1}


def test_parse_log_x_forwarded_for_column(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write("xff.csv", 'X-Forwarded-For,url\n"1.1.1.1, 172.16.0.1",/pricing\n')
    assert set(v.parse_log("xff.csv")[0]) == {"1.1.1.1"}


def test_parse_log_semicolon_export_blocks_with_hint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write("eu.csv", "ip;timestamp;url\n1.1.1.1;2026-10-15T09:00:00Z;/\n")
    visits, _, issues, blocking = v.parse_log("eu.csv")
    assert blocking and visits == {}
    assert "found ['ip;timestamp;url']" in issues[0]
    assert "semicolon-separated" in issues[0]


def test_parse_log_missing_file_blocks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, _, issues, blocking = v.parse_log("nope.csv")
    assert blocking and "Can't read nope.csv" in issues[0]


def visit(views, pages, last=None):
    return {"views": views, "pages": Counter(pages), "first": last, "last": last}


def test_resolve_ips_normalises_and_keeps_best(moltsets):
    replies = {
        "1.1.1.1": ("ok", {"companies": [{"domain": "acme.example", "percentage": "60"},
                                             {"domain": "https://WWW.Acme-Holdings.example/", "percentage": "87.5%"}]}),
        "208.67.222.222": ("ok", {"companies": [{"domain": "examplebeta.example"}]}),
        "9.9.9.9": ("not_found", {"companies": []}),
        "1.0.0.1": ("ok", {"companies": [{"domain": "", "percentage": "90"}]}),
    }
    moltsets.on("ip_to_company", lambda p: replies[p["ip_address"]])
    resolved, stopped = v.resolve_ips(list(replies))
    assert resolved == {"1.1.1.1": ("acme-holdings.example", 87.5), "208.67.222.222": ("examplebeta.example", None)}
    assert stopped is None


def test_resolve_ips_stops_and_keeps_what_it_has(moltsets):
    def reply(p):
        if p["ip_address"] == "9.9.9.9":
            return {"error": {"code": "insufficient_tokens", "message": "out"}}
        return ("ok", {"companies": [{"domain": "acme.example", "percentage": "70"}]})

    moltsets.on("ip_to_company", reply)
    resolved, stopped = v.resolve_ips(["1.1.1.1", "9.9.9.9", "208.67.222.222"])
    assert resolved == {"1.1.1.1": ("acme.example", 70.0)}
    assert stopped.startswith("insufficient_tokens")


def test_group_companies_sums_visits():
    t1, t2 = datetime(2026, 10, 15, 9, tzinfo=timezone.utc), datetime(2026, 10, 15, 11, tzinfo=timezone.utc)
    visits = {"a": visit(2, {"/pricing": 1, "/demo": 1}, t1), "b": visit(1, {"/pricing": 1}, t2)}
    companies = v.group_companies(visits, {"a": ("acme.example", 87.5), "b": ("acme.example", 60.0)})
    acme = companies["acme.example"]
    assert (acme["ips"], acme["views"], acme["ip_match_pct"]) == (2, 3, 87.5)
    assert acme["pages"].most_common(2) == [("/pricing", 2), ("/demo", 1)]
    assert (acme["first"], acme["last"]) == (t1, t2)


def test_block_reason():
    own, excluded = {"example.com"}, {"acme.example"}
    assert v.block_reason("example.com", own, excluded) == "your own domain"
    assert v.block_reason("acme.test", own, excluded) == "on your exclude list"
    assert v.block_reason("ec2-1-2-3-4.compute.amazonaws.com", own, excluded) == "internet provider, cloud or proxy"
    assert v.block_reason("bsnl.co.in", own, excluded) == "internet provider, cloud or proxy"
    assert v.block_reason("gmail.com", own, excluded) == "personal email provider"
    assert v.block_reason("college.ac.zz", own, excluded) == "university"
    assert v.block_reason("examplespace.example", own, excluded) is None


def test_order_companies_held_over_first_then_views_then_recent():
    early, late = datetime(2026, 10, 15, 9, tzinfo=timezone.utc), datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
    companies = [{"domain": "a.example", "views": 1, "last": early}, {"domain": "b.example", "views": 5, "last": early},
                 {"domain": "c.example", "views": 1, "last": late}, {"domain": "d.example", "views": 1, "last": None}]
    ledger = {"companies": {"d.example": {"pending": True}}}
    assert [c["domain"] for c in v.order_companies(companies, ledger)] == ["d.example", "b.example", "c.example", "a.example"]


def test_order_companies_already_searched_last(monkeypatch):
    today = date(2026, 10, 16)
    companies = [{"domain": d, "views": views, "last": None}
                 for d, views in (("fresh.example", 9), ("stale.example", 1), ("new.example", 2), ("held.example", 1))]
    ledger = {"companies": {"fresh.example": {"pending": False, "last_searched_run": "2026-10-01"},
                            "stale.example": {"pending": False, "last_searched_run": "2026-09-01"},  # cache expired
                            "held.example": {"pending": True, "last_searched_run": "2026-10-15"}}}
    order = [c["domain"] for c in v.order_companies(companies, ledger, today)]
    assert order == ["held.example", "new.example", "stale.example", "fresh.example"]
    monkeypatch.setitem(v.STATE, "refresh", True)  # --refresh: nothing comes from the cache
    order = [c["domain"] for c in v.order_companies(companies, ledger, today)]
    assert order == ["held.example", "fresh.example", "new.example", "stale.example"]


def test_ledger_round_trip_and_corrupt_file(tmp_path):
    path = str(tmp_path / "visitors.json")
    assert v.load_ledger(path) == {"companies": {}}
    write(path, "{not json")
    assert v.load_ledger(path) == {"companies": {}}
    ledger = {"companies": {}}
    first, last = datetime(2026, 10, 15, 9, tzinfo=timezone.utc), datetime(2026, 10, 15, 9, 2, tzinfo=timezone.utc)
    pages = Counter({"/pricing": 2, "/demo": 1, "/a": 1, "/b": 1, "/c": 1, "/d": 1})
    acme = {"domain": "acme.example", "ips": 2, "views": 7, "pages": pages, "first": first, "last": last,
            "ip_match_pct": 87.5, "pending": True}
    v.update_ledger(ledger, [acme], "2026-10-16")
    v.save_ledger(ledger, path)
    stats = {"views": 7, "ips": 2, "pages": {"/pricing": 2, "/demo": 1, "/a": 1, "/b": 1, "/c": 1},
             "first_seen": "2026-10-15T09:00:00Z", "last_seen": "2026-10-15T09:02:00Z", "ip_match_pct": 87.5}
    assert v.load_ledger(path) == {"companies": {"acme.example": {
        "first_seen_run": "2026-10-16", "last_run": "2026-10-16", "last_visit": "2026-10-15T09:02:00Z",
        "pending": True, "stats": stats}}}  # held over before its lookup: not searched yet

    v.update_ledger(ledger, [{**acme, "pending": False, "searched": True}], "2026-10-17")
    v.update_ledger(ledger, [{**acme, "pending": False, "searched": True}], "2026-10-18")
    entry = ledger["companies"]["acme.example"]
    assert (entry["first_searched_run"], entry["last_searched_run"]) == ("2026-10-17", "2026-10-18")


def test_search_fit_uses_worst_case_cost(moltsets):
    moltsets.on("get_account", lambda p: ("ok", account(search_left=13)))
    assert v.search_fit(5, 6) == 2
    moltsets.on("get_account", lambda p: ("ok", account(search_left=500)))
    assert v.search_fit(5, 6) == 5


E2E_LOG = ("timestamp,client_ip,url,user_agent\n"
           "2026-10-15T09:00:00Z,1.1.1.1,https://example.com/pricing,Mozilla/5.0\n"
           "2026-10-15T09:01:00Z,1.1.1.1,https://example.com/demo,Mozilla/5.0\n"
           "2026-10-15T09:02:00Z,1.0.0.1,/pricing,Mozilla/5.0\n"
           "2026-10-15T10:00:00Z,208.67.222.222,/,Mozilla/5.0\n"
           "2026-10-15T11:00:00Z,9.9.9.9,/careers,Mozilla/5.0\n"
           "2026-10-15T11:30:00Z,8.8.8.8,/,Mozilla/5.0\n"
           "2026-10-15T12:00:00Z,8.8.4.4,/,Mozilla/5.0\n")
IPS = {"1.1.1.1": ("acme.example", "87.5"), "1.0.0.1": ("acme.example", "60"), "208.67.222.222": ("examplebeta.example", "40"),
       "9.9.9.9": ("gamma.example", "90"), "8.8.8.8": ("amazonaws.com", "99"), "8.8.4.4": ("example.com", "95")}
FIRMS = {"acme.example": {"name": "Acme Robotics", "domain": "acme.example", "industry": "Manufacturing",
                      "employee_range": "51-200", "country": "India"},
         "examplebeta.example": {"name": "Example Beta", "domain": "examplebeta.example", "country": "United States"}}
ACME_COMPANY = {"name": "Acme Robotics", "domain": "acme.example"}
HEAD = {"full_name": "Asha Rao", "title": "Head of Procurement", "seniority": "Head", "country": "India",
        "linkedin_url": "https://www.linkedin.com/in/example_asha_rao", "business_email": "asha@acme.example",
        "business_email_risk_score": "A", "company": ACME_COMPANY}
CEO = {"full_name": "Meera Iyer", "title": "CEO", "country": "India",
       "linkedin_url": "https://www.linkedin.com/in/example_meera", "business_email": "meera@acme.example",
       "business_email_risk_score": "A", "company": ACME_COMPANY}


def fake_visitors(moltsets, search_left):
    moltsets.on("get_account", lambda p: ("ok", account(search_left=search_left)))
    moltsets.on("ip_to_company", lambda p: ("ok", {"companies": [
        {"domain": IPS[p["ip_address"]][0], "percentage": IPS[p["ip_address"]][1]}]}))
    moltsets.on("search_companies", lambda p: page([FIRMS[p["domain"]]], p) if p["domain"] in FIRMS
                else ("not_found", {"results": [], "total": 0}))

    def people(p):
        if p["company_domain"] != "acme.example":
            return ("not_found", {"results": [], "total": 0})
        if p.get("title") == ["Founder", "CEO", "Chief Executive"]:
            return page([CEO], p)
        return page([HEAD], p) if "seniority" in p else ("not_found", {"results": [], "total": 0})

    moltsets.on("search_people", people)


@pytest.fixture
def ledger_path(monkeypatch, tmp_path):
    path = str(tmp_path / "visitors.json")
    monkeypatch.setattr(v, "VISITORS_PATH", path)
    return path


def read_rows(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def test_run_visitors_end_to_end(moltsets, monkeypatch, ledger_path):
    write("log.csv", E2E_LOG)
    fake_visitors(moltsets, search_left=13)  # 13 // (1 + 3 + 2) = room for 2 companies
    run_cli(monkeypatch, "visitors", "--log", "log.csv", "--own-domain", "example.com", "--output", "out.csv")
    rows = read_rows("out.csv")
    assert [r["visitor_domain"] for r in rows] == ["acme.example", "acme.example", "gamma.example", "examplebeta.example"]
    acme = rows[0]
    assert acme["contact_full_name"] == "Meera Iyer" and rows[1]["contact_full_name"] == "Asha Rao"
    assert (acme["visitor_company"], acme["visitor_country"], acme["page_views"], acme["visitor_ip_count"]) == \
        ("Acme Robotics", "India", "3", "2")
    assert (acme["ip_match_pct"], acme["top_pages"], acme["new_company"]) == ("87.5", "/pricing; /demo", "yes")
    assert (acme["first_seen"], acme["last_seen"]) == ("2026-10-15T09:00:00Z", "2026-10-15T09:02:00Z")
    assert rows[2]["contact_search_status"] == "not in MoltSets"
    assert rows[3]["contact_search_status"] == "held over: not enough search results left this window"
    assert rows[3]["visitor_notes"] == "weak IP match (40%)"
    text = open("out.csv").read()
    assert not any(ip in text for ip in IPS)
    ledger = json.load(open(ledger_path))["companies"]
    assert set(ledger) == {"acme.example", "gamma.example", "examplebeta.example"}
    assert (ledger["acme.example"]["pending"], ledger["examplebeta.example"]["pending"]) == (False, True)
    assert ledger["acme.example"]["first_seen_run"] == date.today().isoformat()
    assert ledger["acme.example"]["stats"]["pages"] == {"/pricing": 2, "/demo": 1}
    assert not any(ip in open(ledger_path).read() for ip in IPS)  # domains only, never IPs

    moltsets.on("get_account", lambda p: ("ok", account(search_left=500)))
    run_cli(monkeypatch, "visitors", "--log", "log.csv", "--own-domain", "example.com", "--output", "out.csv")
    again = read_rows("out_2.csv")  # never overwritten
    assert again[0]["visitor_domain"] == "examplebeta.example"  # held over, so first
    assert again[0]["contact_search_status"].startswith("no contacts: Example Beta is in MoltSets")
    # examplebeta.example is searched for the first time now, so it's still new; acme.example and gamma.example were searched in run 1
    assert {(r["visitor_domain"], r["new_company"]) for r in again} == \
        {("examplebeta.example", "yes"), ("acme.example", "no"), ("gamma.example", "no")}
    assert not any(ip in open(ledger_path).read() for ip in IPS)


def test_run_visitors_ctrl_c_saves_and_holds_over(moltsets, monkeypatch, ledger_path):
    write("log.csv", E2E_LOG)
    fake_visitors(moltsets, search_left=500)

    def interrupt(p):
        raise KeyboardInterrupt

    moltsets.on("search_people", interrupt)
    run_cli(monkeypatch, "visitors", "--log", "log.csv", "--own-domain", "example.com", "--output", "out.csv")
    rows = read_rows("out.csv")
    assert rows[0]["contact_search_status"] == "0 contacts found before the run stopped (interrupted)"
    assert {r["contact_search_status"] for r in rows[1:]} == {"held over: run stopped"}
    assert all(entry["pending"] for entry in json.load(open(ledger_path))["companies"].values())


def test_run_visitors_nothing_to_do(moltsets, monkeypatch, ledger_path):
    write("log.csv", "ip,user_agent\n66.249.66.1,Googlebot/2.1\n10.0.0.5,Mozilla/5.0\n2001:db8::1,Mozilla/5.0\n")
    run_cli(monkeypatch, "visitors", "--log", "log.csv", "--output", "out.csv")
    assert not os.path.exists("out.csv")
    assert not os.path.exists(ledger_path)
    assert moltsets.calls == []


def test_visitors_flag_checks(moltsets, monkeypatch):
    for argv in (["visitors"], ["visitors", "--log", "x.csv", "--max-companies", "0"]):
        with pytest.raises(SystemExit):
            run_cli(monkeypatch, *argv)


ACME_LOG = ("timestamp,client_ip,url,user_agent\n"
            "2026-10-15T09:00:00Z,1.1.1.1,https://example.com/pricing,Mozilla/5.0\n"
            "2026-10-15T09:01:00Z,1.1.1.1,https://example.com/demo,Mozilla/5.0\n"
            "2026-10-15T09:02:00Z,1.0.0.1,/pricing,Mozilla/5.0\n")
VISITORS = ["visitors", "--log", "log.csv", "--own-domain", "example.com", "--output", "out.csv"]


def people_calls(moltsets, domain):
    return [p for p in moltsets.calls_to("search_people") if p["company_domain"] == domain]


def test_run_visitors_unreadable_exclude_file_blocks(moltsets, monkeypatch, ledger_path, capsys):
    write("log.csv", E2E_LOG)
    with pytest.raises(SystemExit) as stop:
        run_cli(monkeypatch, *VISITORS, "--exclude-file", "missing.csv")
    assert "Fix the exclude file(s)" in str(stop.value.code)
    assert "Can't read missing.csv" in capsys.readouterr().out
    assert moltsets.calls == []
    assert not os.path.exists("out.csv") and not os.path.exists(ledger_path)


def test_run_visitors_searches_held_over_company_missing_from_the_log(moltsets, monkeypatch, ledger_path):
    write("log.csv", E2E_LOG)
    fake_visitors(moltsets, search_left=13)  # room for 2 companies: examplebeta.example is held over
    run_cli(monkeypatch, *VISITORS)
    assert json.load(open(ledger_path))["companies"]["examplebeta.example"]["pending"] is True

    write("log.csv", ACME_LOG)  # a rolling export: examplebeta.example isn't in it any more
    moltsets.on("get_account", lambda p: ("ok", account(search_left=500)))
    run_cli(monkeypatch, *VISITORS)
    beta = next(r for r in read_rows("out_2.csv") if r["visitor_domain"] == "examplebeta.example")
    assert beta["contact_search_status"].startswith("no contacts: Example Beta is in MoltSets")
    assert (beta["page_views"], beta["visitor_ip_count"], beta["top_pages"]) == ("1", "1", "/")  # from the record
    assert (beta["first_seen"], beta["last_seen"], beta["ip_match_pct"]) == \
        ("2026-10-15T10:00:00Z", "2026-10-15T10:00:00Z", "40")
    assert people_calls(moltsets, "examplebeta.example")
    entry = json.load(open(ledger_path))["companies"]["examplebeta.example"]
    assert entry["pending"] is False and entry["first_searched_run"] == date.today().isoformat()


def test_run_visitors_reemits_already_searched_outside_the_cap(moltsets, monkeypatch, ledger_path):
    write("log.csv", ACME_LOG)
    fake_visitors(moltsets, search_left=500)
    run_cli(monkeypatch, *VISITORS, "--max-companies", "1")
    acme_calls = len(people_calls(moltsets, "acme.example"))
    assert acme_calls

    write("log.csv", ACME_LOG + "2026-10-16T10:00:00Z,208.67.222.222,/,Mozilla/5.0\n")  # cumulative: one new company
    run_cli(monkeypatch, *VISITORS, "--max-companies", "1")
    rows = read_rows("out_2.csv")
    assert [(r["visitor_domain"], r["contact_full_name"]) for r in rows] == \
        [("examplebeta.example", ""), ("acme.example", "Meera Iyer"), ("acme.example", "Asha Rao")]
    assert rows[0]["contact_search_status"].startswith("no contacts: Example Beta is in MoltSets")  # searched
    assert len(people_calls(moltsets, "acme.example")) == acme_calls  # re-emitted from the cache
    ledger = json.load(open(ledger_path))["companies"]
    assert (ledger["acme.example"]["pending"], ledger["examplebeta.example"]["pending"]) == (False, False)


def test_run_visitors_stop_while_resolving_still_searches(moltsets, monkeypatch, ledger_path):
    write("log.csv", E2E_LOG)
    fake_visitors(moltsets, search_left=500)
    moltsets.on("ip_to_company", lambda p: {"error": {"code": "insufficient_tokens", "message": "out"}}
                if p["ip_address"] == "208.67.222.222" else ("ok", {"companies": [
                    {"domain": IPS[p["ip_address"]][0], "percentage": IPS[p["ip_address"]][1]}]}))
    run_cli(monkeypatch, *VISITORS)
    # busiest first: 1.1.1.1 and 1.0.0.1 (acme.example) resolve before the stop at 208.67.222.222
    assert moltsets.calls_to("ip_to_company")[-1] == {"ip_address": "208.67.222.222"}
    rows = read_rows("out.csv")
    assert [r["contact_full_name"] for r in rows] == ["Meera Iyer", "Asha Rao"]
    assert json.load(open(ledger_path))["companies"]["acme.example"]["pending"] is False


def test_run_visitors_caps_uncached_ips_at_enrichment_left(moltsets, monkeypatch, ledger_path, capsys):
    write("log.csv", E2E_LOG)
    fake_visitors(moltsets, search_left=500)
    v.resolve_ips(["8.8.4.4", "8.8.8.8"])  # cached by an earlier run
    assert api.is_cached("ip_to_company", {"ip_address": "8.8.4.4"})
    assert not api.is_cached("ip_to_company", {"ip_address": "1.1.1.1"})
    before = len(moltsets.calls_to("ip_to_company"))
    # 6 IPs, defaults 3 + 2 per company: 30 records kept for email lookups, so 32 left = 2 uncached IPs now
    moltsets.on("get_account", lambda p: ("ok", account(search_left=500, enrich_left=32)))
    run_cli(monkeypatch, "visitors", "--log", "log.csv", "--output", "out.csv")
    resolved_now = [p["ip_address"] for p in moltsets.calls_to("ip_to_company")[before:]]
    assert resolved_now == ["1.1.1.1", "1.0.0.1"]  # the busiest uncached; cached IPs cost nothing
    out = capsys.readouterr().out
    assert "2 uncached IPs wait for the next run" in out
    assert "Note: no --own-domain given" in out
    assert {r["visitor_domain"] for r in read_rows("out.csv")} == {"acme.example", "example.com"}  # 8.8.4.4 from cache
    monkeypatch.setitem(v.STATE, "refresh", True)
    assert not api.is_cached("ip_to_company", {"ip_address": "8.8.4.4"})


def test_run_visitors_new_company_until_first_searched(moltsets, monkeypatch, ledger_path):
    write("log.csv", E2E_LOG)
    fake_visitors(moltsets, search_left=13)
    run_cli(monkeypatch, *VISITORS)
    beta = [r for r in read_rows("out.csv") if r["visitor_domain"] == "examplebeta.example"]
    assert [(r["new_company"], r["contact_search_status"][:9]) for r in beta] == [("yes", "held over")]
    assert "first_searched_run" not in json.load(open(ledger_path))["companies"]["examplebeta.example"]

    moltsets.on("get_account", lambda p: ("ok", account(search_left=500)))
    for run, expected in (("out_2.csv", "yes"), ("out_3.csv", "no")):
        run_cli(monkeypatch, *VISITORS)
        beta = next(r for r in read_rows(run) if r["visitor_domain"] == "examplebeta.example")
        assert beta["contact_search_status"].startswith("no contacts") and beta["new_company"] == expected, run


def test_run_visitors_ctrl_c_in_budget_check_still_saves(moltsets, monkeypatch, ledger_path):
    write("log.csv", E2E_LOG)
    fake_visitors(moltsets, search_left=500)

    def get_account(p):
        if moltsets.calls_to("ip_to_company"):  # the account check after resolving: search_fit
            raise KeyboardInterrupt
        return ("ok", account())

    moltsets.on("get_account", get_account)
    with pytest.raises(KeyboardInterrupt):
        run_cli(monkeypatch, *VISITORS)
    rows = read_rows("out.csv")
    assert [r["visitor_domain"] for r in rows] == ["acme.example", "gamma.example", "examplebeta.example"]
    assert {r["contact_search_status"] for r in rows} == {"held over: run stopped"}
    assert moltsets.calls_to("search_people") == []
    ledger = json.load(open(ledger_path))["companies"]
    assert set(ledger) == {"acme.example", "gamma.example", "examplebeta.example"}
    assert all(entry["pending"] for entry in ledger.values())


def test_run_visitors_log_without_visits_still_searches_held_over(moltsets, monkeypatch, ledger_path):
    write("log.csv", E2E_LOG)
    fake_visitors(moltsets, search_left=13)
    run_cli(monkeypatch, *VISITORS)
    write("log.csv", "ip,user_agent\n66.249.66.1,Googlebot/2.1\n")  # nothing usable this period
    moltsets.on("get_account", lambda p: ("ok", account(search_left=500)))
    run_cli(monkeypatch, *VISITORS)
    rows = read_rows("out_2.csv")
    assert [r["visitor_domain"] for r in rows] == ["examplebeta.example"]
    assert rows[0]["contact_search_status"].startswith("no contacts: Example Beta is in MoltSets")
