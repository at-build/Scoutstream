"""account mode: plan, tokens and allowances (free)."""

from moltsets.api import (
    call,
    describe_search_allowance,
    fetch_account,
    remind_if_search_exhausted,
    STATE,
    StopRun,
    ToolError,
)


def run_account(args):
    acct = fetch_account()
    raw = acct["raw"]
    tokens = acct["tokens"]
    print(f"Plan: {acct['plan']} ({acct['status']})")
    print(f"Tokens: {'unlimited' if tokens == -1 else tokens}")
    print(f"Search results left: {describe_search_allowance()}; max {STATE['max_per_call']} per call")
    fair_use = raw.get("fair_use") or {}
    for pool in ("search", "enrich"):
        for kind in ("records", "requests"):
            for window, w in ((fair_use.get(pool) or {}).get(kind) or {}).items():
                resets = f", resets {w['resets_at']}" if w.get("resets_at") else ""
                print(f"  {pool} {kind} ({window}): {w.get('remaining')}/{w.get('limit')} left{resets}")
    print(f"Phone tokens: {acct['phone_tokens']} (plan: {raw.get('phone_plan') or 'none'})")
    print(f"Personal emails (US only) available: {acct['personal_email']}")
    try:
        _, usage = call("get_usage", {"period": "today"})
        totals = usage.get("totals") or {}
        print(
            f"Today: {totals.get('calls', 0)} calls, {totals.get('with_data', 0)} with data, "
            f"{totals.get('tokens', 0)} tokens"
        )
    except (ToolError, StopRun):
        pass
    remind_if_search_exhausted()
