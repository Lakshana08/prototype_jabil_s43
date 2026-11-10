"""Deterministic intent + entity extraction for planner questions (English / Mandarin).

Entities are matched against the backend's known keys (orders, materials, SLocs, MRP
controllers), so an extracted value is always a real key, never a guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.data.repository import DataRepository

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


def parse(text: str, repo: DataRepository) -> ParsedQuery:
    low = text.lower()
    q = ParsedQuery(text=text)
    q.intents = [name for name, words in INTENT_KEYWORDS.items() if any(w in low for w in words)]
    if any(w in low for w in EXPORT_KEYWORDS):
        q.export_format = "xlsx" if any(w in low for w in EXCEL_KEYWORDS) else "csv"

    for num in ORDER_RE.findall(text):
        target = q.work_orders if num in repo.production_order_by_order else q.unknown_orders
        if num not in target:
            target.append(num)

    known_mats = repo.known_materials
    slocs = repo.known_storage_locations
    mrps = repo.known_mrp_controllers
    types = repo.known_order_types
    for tok in TOKEN_RE.findall(text):
        up = tok.upper()
        if up in known_mats and up not in q.materials:
            q.materials.append(up)
        elif up in slocs and up not in q.storage_locations:
            q.storage_locations.append(up)
        elif tok in mrps and tok not in q.mrp_controllers:  # case-sensitive: avoid matching ordinary words
            q.mrp_controllers.append(tok)
        elif up in types and up not in q.order_types:
            q.order_types.append(up)

    # "machine-head" also contains no other keyword; "over-control" must not double as over-issued
    if "material_status" in q.intents and "over_issued" in q.intents and not _has_any(low, ["over-issue", "over issue", "overissue", "z67", "z68"]):
        q.intents.remove("over_issued")
    return q


def _has_any(low: str, words: list[str]) -> bool:
    return any(w in low for w in words)
