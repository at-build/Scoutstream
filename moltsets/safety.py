"""Everything from outside is data: API replies, input CSVs, web-log fields. Helpers that keep it that way.

Three risks are handled here (see SECURITY.md):
  - text that carries terminal escape codes, or characters people can't see (zero-width, bidi overrides,
    Unicode tags) but a terminal or an AI assistant reading the output can: clean_text / clean;
  - spreadsheet formulas in CSV cells ("=HYPERLINK(...)", "=cmd|..."): safe_cell / write_table;
  - state files and output CSVs that hold personal data being created world-readable: open_private.
"""

import csv
import os
import re

MAX_TEXT = 500  # longest string kept from an API reply; real names, titles, URLs and emails are far shorter

# C0 controls except tab and newline, DEL, C1 controls, zero-width and bidi formatting characters,
# line / paragraph separators, the BOM, and the Unicode tag block (invisible ASCII look-alikes).
_UNSAFE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f­​-‏ -‮⁠-⁯﻿\U000e0000-\U000e007f]")
_LINE_BREAKS = re.compile(r"[\r\n\t]+")
# A cell a spreadsheet would run as a formula. "+" / "-" are let through for plain numbers and phone numbers.
_FORMULA = re.compile(r"^[=@\t\r]|^[+\-](?![\d\s().\-]*$)")


def clean_text(value, limit=MAX_TEXT, keep_newlines=False):
    """A string with control and invisible characters removed; other values unchanged.

    Line breaks and tabs become spaces unless keep_newlines, so a value can't fake a new line of console
    output. A string longer than `limit` is cut and marked with "…" (limit=None: never cut).
    """
    if not isinstance(value, str):
        return value
    if not keep_newlines:
        value = _LINE_BREAKS.sub(" ", value)
    value = _UNSAFE.sub("", value)
    if limit and len(value) > limit:
        value = value[:limit - 1] + "…"
    return value


def clean(value, limit=MAX_TEXT):
    """clean_text applied to every string inside a JSON-like value, dict keys included (some get printed)."""
    if isinstance(value, dict):
        return {clean_text(k, limit): clean(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v, limit) for v in value]
    return clean_text(value, limit)


def safe_cell(value):
    """A CSV cell that Excel / Sheets / Numbers will show as text: a formula gets a leading apostrophe."""
    if isinstance(value, str) and _FORMULA.match(value):
        return "'" + value
    return value


def write_table(path, fieldnames, rows):
    """Write dict rows as a CSV with every header and cell passed through safe_cell.

    Keys missing from a row are written blank; keys not in fieldnames are left out. The file holds contact
    data, so a new one is created readable by the current user only.
    """
    with open_private(path, newline="") as f:
        writer = csv.writer(f)
        writer.writerow([safe_cell(h) for h in fieldnames])
        for row in rows:
            writer.writerow([safe_cell(row.get(name, "")) for name in fieldnames])


def open_private(path, newline=None):
    """Open a file for writing, created readable by the current user only (0600). For files holding contact
    data. A file that already exists keeps the permissions it has."""
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8",
                     newline=newline)
