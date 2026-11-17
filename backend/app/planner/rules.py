"""Deterministic business rules (Technical Spec §6).

Every function here is pure: identical inputs give identical outputs, independent of user
or session (NFR "Reproducibility"). The agent layer only routes and narrates; it never
re-derives these results.
"""
from __future__ import annotations

import re

# ------------------------------------------------------------------ §6.3 Aging
def is_aged(aging_days: int | float | None, threshold_days: int) -> bool:
    return aging_days is not None and aging_days > threshold_days


# ------------------------------------------------------------------ §6.2 Over-issued WIP (component level)
# Components the 14C process excludes from issuance control: their remaining-issue figure is noise.
UNCONTROLLED_EXCLUSIONS = {"Bulk material", "Dummy part (-S)"}


def over_issued_qty(component: dict) -> float:
    """Quantity issued beyond the theoretical BOM requirement (S8 remaining < 0), else 0."""
    if component.get("ExcludeReason") in UNCONTROLLED_EXCLUSIONS:
        return 0.0
    remaining = component.get("RemainingIssueQty") or 0
    return round(-remaining, 3) if remaining < 0 else 0.0


def z67_z68_adjustment(component: dict) -> dict | None:
    """Net GI (Other) carries the Z67/Z68 adjust-in / adjust-out postings from the 14C report."""
    qty = component.get("NetGI_Other") or 0
    if not qty:
        return None
    return {
        "quantity": qty,
        "direction": "adjust-in (issued to order)" if qty > 0 else "adjust-out (returned from order)",
    }


def classify_over_issue(component: dict) -> str | None:
    """Clarification Q4: WIP > order requirement is 'over-control (reasonable)' when explained by a
    Z67/Z68 adjust-in, otherwise plain 'over-issued'."""
    if over_issued_qty(component) <= 0:
        return None
    adj = z67_z68_adjustment(component)
    if adj and adj["quantity"] > 0:
        return "Over-control (reasonable - Z67/Z68 adjustment)"
    return "Over-issued"


# ------------------------------------------------------------------ §6.1 / §6.2 Material status (SLoc level)
STATUS_NON_CONTROLLED = "Non-controlled"
STATUS_AVAILABLE = "Available to issue"
STATUS_OVER_CONTROL = "Over-control / over-issued"


def material_status(unrestricted: float, controlled_remaining: float | None) -> tuple[str, float | None]:
    """Returns (status, available_to_issue_qty).

    controlled_remaining is None when the SLoc|Material key has no match in the open,
    controlled production-order requirement set.
    """
    if controlled_remaining is None:
        return STATUS_NON_CONTROLLED, None
    available = round(unrestricted - controlled_remaining, 3)
    return (STATUS_AVAILABLE if available >= 0 else STATUS_OVER_CONTROL), available


# ------------------------------------------------------------------ §6.4 Machine-head material (review only)
# Raw material that fills mould / feed lines (resin, paint, ink) - not semi-finished "moulded" parts.
_MOULDING_PATTERN = re.compile(r"resin|pellet|masterbatch|paint|ink|thinner|hardener", re.I)


def needs_machine_head_review(order_type: str, component: dict) -> bool:
    """Candidate surfacing only - never a resolved status."""
    if "machine-head" in (component.get("ExcludeReason") or ""):
        return True
    return (
        order_type == "ZNPC"
        and bool(_MOULDING_PATTERN.search(component.get("ProductDescription") or ""))
        and (component.get("ActualUsage") or 0) > 0
    )


# ------------------------------------------------------------------ Data quality (NFR anomaly handling)
def data_quality_issues(component: dict) -> list[str]:
    issues = []
    if not component.get("StorageLocation"):
        issues.append("Blank storage location - maintain SLoc on the order (CO02/CO03)")
    return issues


# ------------------------------------------------------------------ Root-cause narration (FR-11)
def shortage_root_cause(component: dict) -> str:
    status = component.get("ShortageStatus", "")
    unit = component.get("BaseUnit", "")
    open_qty = fmt(component.get("OpenDemandQty"))
    alloc = fmt(component.get("AllocatedQty"))
    short = fmt(component.get("ShortageQty"))
    wh = fmt(component.get("WarehouseStock"))
    if status.startswith("Short - warehouse insufficient"):
        return (f"Open demand {open_qty} {unit}; line-side covers {alloc}, short {short}. "
                f"Warehouse holds only {wh} - expedite inbound / supplier delivery.")
    if status.startswith("Short at line"):
        return (f"Open demand {open_qty} {unit}; line-side covers {alloc}, short {short}. "
                f"Warehouse has {wh} - replenish line-side via pull list (ZPL).")
    if status.startswith("Covered"):
        return f"Open demand {open_qty} {unit} fully covered by line-side allocation ({alloc})."
    reason = component.get("ExcludeReason")
    if reason:
        return f"Excluded from shortage control: {reason}."
    return "No open demand - theoretical requirement already issued."


def is_short(component: dict) -> bool:
    return (component.get("ShortageQty") or 0) > 0


def fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.3f}".rstrip("0").rstrip(".")
    return f"{value:,}"
