"""Paging for list answers, so one reply never sends the whole result set to the LLM.

* The server slices the rows (``page_slice``) - the model never picks the page size.
* Every page carries the true total and a ready-made follow-up prompt (``next_page_prompt``)
  that repeats all filters of the original question and ends in ``rows X-Y``.
* The page is read back from that text (``skip_from_text``), so we never rely on the model
  doing the arithmetic.
"""
from __future__ import annotations

import re

PAGE_SIZE = 20

_ROWS_RE = re.compile(r"\brows?\s+(\d+)\s*(?:-|–|to)\s*\d+", re.IGNORECASE)


def skip_from_text(text: str | None) -> int | None:
    """'... rows 21-40' -> 20; None when the text names no row range."""
    m = _ROWS_RE.search(text or "")
    return max(0, int(m.group(1)) - 1) if m else None


def resolve_skip(model_skip, requested_skip: int | None) -> int:
    """The model's non-zero value wins; otherwise the page parsed from the user's text."""
    try:
        value = int(model_skip or 0)
    except (TypeError, ValueError):
        value = 0
    return max(0, value or requested_skip or 0)


def page_slice(rows: list, skip: int, top: int = PAGE_SIZE) -> list:
    return rows[skip:skip + top]


def page_meta(total: int, skip: int, returned: int) -> dict:
    return {"count": total, "skip": skip, "returned": returned, "has_more": skip + returned < total}


def next_page_prompt(subject: str, meta: dict) -> str | None:
    """Follow-up message for the 'Show next 20' button; None on the last page."""
    if not meta.get("has_more"):
        return None
    start = meta["skip"] + meta["returned"] + 1
    end = min(start + PAGE_SIZE - 1, meta["count"])
    return f"Show {subject}, rows {start}-{end}"
