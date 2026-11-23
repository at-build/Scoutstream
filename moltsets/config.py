"""Constants and MoltSets vocabularies shared by every mode."""

import os
import re

from dotenv import load_dotenv


load_dotenv()


BASE_URL = "https://api.moltsets.com/api/v1/tools"


API_KEY = os.environ.get("MOLTSETS_API_KEY")


USER_AGENT = "scoutstream/1.0"  # required: requests without a User-Agent get 403


SPEC_DOC = "MOLTSETS_SPEC.md"


# Tools that draw on the fair-use *search* allowance; everything else used here draws on
# the (larger) enrichment allowance.
SEARCH_TOOLS = {
    "search_people",
    "search_companies",
    "search_linkedin_profile",
    "search_business_email_by_name",
    "search_business_profile_by_name",
}


FAIR_USE_CODES = {"fair_use_limit_exceeded", "fair_use_requests_exceeded"}


# Fair-use windows that reopen within this many seconds are waited out; longer ones
# stop the run (the 5-hour window can be hours away).
MAX_WAIT_SECONDS = 120


MAX_PER_CALL_PAID = 25  # search limit max; the free plan reports its own (5) via get_account


SEARCH_WINDOW = 10_000  # offset + limit can't exceed this


# What to do with an email, by MoltSets' send-safety grade (precedence D > A > B > C > F).
RISK_ACTIONS = {
    "A": "send",
    "B": "probably send",
    "C": "your call (catch-all domain)",
    "D": "do not send",
    "F": "your call (no signal)",
}


# Fixed MoltSets vocabularies (exact match; sent in this canonical spelling).
INDUSTRIES = [
    "Professional and Business Services", "Information Technology", "Manufacturing", "Retail",
    "Health and Pharmaceuticals", "Creative Arts and Entertainment", "Construction",
    "Marketing & Advertising", "Finance and Banking", "Tourism and Hospitality",
    "Non-Profit and Social Services", "Food and Beverage", "Real Estate", "Education",
    "Transportation and Logistics", "Media and Publishing", "Energy",
    "Government and Public Administration", "Automotive", "Telecommunications", "Agriculture", "Utilities",
]


EMPLOYEE_RANGES = ["1-10", "11-20", "21-50", "51-200", "201-500", "501-1000", "1001-5000", "5001+"]


REVENUE_RANGES = ["Below $500k", "$500k - $1M", "$1M - $5M", "$5M - $10M", "$10M - $20M", "$20M - $50M", "Above $50M"]


# MoltSets seniority values with their ladder position (used for ranking).
SENIORITY_LADDER = {
    "Intern": 0, "Entry": 1, "Senior": 2, "Manager": 3, "Director": 4, "Head": 4,
    "VP": 5, "Partner": 5, "Owner": 6, "C Suite": 6,
}


DIRECTOR_PLUS = ["Director", "Head", "VP", "Partner", "Owner", "C Suite"]


SENIORITY_TERMS = {
    "director+": DIRECTOR_PLUS,
    "leadership": DIRECTOR_PLUS,
    "executive": ["C Suite", "Owner"],
    "c-suite": ["C Suite", "Owner"],
    "founder": ["Owner", "C Suite"],
    "vp+": ["VP", "Partner", "Owner", "C Suite"],
    "vp": ["VP"],
    "vice president": ["VP"],
    "director": ["Director", "Head"],
    "manager+": ["Manager"] + DIRECTOR_PLUS,
    "manager": ["Manager"],
    "junior": ["Intern", "Entry"],
}


DEFAULT_SENIORITY = "director+"


ALL_TERMS = {"all", "any"}


# Fallback when a person has no seniority on record (~40% of profiles): infer the
# ladder position from the job title. Checked top to bottom, so "Vice President"
# isn't read as "President" and "Chief ... Officer" isn't read as "Officer".
TITLE_LEVELS = [
    (5, ["vice president", "vp", "avp"]),
    (6, ["chief", "ceo", "cfo", "coo", "cpo", "cto", "president", "founder", "owner", "managing director"]),
    (4, ["director", "head", "general manager", "gm"]),
    (3, ["manager", "lead"]),
    (2, ["senior", "sr"]),
    (0, ["intern", "trainee"]),
    (1, ["executive", "associate", "analyst", "engineer", "buyer", "officer", "specialist", "coordinator"]),
]


# Everyday role terms -> MoltSets `title` keywords (a list is OR-matched). MoltSets has no
# procurement/supply-chain department (closest: "Operations", ~60% filled), so roles are
# searched by job title (~85% filled) instead. Unlisted terms are used as title keywords as-is.
JOB_TITLE_HINTS = {
    "purchasing": ["Procurement", "Purchasing", "Purchase", "Sourcing", "Buyer"],
    "procurement": ["Procurement", "Purchasing", "Purchase", "Sourcing", "Buyer"],
    "founder": ["Founder"],
    "founders": ["Founder"],
    # "Materials" isn't a default: at aerospace / hardware companies it mostly returns materials engineers
    # (3 of one hardware company's 5 role slots in testing). Ask for `materials` explicitly for "Materials Manager"-style roles.
    "supply chain": ["Supply Chain", "Logistics"],
    "supply-chain": ["Supply Chain", "Logistics"],
    "materials": ["Materials"],
    "logistics": ["Logistics", "Supply Chain"],
}


COUNTRY_ALIASES = {
    "usa": "United States", "us": "United States", "u.s.": "United States", "america": "United States",
    "united states of america": "United States", "uk": "United Kingdom", "u.k.": "United Kingdom",
    "britain": "United Kingdom", "great britain": "United Kingdom", "england": "United Kingdom",
    "uae": "United Arab Emirates", "hk": "Hong Kong", "korea": "South Korea",
}


FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "hotmail.com", "outlook.com", "live.com",
    "icloud.com", "me.com", "aol.com", "rediffmail.com", "protonmail.com", "proton.me", "zoho.com",
}


# Personal-mailbox providers, matched on the organisation part of the domain (yahoo.co.in, hotmail.fr, ...).
PERSONAL_EMAIL_PROVIDERS = {
    "gmail", "googlemail", "yahoo", "ymail", "hotmail", "outlook", "live", "msn", "icloud", "me", "aol",
    "rediffmail", "protonmail", "proton", "zoho", "gmx", "mail",
}


ACADEMIC_DOMAIN_RE = re.compile(r"(\.edu|\.ac\.[a-z]{2}|\.edu\.[a-z]{2})$")


# Titles searched by --founders, and how a founder title is recognised.
FOUNDER_TITLES = ["Founder", "CEO", "Chief Executive"]


FOUNDER_RE = re.compile(r"founder|\bceo\b|chief executive", re.I)


# Titles that match a keyword but aren't the role: "Executive Assistant to the Co-Founder", interns.
WEAK_TITLE_RE = re.compile(
    r"executive assistant|personal assistant|administrative assistant|assistant to\b|\bintern\b|internship|"
    r"\btrainee\b|\bstudent\b", re.I)


# Keywords that also match unrelated jobs; a title matched only by these, and containing "engineer", is weak.
NOISY_TITLE_KEYWORDS = {"materials"}


# Local cache of paid lookups, so re-runs and merges don't spend search results or tokens twice.
CACHE_PATH = os.environ.get("MOLTSETS_CACHE") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".moltsets_cache.json")  # repo root


CACHE_TTL_SECONDS = 30 * 24 * 3600


UNCACHED_TOOLS = {"get_account", "get_billing", "get_usage"}


# Dropped when comparing company names ("Example Electric India Ltd" == "Example Electric").
COMPANY_NOISE = re.compile(
    r"\b(private|pvt|limited|ltd|inc|llc|llp|plc|corp|corporation|co|company|group|india|"
    r"technologies|technology|tech|solutions|industries|the|and)\b"
)


PROVIDERS = ["lusha", "crustdata", "explorium", "rocketreach", "adapt", "apollo", "zoominfo", "moltsets"]


# Accepted CSV headers (matched case-insensitively; spaces and hyphens count as "_").
NAME_COLUMNS = ("name", "full_name", "prospect", "contact_name")


FIRST_NAME_COLUMNS = ("first_name", "firstname")


LAST_NAME_COLUMNS = ("last_name", "lastname", "surname")


DOMAIN_COLUMNS = ("company_domain", "domain", "website", "company_website", "company_url", "target_domain")


COMPANY_COLUMNS = ("company", "company_name", "target_company", "organization", "organisation")


LINKEDIN_COLUMNS = ("linkedin_url", "linkedin", "linkedin_profile", "linkedin_profile_url")


EMAIL_COLUMNS = ("email", "business_email", "work_email", "email_address")


LOCATION_COLUMNS = ("location", "country")


DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")


EMAIL_RE = re.compile(r"^[^@\s;,]+@([a-z0-9.-]+\.[a-z]{2,})$", re.I)


LINKEDIN_RE = re.compile(r"linkedin\.com/in/([^/?#\s]+)", re.I)


LINKEDIN_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_%-]*$", re.I)


# lookalikes: most seed accounts per run (each one is a company lookup).
MAX_SEEDS = 100
# search_companies accepts at most this many exclude_domain values; the rest are
# filtered locally, and each one MoltSets returns costs a search result.
MAX_EXCLUDE_DOMAINS = 100


# visitors: log columns (headers matched case-insensitively; spaces and hyphens count as "_").
IP_COLUMNS = ("ip", "ip_address", "client_ip", "clientip", "remote_addr", "remote_ip", "c_ip", "x_forwarded_for")
TIME_COLUMNS = ("timestamp", "time", "datetime", "date", "edgestarttimestamp")
PAGE_COLUMNS = ("url", "page", "page_url", "path", "request_uri", "clientrequestpath")
USER_AGENT_COLUMNS = ("user_agent", "ua", "clientrequestuseragent")
BOT_RE = re.compile(r"bot|crawl|spider|slurp|curl|python-requests|headless|uptime|monitor|preview", re.I)
STATIC_ASSET_RE = re.compile(r"\.(?:js|css|png|jpe?g|gif|svg|ico|woff2?|map)$", re.I)
# IPs that resolve to these organisations (matched on domain_stem) are networks, not visitors worth prospecting.
VISITOR_BLOCKLIST = {
    # internet providers and telecoms
    "jio", "airtel", "bsnl", "vodafone", "myvi", "actcorp", "hathway", "tatacommunications", "comcast", "verizon",
    "att", "charter", "spectrum", "cox", "t-mobile", "bt", "sky", "virginmedia",
    # clouds, CDNs and corporate proxies
    "amazonaws", "amazon", "aws", "google", "googleusercontent", "microsoft", "azure", "cloudflare", "akamai",
    "fastly", "digitalocean", "linode", "ovh", "hetzner", "oracle", "alibaba", "zscaler",
}
WEAK_IP_MATCH = 50  # an ip_to_company percentage below this is noted as a weak match
# Which visiting companies the script has seen, and which are held over. Domains only, never IPs.
VISITORS_PATH = os.environ.get("MOLTSETS_VISITORS") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".moltsets_visitors.json")
