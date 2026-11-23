"""Replay moltsets_poc.py from the local cache only: any paid MoltSets call stops the run.

    python3 tools/offline_replay.py prospect --companies-file targets.csv --output .replay/x.csv

get_account / get_billing / get_usage are free and still go to MoltSets (the pre-run check needs them).
Every other call must be answered from .moltsets_cache.json; one that isn't raises OfflineMiss instead
of spending search results or tokens. Used to prove a refactor changed no output.
"""

import os
import runpy
import sys

import requests

FREE_TOOLS = {"get_account", "get_billing", "get_usage"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class OfflineMiss(Exception):
    """A call the cache couldn't answer: the replay would have paid for it."""


_post = requests.post


def guarded_post(url, *args, **kwargs):
    tool = url.rstrip("/").rsplit("/", 1)[-1]
    if tool not in FREE_TOOLS:
        raise OfflineMiss(f"cache miss: {tool} {kwargs.get('json')}")
    return _post(url, *args, **kwargs)


requests.post = guarded_post
sys.path.insert(0, ROOT)
sys.argv = [os.path.join(ROOT, "moltsets_poc.py"), *sys.argv[1:]]
runpy.run_path(sys.argv[0], run_name="__main__")
