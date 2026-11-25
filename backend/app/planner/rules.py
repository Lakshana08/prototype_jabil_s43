"""Business rules (Technical Spec §6) - the single place where the planner's results are decided.

Every function is pure: the same input always gives the same output (NFR "Reproducibility").
The rest of the app only loads data (app.data), applies these rules (service.py) and words the
answer (LLM / templates) - it never re-derives a result.

    Spec  Rule                              Function                     Input fields
    ----  --------------------------------  ---------------------------  ---------------------------------------
    6.1   Non-controlled material           material_status()            Unrestricted stock, ControlledRemaining
    6.2   Over-control / available          material_status()              (lookup by SLoc + Material, Z_AGEDINV
          ... for every stock position      classify_positions()            sheet 2 SumRemainingQty)
          Z67/Z68 context (supplementary)   z67_z68_adjustment()         Net GI (Other)
    6.3   Aging (> 14 days, fixed)          is_aged()                    AgingDays
    6.4   Machine-head (review flag only)   needs_machine_head_review()  OrderType, description, ActualUsage
    6.5   Bulk / "-S" exclusions            NOT implemented (SME open questions #1 and #2)

    Supporting rules on work-order components (shortage view):
          Shortage + root cause (FR-11)     is_short(), shortage_root_cause()
          Over-issued component             over_issued_qty(), classify_over_issue()
          Data quality                      data_quality_issues()
"""
from __future__ import annotations

import re
from typing import Iterable

# ====================================================================================================
# 6.1 / 6.2  Material status per storage location + material
# ====================================================================================================
#
#   ControlledRemainingIssuance = LOOKUP(SLoc + Material) in Z_AGEDINV sheet 2     (NO MATCH -> NULL)
#   AvailableToIssueQty         = UnrestrictedStock - ControlledRemainingIssuance
#
#   CASE
#     WHEN ControlledRemainingIssuance IS NULL   THEN '无管制料'  Non-controlled
#     WHEN AvailableToIssueQty >= 0              THEN '可发量'    Available to issue
#     ELSE                                            '领超管制'  Over-control / over-issued
#   END

STATUS_NON_CONTROLLED = "Non-controlled"            # 无管制料
STATUS_AVAILABLE = "Available to issue"             # 可发量
STATUS_OVER_CONTROL = "Over-control / over-issued"  # 领超管制

STATUS_CODE = {STATUS_AVAILABLE: "AV", STATUS_OVER_CONTROL: "OC", STATUS_NON_CONTROLLED: "NC"}  # S43 MaterialStatus
STATUS_CN = {STATUS_AVAILABLE: "可发量", STATUS_OVER_CONTROL: "领超管制", STATUS_NON_CONTROLLED: "无管制料"}


def lookup_key(storage_location: str, material: str) -> str:
    """The lookup key, written like the SME's Excel key column: SLoc & Material (e.g. 'A201ACPE21670')."""
    return f"{storage_location}{material}".upper()


def controlled_remaining_lookup(wo_component_rows: Iterable[dict]) -> dict[str, float]:
    """Build the lookup table: key -> ControlledRemainingIssuance.

    Source: Z_AGEDINV sheet 2 (Sheet2_WOComponent). Its SumRemainingQty is already
    SUMIF(ActualRemainingQty) over the key, so one value per key is taken.
    A key that is missing from this table is a NO MATCH (-> non-controlled)."""
    return {lookup_key(r["StorageLocation"], r["Component"]): r.get("SumRemainingQty") or 0
            for r in wo_component_rows if r.get("StorageLocation") and r.get("Component")}


def material_status(unrestricted: float, controlled_remaining: float | None) -> tuple[str, float | None]:
    """The CASE above for ONE position. Returns (status, AvailableToIssueQty).

    controlled_remaining is None when the lookup found NO MATCH."""
    if controlled_remaining is None:
        return STATUS_NON_CONTROLLED, None
    available = round(unrestricted - controlled_remaining, 3)
    return (STATUS_AVAILABLE if available >= 0 else STATUS_OVER_CONTROL), available


def classify_positions(stock_rows: Iterable[dict], lookup: dict[str, float],
                       net_gi_other: dict[str, float] | None = None) -> list[dict]:
    """Apply 6.1 / 6.2 to every storage location + material position.

    1. Add up the unrestricted stock per key (several stock rows can share a key).
    2. Look up the controlled remaining issuance for the key (missing -> NULL).
    3. material_status() decides the status and the available-to-issue quantity.
    4. Over-control positions get the Z67/Z68 postings as supplementary context.
    """
    positions: dict[str, dict] = {}
    for s in stock_rows:                                                     # step 1
        key = lookup_key(s["StorageLocation"], s["Material"])
        p = positions.setdefault(key, {
            "Material": s["Material"], "Description": s.get("ProductDescription"),
            "StorageLocation": s["StorageLocation"], "BaseUnit": s.get("BaseUnit"),
            "Unrestricted": 0.0, "AgingDays": s.get("AgingDays")})
        p["Unrestricted"] = round(p["Unrestricted"] + (s.get("Unrestricted") or 0), 3)
        p["AgingDays"] = max(p["AgingDays"] or 0, s.get("AgingDays") or 0)

    out = []
    for key, p in positions.items():
        controlled = lookup.get(key)                                         # step 2
        status, available = material_status(p["Unrestricted"], controlled)   # step 3
        adj = (z67_z68_adjustment((net_gi_other or {}).get(key))             # step 4
               if status == STATUS_OVER_CONTROL else None)
        out.append({**p, "Key": key,
                    "ControlledRemainingIssueQty": round(controlled, 3) if controlled is not None else None,
                    "AvailableToIssueQty": available,
                    "Status": status, "StatusCode": STATUS_CODE[status], "StatusCN": STATUS_CN[status],
                    "Z67Z68Adjustment": adj["quantity"] if adj else None,
                    "Z67Z68Context": adj["text"] if adj else None,
                    "IsAged": is_aged(p["AgingDays"])})
    return out


def z67_z68_adjustment(net_gi_other: float | None) -> dict | None:
    """6.2 supplementary field - never part of the status formula.

    Net GI (Other) holds the Z67 (adjust-in, issued to the order) / Z68 (adjust-out) postings.
    An adjust-in can explain an over-control / over-issue as a reasonable adjustment."""
    qty = net_gi_other or 0
    if not qty:
        return None
    if qty > 0:
        return {"quantity": qty, "direction": "adjust-in (issued to order)",
                "text": f"Z67/Z68 adjust-in {fmt(qty)} - possibly a reasonable adjustment"}
    return {"quantity": qty, "direction": "adjust-out (returned from order)",
            "text": f"Z67/Z68 adjust-out {fmt(-qty)}"}


# ====================================================================================================
# 6.3  Aging:  IsAged = AgingDays > 14
# ====================================================================================================
# Fixed global threshold - not configurable per site / material / environment without a change request.
AGING_THRESHOLD_DAYS = 14


def is_aged(aging_days: int | float | None, threshold_days: int = AGING_THRESHOLD_DAYS) -> bool:
    return aging_days is not None and aging_days > threshold_days


# ====================================================================================================
# 6.4  Machine-head material - FLAG FOR REVIEW only, never a resolved status
# ====================================================================================================
#   FLAG_FOR_REVIEW = order_type = 'ZNPC'
#                     AND description matches the moulding / painting pattern (heuristic)
#                     AND goods were already issued to the order (ActualUsage > 0)

# Raw material that fills mould / feed lines (resin, paint, ink) - not semi-finished "moulded" parts.
_MOULDING_PATTERN = re.compile(r"resin|pellet|masterbatch|paint|ink|thinner|hardener", re.I)


def needs_machine_head_review(order_type: str, component: dict) -> bool:
    return (order_type == "ZNPC"
            and bool(_MOULDING_PATTERN.search(component.get("ProductDescription") or ""))
            and (component.get("ActualUsage") or 0) > 0)


# ====================================================================================================
# 6.5  NOT implemented until the SME confirms them (Functional Spec open questions #1 and #2):
#      * "Bulk material" exclusion
#      * "-S" suffix material handling
#      Every material / component below is therefore evaluated the same way.
# ====================================================================================================


# ====================================================================================================
# Work-order component rules (shortage view)
# ====================================================================================================
def is_short(component: dict) -> bool:
    """Still missing after line-side stock is allocated (ShortageQty is calculated in S43)."""
    return (component.get("ShortageQty") or 0) > 0


def shortage_root_cause(component: dict) -> str:
    """FR-11: explain the S43 ShortageStatus and the action it implies."""
    status = component.get("ShortageStatus", "")
    unit = component.get("BaseUnit", "")
    open_qty, alloc = fmt(component.get("OpenDemandQty")), fmt(component.get("AllocatedQty"))
    short, wh = fmt(component.get("ShortageQty")), fmt(component.get("WarehouseStock"))
    if status.startswith("Short - warehouse insufficient"):
        return (f"Open demand {open_qty} {unit}; line-side covers {alloc}, short {short}. "
                f"Warehouse holds only {wh} - expedite inbound / supplier delivery.")
    if status.startswith("Short at line"):
        return (f"Open demand {open_qty} {unit}; line-side covers {alloc}, short {short}. "
                f"Warehouse has {wh} - replenish line-side via pull list (ZPL).")
    if status.startswith("Covered"):
        return f"Open demand {open_qty} {unit} fully covered by line-side allocation ({alloc})."
    if component.get("ExcludeReason"):
        return f"Excluded from shortage control: {component['ExcludeReason']}."
    return "No open demand - theoretical requirement already issued."


def over_issued_qty(component: dict) -> float:
    """Issued beyond the theoretical BOM requirement: RemainingIssueQty < 0 -> its absolute value, else 0."""
    remaining = component.get("RemainingIssueQty") or 0
    return round(-remaining, 3) if remaining < 0 else 0.0


def classify_over_issue(component: dict) -> str | None:
    """Clarification Q4: an over-issue explained by a Z67/Z68 adjust-in is 'reasonable', otherwise plain."""
    if over_issued_qty(component) <= 0:
        return None
    adj = z67_z68_adjustment(component.get("NetGI_Other"))
    if adj and adj["quantity"] > 0:
        return "Over-control (reasonable - Z67/Z68 adjustment)"
    return "Over-issued"


def data_quality_issues(component: dict) -> list[str]:
    """NFR anomaly handling: data problems the planner must fix in SAP."""
    issues = []
    if not component.get("StorageLocation"):
        issues.append("Blank storage location - maintain SLoc on the order (CO02/CO03)")
    return issues


# ====================================================================================================
# Formatting helper (shared by the answer texts)
# ====================================================================================================
def fmt(value) -> str:
    """1234.5 -> '1,234.5', 2.0 -> '2', None -> '-'."""
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.3f}".rstrip("0").rstrip(".")
    return f"{value:,}"
