"""MoltSets API client: call() with caching and retries, run state, account and budget checks."""

import json
import os
import time

import requests

from moltsets.config import (
    API_KEY,
    BASE_URL,
    CACHE_PATH,
    CACHE_TTL_SECONDS,
    FAIR_USE_CODES,
    MAX_PER_CALL_PAID,
    MAX_WAIT_SECONDS,
    SEARCH_TOOLS,
    SEARCH_WINDOW,
    SPEC_DOC,
    UNCACHED_TOOLS,
    USER_AGENT,
)
from moltsets.safety import clean, clean_text, open_private


# Run-wide state: allowances seen in response metadata, and switches flipped mid-run.
STATE = {
    "search_remaining": None,  # search results left (free trial, or the 5h pool on paid plans)
    "trial_plan": False,  # True on the free plan (lifetime search trial)
    "max_per_call": MAX_PER_CALL_PAID,
    "search_blocked": None,  # set when the search allowance runs out mid-run
    "phones": False,
    "last_metadata": {},
    "refresh": False,  # --refresh: ignore cached responses (fresh ones are still saved)
    "cache": None,  # loaded lazily from CACHE_PATH
    "cache_hits": 0,
}


class StopRun(Exception):
    """Out of tokens / enrichment allowance, or an account problem: stop and save what we have."""


class SearchLimitReached(Exception):
    """The search allowance ran out; lookups that don't search can still continue."""


class PhoneTokensExhausted(Exception):
    pass


class ToolError(Exception):
    """One call failed (bad input, server error); the row is kept with what was found."""


def _headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


def _track(tool, metadata):
    if not metadata:
        return
    STATE["last_metadata"] = metadata
    trial = metadata.get("search_trial")
    if trial and trial.get("results_remaining") is not None:
        STATE["search_remaining"] = trial["results_remaining"]
    elif tool in SEARCH_TOOLS and (metadata.get("fair_use") or {}).get("records_remaining_5h") is not None:
        STATE["search_remaining"] = metadata["fair_use"]["records_remaining_5h"]


def _cache():
    if STATE["cache"] is None:
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                STATE["cache"] = json.load(f)
        except (OSError, ValueError):
            STATE["cache"] = {}
        if not isinstance(STATE["cache"], dict):  # a damaged or hand-edited file: start again
            STATE["cache"] = {}
    return STATE["cache"]


def _cache_key(tool, payload):
    return None if tool in UNCACHED_TOOLS else f"{tool} {json.dumps(payload, sort_keys=True)}"


def is_cached(tool, payload):
    """True when call(tool, payload) would be answered from the local cache, i.e. free."""
    key = _cache_key(tool, payload)
    hit = _cache().get(key) if key and not STATE["refresh"] else None
    if not (isinstance(hit, dict) and isinstance(hit.get("at"), (int, float)) and "status" in hit):
        return False
    return time.time() - hit["at"] < CACHE_TTL_SECONDS


def _cache_put(key, status, results):
    cache = _cache()
    cache[key] = {"at": time.time(), "status": status, "results": results}
    tmp = f"{CACHE_PATH}.tmp"
    try:
        with open_private(tmp) as f:  # contact data: readable by you only
            json.dump(cache, f)
        os.replace(tmp, CACHE_PATH)
    except OSError as e:
        print(f"   (couldn't save the lookup cache: {e})")


def call(tool, payload):
    """POST to a MoltSets tool and return (status, results).

    status is "ok" (data found, tokens charged) or "not_found" (a normal miss, free;
    MoltSets returns HTTP 200 for both). Errors come back as {"error": {"code", ...}}
    and are raised as StopRun / SearchLimitReached / PhoneTokensExhausted / ToolError.
    Burst rate limits and short fair-use waits are retried here. Paid lookups are
    answered from the local cache when the same call was made in the last 30 days.

    Everything MoltSets returns is third-party text (names and job titles are written by the people
    themselves), so results and error messages go through clean(): no control or invisible characters,
    no line breaks, no over-long strings.
    """
    key = _cache_key(tool, payload)
    if is_cached(tool, payload):
        hit = _cache()[key]
        STATE["cache_hits"] += 1
        return hit["status"], clean(hit.get("results") or {})
    backoff = 2
    for attempt in range(5):
        try:
            # No redirects: the API key must never follow one to another host.
            resp = requests.post(f"{BASE_URL}/{tool}", json=payload, headers=_headers(), timeout=60,
                                 allow_redirects=False)
        except requests.exceptions.RequestException as e:
            if attempt < 2:
                time.sleep(backoff)
                backoff *= 2
                continue
            raise ToolError(f"{tool}: network error ({e})")
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        _track(tool, metadata)
        if 200 <= resp.status_code < 300:
            status, results = body.get("status"), clean(body.get("results") or {})
            if not isinstance(results, dict):
                raise ToolError(f"{tool}: unexpected reply from MoltSets")
            if key and status in ("ok", "not_found"):
                _cache_put(key, status, results)
            return status, results
        if 300 <= resp.status_code < 400:
            raise ToolError(f"{tool}: unexpected redirect (HTTP {resp.status_code}), not followed")

        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = clean_text(str(error.get("code") or f"http_{resp.status_code}"), 60)
        message = clean_text(str(error.get("message") or resp.text[:200]), 200)
        retry_after = metadata.get("retry_after") or resp.headers.get("Retry-After")
        retry_after = str(retry_after or "")[:9]  # isascii: "²" is a digit to isdigit() but not to int()
        retry_after = int(retry_after) if retry_after.isascii() and retry_after.isdigit() else None

        if code == "rate_limited" or (code in FAIR_USE_CODES and retry_after and retry_after <= MAX_WAIT_SECONDS):
            wait = min(retry_after or backoff, MAX_WAIT_SECONDS)  # the server never decides an unbounded wait
            print(f"   MoltSets rate limit ({code}); waiting {wait}s ...")
            time.sleep(wait)
            backoff *= 2
            continue
        if code == "internal_error" and attempt == 0:
            time.sleep(5)  # usually a temporarily unavailable endpoint: one retry, then move on
            continue
        if code in FAIR_USE_CODES:
            when = f" (reopens in ~{retry_after // 60} min)" if retry_after else ""
            if tool in SEARCH_TOOLS:
                if not retry_after:  # the free search trial never refreshes
                    STATE["search_remaining"] = 0
                raise SearchLimitReached(f"{code}{when}: {message}")
            raise StopRun(f"{code}{when}: {message}")
        if code == "insufficient_phone_tokens":
            raise PhoneTokensExhausted(message)
        if code in ("unauthorized", "forbidden", "account_inactive", "insufficient_tokens"):
            raise StopRun(f"{code}: {message}")
        raise ToolError(f"{tool}: {code} ({message})")
    raise ToolError(f"{tool}: still rate-limited after retries")


def try_call(tool, payload, notes):
    """call(), but a one-off failure is noted on the row instead of aborting it."""
    try:
        return call(tool, payload)
    except ToolError as e:
        notes.append(str(e))
        return None, {}


def paged_search(tool, filters, max_results):
    """Run a search, paging until max_results are collected. Returns (results, total)."""
    found, total, offset = [], None, 0
    per_call = STATE["max_per_call"]
    while len(found) < max_results:
        limit = min(per_call, max_results - len(found))
        if offset + limit > SEARCH_WINDOW:
            break
        try:
            status, res = call(tool, {**filters, "limit": limit, "offset": offset})
        except SearchLimitReached as e:
            if not found:
                raise
            STATE["search_blocked"] = str(e)  # keep the pages already paid for
            break
        total = res.get("total", total)
        page = res.get("results") or [] if status == "ok" else []
        found += page
        offset += len(page)
        if len(page) < limit or (total is not None and offset >= total):
            break
    return found, total or 0


def lookup_mobile(linkedin, notes):
    if not (STATE["phones"] and linkedin):
        return {}
    try:
        status, res = try_call("linkedin_to_mobile_phone", {"linkedin_url": linkedin}, notes)
        return res if status == "ok" else {}
    except PhoneTokensExhausted as e:
        STATE["phones"] = False
        print(f"   Out of MoltSets phone tokens — skipping phone lookups from here on ({e}).")
        notes.append("phone lookup skipped: out of phone tokens")
        return {}


def fetch_account():
    _, account = call("get_account", {})
    trial = account.get("search_trial")
    fair_use = account.get("fair_use") or {}
    search_pool = ((fair_use.get("search") or {}).get("records") or {}).get("5h") or {}
    enrich_pool = ((fair_use.get("enrich") or {}).get("records") or {}).get("5h") or {}
    if trial:
        STATE["trial_plan"] = True
        STATE["search_remaining"] = trial.get("results_remaining")
        STATE["max_per_call"] = trial.get("max_per_call") or 5
    else:
        STATE["search_remaining"] = search_pool.get("remaining")
    return {
        "status": account.get("status"),
        "plan": account.get("plan"),
        "tokens": account.get("token_balance"),
        "search_left": STATE["search_remaining"],
        "enrich_left": enrich_pool.get("remaining"),
        "phone_tokens": account.get("phone_token_balance") or 0,
        "personal_email": account.get("personal_email_available"),
        "raw": account,
    }


def describe_search_allowance():
    left = STATE["search_remaining"]
    if left is None:
        return "unknown"
    return f"{left} (free trial, lifetime)" if STATE["trial_plan"] else f"{left} (this 5-hour window)"


def check_budget(search_needed=0, tokens_needed=0, phones_wanted=0, search_required=False):
    """Free pre-flight via get_account. Returns (issues, blocking) and prints balances.

    The *_needed figures are upper bounds: misses use neither tokens nor search results.
    """
    print("Checking your MoltSets account (free) ...")
    try:
        acct = fetch_account()
    except (StopRun, ToolError, SearchLimitReached) as e:
        return [f"MoltSets account check failed: {e}"], True
    if acct["status"] not in (None, "active"):
        return [f"MoltSets account status is {acct['status']!r} — log in at https://app.moltsets.com."], True
    tokens = acct["tokens"]
    print(
        f"   Plan: {acct['plan']}  Tokens: {'unlimited' if tokens == -1 else tokens}  "
        f"Search results left: {describe_search_allowance()}  Max per search call: {STATE['max_per_call']}"
        + (f"  Phone tokens: {acct['phone_tokens']}" if phones_wanted else "")
    )

    issues, blocking = [], False
    search_left = acct["search_left"]
    if search_needed and search_left is not None:
        if search_left <= 0:
            issues.append("Your MoltSets search allowance is used up" + (
                " (free trial)." if STATE["trial_plan"] else " for this 5-hour window."
            ) + (" Nothing to run without it." if search_required else " Name lookups will be skipped."))
            blocking = search_required
        elif search_left < search_needed:
            issues.append(
                f"This run can use up to {search_needed} search results but {search_left} are left. Once they run "
                "out, remaining searches are skipped and the script saves what it has."
            )
    if tokens not in (None, -1) and tokens < tokens_needed:
        issues.append(
            f"Token balance is {tokens}; this run can use up to {tokens_needed} (only found data is charged). "
            "If tokens run out the script stops and saves what it has."
        )
    if acct["enrich_left"] is not None and acct["enrich_left"] < tokens_needed:
        issues.append(
            f"Up to {tokens_needed} enrichment records may be needed but {acct['enrich_left']} are left this window."
        )
    if phones_wanted:
        if acct["phone_tokens"] <= 0:
            issues.append("--phones: your account has no phone tokens — continuing without phone lookups.")
            STATE["phones"] = False
        elif acct["phone_tokens"] < phones_wanted:
            issues.append(
                f"--phones: {acct['phone_tokens']} phone token(s) for up to {phones_wanted} lookups — phone lookups "
                "stop when they run out (1 token per number found)."
            )
    return issues, blocking


def remind_if_search_exhausted():
    if STATE["trial_plan"] and STATE["search_remaining"] == 0:
        print(
            "\n>> Reminder: your MoltSets free search trial (100 results) is used up. Upgrade to the $27 or $97 plan, "
            f"then build the deferred use cases in {SPEC_DOC} ('Deferred until paid plan')."
        )
