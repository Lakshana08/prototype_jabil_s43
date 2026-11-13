"""Deterministic intent + entity extraction for planner questions (English / Mandarin).

Candidate keys in the text are checked against S43 (one small look-up each), so an extracted
value is always a real key, never a guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.data.repository import LiveSource

INTENT_KEYWORDS: dict[str, list[str]] = {
    "shortage": ["short", "shortage", "missing", "lack"],
    "aged": ["aged", "aging", "ageing", "old stock", "slow moving"],
    "over_issued": ["over-issue", "over issue", "overissue", "over-issued", "over issued", "excess issue",
                    "z67", "z68"],
    "material_status": ["over-control", "over control", "overcontrol", "non-controlled", "non controlled",
                        "uncontrolled", "issuable", "available to issue", "control status"],
    "production_order": ["production order", "order status", "order detail", "work order detail", "operations",
                         "confirmation", "progress"],
    "material": ["material master", "mrp type", "procurement", "lead time", "where is", "how much",
                 "inventory", "stock", "available"],
    "machine_head": ["machine-head", "machine head", "machinehead"],
    "help": ["help", "what can you do", "capabilit"],
}
EXPORT_KEYWORDS = ["csv", "export", "download", "excel", "xlsx", "spreadsheet"]
EXCEL_KEYWORDS = ["excel", "xlsx", "spreadsheet"]

ORDER_RE = re.compile(r"(?<!\d)(\d{9})(?!\d)")
_NOT_KEYS = {"csv", "wo", "sku", "xlsx", "ok", "mrp", "bom", "gr", "gi", "aged", "all"}
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]*[A-Za-z0-9]|[A-Za-z0-9]")


@dataclass
class ParsedQuery:
    text: str
    intents: list[str] = field(default_factory=list)
    work_orders: list[str] = field(default_factory=list)
    unknown_orders: list[str] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)
    storage_locations: list[str] = field(default_factory=list)
    mrp_controllers: list[str] = field(default_factory=list)
    order_types: list[str] = field(default_factory=list)
    export_format: str = ""  # "" (no file), "csv" or "xlsx"

    def as_dict(self) -> dict:
        return {k: v for k, v in vars(self).items() if k != "text" and v}


MAX_KEY_CANDIDATES = 8  # each candidate costs up to a few S43 look-ups


def parse(text: str, source: LiveSource) -> ParsedQuery:
    low = text.lower()
    q = ParsedQuery(text=text)
    q.intents = [name for name, words in INTENT_KEYWORDS.items() if any(w in low for w in words)]
    if any(w in low for w in EXPORT_KEYWORDS):
        q.export_format = "xlsx" if any(w in low for w in EXCEL_KEYWORDS) else "csv"

    for num in ORDER_RE.findall(text):
        target = q.work_orders if source.find_order(num) else q.unknown_orders
        if num not in target:
            target.append(num)

    # Only tokens that look like SAP keys are looked up: they contain a digit (material, location)
    # or are an upper-case code (MRP controller, order type). Ordinary words are never sent to S43.
    candidates = []
    for tok in TOKEN_RE.findall(text):
        if ORDER_RE.fullmatch(tok) or tok.lower() in _NOT_KEYS or tok in candidates:
            continue
        if any(ch.isdigit() for ch in tok) or (tok.isupper() and 2 <= len(tok) <= 6):
            candidates.append(tok)
    for tok in candidates[:MAX_KEY_CANDIDATES]:
        up = tok.upper()
        if (mat := source.find_material(tok)) and mat not in q.materials:
            q.materials.append(mat)
        elif (sl := source.find_storage_location(tok)) and sl not in q.storage_locations:
            q.storage_locations.append(sl)
        elif tok.isupper() and (mrp := source.find_mrp_controller(tok)) and mrp not in q.mrp_controllers:
            q.mrp_controllers.append(mrp)  # case-sensitive: avoid matching ordinary words
        elif tok.isupper() and (ot := source.find_order_type(up)) and ot not in q.order_types:
            q.order_types.append(ot)

    # "machine-head" also contains no other keyword; "over-control" must not double as over-issued
    if "material_status" in q.intents and "over_issued" in q.intents and not _has_any(low, ["over-issue", "over issue", "overissue", "z67", "z68"]):
        q.intents.remove("over_issued")
    return q


def _has_any(low: str, words: list[str]) -> bool:
    return any(w in low for w in words)
