"""Test setup: a fake MoltSets API behind requests.post, a throwaway lookup cache, and golden-file helpers.

Every mode reaches MoltSets through call() -> requests.post, so patching requests.post covers the
single-file script and the moltsets/ package alike. Tests import only `moltsets_poc` (as `cli`), so the
same tests run before and after the package split.
"""

import json
import os
import sys
import tempfile

import pytest
import requests

# Before moltsets_poc is imported: CACHE_PATH and API_KEY are read at import time.
os.environ["MOLTSETS_CACHE"] = os.path.join(tempfile.mkdtemp(), "cache.json")
os.environ.setdefault("MOLTSETS_API_KEY", "ms_test")

import moltsets_poc as cli  # noqa: E402

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden")


class FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.ok = status_code < 400
        self.headers = {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


def account(search_left=500, enrich_left=1000):
    """A get_account result for a paid plan with these allowances left."""
    return {"status": "active", "plan": "starter", "token_balance": -1, "phone_token_balance": 0,
            "fair_use": {"search": {"records": {"5h": {"remaining": search_left}}},
                         "enrich": {"records": {"5h": {"remaining": enrich_left}}}}}


class FakeMoltSets:
    """handler(payload) -> (status, results), or {"error": {"code": ..., "message": ...}} for an API error."""

    def __init__(self):
        self.handlers = {"get_account": lambda payload: ("ok", account()),
                         "get_usage": lambda payload: ("ok", {"totals": {}})}
        self.calls = []

    def on(self, tool, handler):
        self.handlers[tool] = handler

    def post(self, url, json=None, headers=None, timeout=None, **kwargs):
        tool = url.rsplit("/", 1)[-1]
        self.calls.append((tool, json))
        if tool not in self.handlers:
            raise AssertionError(f"unexpected MoltSets call: {tool} {json}")
        reply = self.handlers[tool](json)
        if isinstance(reply, dict) and "error" in reply:
            return FakeResponse({"error": reply["error"], "metadata": {}}, status_code=400)
        status, results = reply
        return FakeResponse({"status": status, "results": results, "metadata": {}})

    def calls_to(self, tool):
        return [payload for t, payload in self.calls if t == tool]


def page(items, payload):
    """A search reply honouring limit / offset, the way MoltSets pages results."""
    offset, limit = payload.get("offset", 0), payload.get("limit", len(items))
    chunk = items[offset:offset + limit]
    return ("ok" if chunk else "not_found"), {"results": chunk, "total": len(items)}


@pytest.fixture
def moltsets(monkeypatch, tmp_path):
    """Fake API, fresh run state, and the test's own folder as the working directory.

    input() answers "y" to every prompt; a test that expects a blocking pre-check must patch it to "q",
    or precheck() keeps asking for a corrected file.
    """
    fake = FakeMoltSets()
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    fresh = {"search_remaining": None, "trial_plan": False, "max_per_call": 25, "search_blocked": None,
             "phones": False, "last_metadata": {}, "refresh": False, "cache": {}, "cache_hits": 0}
    for key, value in fresh.items():
        monkeypatch.setitem(cli.STATE, key, value)
    monkeypatch.chdir(tmp_path)
    return fake


def write(path, text):
    with open(path, "w", newline="") as f:
        f.write(text)


def run_cli(monkeypatch, *argv):
    """Run moltsets_poc.py's main() with these arguments."""
    monkeypatch.setattr(sys, "argv", ["moltsets_poc.py", *argv])
    cli.main()


def snapshot(capsys, *paths):
    """Console output so far, plus the named files, as one text for golden comparison."""
    parts = [capsys.readouterr().out]
    for path in paths:
        with open(path) as f:
            parts.append(f"===== {path} =====\n{f.read()}")
    return "\n".join(parts)


def check_golden(name, text):
    """Compare text with tests/golden/<name>.txt. UPDATE_GOLDEN=1 rewrites the file instead."""
    path = os.path.join(GOLDEN_DIR, f"{name}.txt")
    if os.environ.get("UPDATE_GOLDEN"):
        os.makedirs(GOLDEN_DIR, exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return
    with open(path) as f:
        assert text == f.read(), f"output differs from {path}"
