"""Planner skills on the Excel-based S43 reports (SMC_OData_V4_URLs.xlsx).

These entity sets mirror the SME's Excel files one to one, as flat rows:

* ``Sheet1_Data``            Z_AGEDINV4 - stock per plant / storage location / material with aging
* ``Sheet2_WOComponent``     Z_AGEDINV4 sheet 2 - work-order components with the remaining quantity to issue
* ``Sheet3_WIPIssuable``     Z_AGEDINV4 - WIP issuable quantity per storage location + material
  (``Sheet4_OverControl`` / ``Sheet5_PutawayPending`` / ``Sheet6_SuffixS`` are its filtered views)
* ``COOIS_OrderStatus``      COOIS - user status of work orders (NMVT = excluded)
* ``MOD14C_Data``            PP-GP-014C-NSU - component issuance per work order (SME formulas)
* ``MOD14C_Sheet1``          PP-GP-014C-NSU - need / stock / issuable per storage location + material
* ``PP134_OrderMaster``      PP-1341_1 - order master: type, issuance location, material, MRP controller, qty, dates

The quantities are calculated in S43 (CDS views); these skills only filter, total and explain them.
Every call reads only the rows the question needs (server-side ``$filter``); nothing is cached.
"""
from __future__ import annotations

from collections import Counter

from app.data.odata import ODataError, literal
from app.data.repository import DataLoadError, LiveSource
from app.planner.rules import fmt
from app.planner.service import Section, _d, _md_table, _plural

# view -> (repository key, title, plain-language meaning)
ISSUABLE_VIEWS = {
    "all": ("issuable_all", "WIP issuable stock", "stock per storage location and material vs. the quantity still to issue"),
    "over_control": ("issuable_over_control", "Over-control stock",
                     "positions where more has to be issued than there is stock (over-control / over-issued)"),
    "putaway_pending": ("issuable_putaway_pending", "Stock waiting for put-away",
                        "stock in put-away (receiving) locations that has not been put away yet"),
    "suffix_s": ("issuable_suffix_s", "'-S' material stock", "stock of materials whose number ends in '-S'"),
}


def clean_keys(values) -> list[str]:
    """User-supplied keys as S43 spells them (upper case). Anything that is not a plain key is dropped."""
    out: list[str] = []
    for v in [values] if isinstance(values, (str, int)) else (values or []):
        text = str(v).strip().upper()
        if not text:
            continue
        try:
            literal(text)
        except ODataError:
            continue
        if text not in out:
            out.append(text)
    return out


class ReportService:
    def __init__(self, source: LiveSource, aging_threshold_days: int):
        self.source = source
        self.aging_threshold = aging_threshold_days

    # ================================================================ stock list (Sheet1_Data)
    def stock_list(self, materials=(), slocs=(), min_aging_days: int | None = None) -> Section:
        extra = f"AgingDays ge {int(min_aging_days)}" if min_aging_days else None
        recs = self.source.report_rows("stock_list", {"Material": materials, "StorageLocation": slocs}, extra)
        rows = [{
            "Material": r["Material"], "Description": r.get("MaterialDescription"),
            "Plant": r.get("Plant"), "StorageLocation": r["StorageLocation"], "StorageBin": r.get("StorageBin") or None,
            "MaterialType": r.get("MaterialType"), "MaterialGroup": r.get("MaterialGroup"), "ABC": r.get("ABCIndicator"),
            "AgingDays": r.get("AgingDays"), "LastPlacementDate": _d(r.get("LastPlacementDate")),
            "Unrestricted": r.get("Unrestricted"), "UnrestrictedConsignment": r.get("UnrestrictedConsignment"),
            "Blocked": r.get("Blocked"), "QualityInspection": r.get("QualityInspection"),
            "StockInTransfer": r.get("StockInTransfer"), "Returns": r.get("Returns"),
            "BaseUnit": r.get("BaseUnitOfMeasure"), "StandardPricePerUnit": r.get("StandardPricePerUnit"),
            "StockValue": r.get("UnRestrictStkVal"), "Currency": r.get("Currency"),
        } for r in recs]
        rows.sort(key=lambda r: (-(r["AgingDays"] or 0), r["Material"]))
        value = round(sum(r["StockValue"] or 0 for r in rows), 2)
        currency = next((r["Currency"] for r in rows if r["Currency"]), "")
        aged = [r for r in rows if (r["AgingDays"] or 0) > self.aging_threshold]
        scope = _scope(materials=materials, slocs=slocs, min_aging=min_aging_days)
        if rows:
            headline = (f"There {'is' if len(rows) == 1 else 'are'} {_plural(len(rows), 'stock position')}{scope}, "
                        f"worth {value:,.0f} {currency} (unrestricted stock value). "
                        f"{_plural(len(aged), 'position')} {'has' if len(aged) == 1 else 'have'} not been used "
                        f"for more than {self.aging_threshold} days; the oldest is {rows[0]['Description']} "
                        f"({rows[0]['Material']}) in {rows[0]['StorageLocation']} at {rows[0]['AgingDays']} days.")
        else:
            headline = f"No stock found{scope}."
        lines = [f"**{len(rows)} stock position(s)**{scope} - value {value:,.2f} {currency}", ""]
        lines += _md_table(["Material", "Description", "SLoc", "Aging days", "Unrestricted", "Consignment", "Value"],
                           [[r["Material"], r["Description"], r["StorageLocation"], r["AgingDays"],
                             f"{fmt(r['Unrestricted'])} {r['BaseUnit']}", fmt(r["UnrestrictedConsignment"]),
                             fmt(r["StockValue"])] for r in rows])
        return Section("stock_list", "Stock list (aged inventory export)", "\n".join(lines),
                       data={"filters": _filters(materials=materials, slocs=slocs, min_aging=min_aging_days),
                             "positions": len(rows), "totalValue": value, "currency": currency,
                             "agedPositions": len(aged), "agingThresholdDays": self.aging_threshold},
                       rows=rows, headline=headline,
                       actions=[f"{len(aged)} position(s) older than {self.aging_threshold} days - use first or "
                                "review for return/scrap."] if aged else [],
                       severity="warning" if aged else "info" if rows else "ok")

    # ================================================================ WIP issuable (Sheet3..6)
    def issuable_stock(self, view: str = "all", materials=(), slocs=(), statuses=()) -> Section:
        key, title, meaning = ISSUABLE_VIEWS.get(view, ISSUABLE_VIEWS["all"])
        recs = self.source.report_rows(key, {"Material": materials, "StorageLocation": slocs,
                                             "MaterialStatus": statuses})
        rows = [{
            "StorageLocation": r["StorageLocation"], "Material": r["Material"],
            "Description": r.get("MaterialDescription"), "AgingDays": r.get("AgingDays"),
            "StockQty": r.get("SumUnrestricted"), "StillToIssue": r.get("ControlledRemainingQty"),
            "AvailableToIssue": r.get("AvailableToIssueQty"), "BaseUnit": r.get("BaseUnitOfMeasure"),
            "StatusCode": r.get("MaterialStatus"), "Status": r.get("MaterialStatusText"), "Remark": r.get("Remark"),
        } for r in recs]
        # Most urgent first: the biggest deficit (negative availability), then the oldest stock.
        rows.sort(key=lambda r: ((r["AvailableToIssue"] or 0), -(r["AgingDays"] or 0)))
        by_status = Counter(r["Status"] or r["StatusCode"] or "-" for r in rows)
        short = [r for r in rows if (r["AvailableToIssue"] or 0) < 0]
        scope = _scope(materials=materials, slocs=slocs, statuses=statuses)
        if rows:
            split = ", ".join(f"{n} {s.lower()}" for s, n in by_status.most_common())
            headline = f"{_plural(len(rows), 'position')}{scope} in the {title.lower()} list ({split})."
            if short:
                worst = short[0]
                headline += (f" {_plural(len(short), 'position')} need more than the stock on hand; the largest gap is "
                             f"{worst['Description']} ({worst['Material']}) in {worst['StorageLocation']}: "
                             f"{fmt(-worst['AvailableToIssue'])} {worst['BaseUnit']} short.")
        else:
            headline = f"No positions{scope} in the {title.lower()} list."
        lines = [f"**{len(rows)} position(s)** - {meaning}{scope}", ""]
        lines += _md_table(["SLoc", "Material", "Description", "Stock", "Still to issue", "Available", "Status"],
                           [[r["StorageLocation"], r["Material"], r["Description"],
                             f"{fmt(r['StockQty'])} {r['BaseUnit']}", fmt(r["StillToIssue"]),
                             fmt(r["AvailableToIssue"]), r["Status"]] for r in rows])
        return Section("issuable_stock", title, "\n".join(lines),
                       data={"view": view, "meaning": meaning,
                             "filters": _filters(materials=materials, slocs=slocs, statuses=statuses),
                             "positions": len(rows), "byStatus": dict(by_status), "positionsShort": len(short)},
                       rows=rows, headline=headline,
                       actions=[f"{len(short)} position(s) have less stock than still has to be issued."] if short else [],
                       severity="warning" if short else "info" if rows else "ok")

    # ================================================================ WO components (Sheet2_WOComponent)
    def wo_component_remaining(self, orders=(), components=(), slocs=(), mrp=(), order_types=()) -> Section:
        recs = self.source.report_rows("wo_component", {
            "OrderNumber": orders, "Component": components, "StorageLocation": slocs,
            "MRPController": mrp, "OrderType": order_types})
        rows = [{
            "WorkOrder": r["OrderNumber"], "OrderType": r.get("OrderType"), "MRPController": r.get("MRPController"),
            "HeaderMaterial": r.get("Material"), "HeaderDescription": r.get("MaterialDescription"),
            "OrderQty": r.get("TotalOrderQty"), "ConfirmedYield": r.get("ConfirmedYield"),
            "Component": r["Component"], "StorageLocation": r["StorageLocation"], "Unit": r.get("Unit"),
            "QtyPer": r.get("QtyPer"), "ScrapPct": r.get("ComponentScrapPct"),
            "BOMTheoreticalQty": r.get("BOMTheoreticalQty"), "ActualUsage": r.get("ActualUsage"),
            "RemainingQty": r.get("RemainingQty"), "ActualRemainingQty": r.get("ActualRemainingQty"),
            "SumRemainingForSLocMaterial": r.get("SumRemainingQty"),
        } for r in recs]
        rows.sort(key=lambda r: (-(r["ActualRemainingQty"] or 0), r["WorkOrder"], r["Component"]))
        open_rows = [r for r in rows if (r["ActualRemainingQty"] or 0) > 0]
        n_orders = len({r["WorkOrder"] for r in rows})
        scope = _scope(orders=orders, components=components, slocs=slocs, mrp=mrp, order_types=order_types)
        if rows:
            headline = (f"{_plural(len(rows), 'component line')} on {_plural(n_orders, 'work order')}{scope}; "
                        f"{_plural(len(open_rows), 'line')} still {'has' if len(open_rows) == 1 else 'have'} "
                        "quantity left to issue.")
        else:
            headline = f"No work-order component lines found{scope}."
        lines = [f"**{len(rows)} component line(s)** on {n_orders} work order(s){scope}", ""]
        lines += _md_table(["Work order", "Component", "SLoc", "BOM qty", "Used", "Remaining", "Sum per SLoc+material"],
                           [[r["WorkOrder"], r["Component"], r["StorageLocation"], fmt(r["BOMTheoreticalQty"]),
                             fmt(r["ActualUsage"]), f"{fmt(r['ActualRemainingQty'])} {r['Unit']}",
                             fmt(r["SumRemainingForSLocMaterial"])] for r in rows])
        return Section("wo_component_remaining", "Work-order components - remaining to issue", "\n".join(lines),
                       data={"filters": _filters(orders=orders, components=components, slocs=slocs, mrp=mrp,
                                                 order_types=order_types),
                             "lines": len(rows), "workOrders": n_orders, "linesWithRemaining": len(open_rows)},
                       rows=rows, headline=headline, severity="info" if rows else "ok")

    # ================================================================ 14C issuance (MOD14C_Data / MOD14C_Sheet1)
    def component_issuance_14c(self, orders=(), components=(), slocs=(), mrp=(), order_types=()) -> Section:
        recs = self.source.report_rows("issuance_14c", {
            "OrderNumber": orders, "Component": components, "StorageLocation": slocs,
            "MRPController": mrp, "OrderType": order_types})
        rows = [{
            "WorkOrder": r["OrderNumber"], "OrderType": r.get("OrderType"), "MRPController": r.get("MRPController"),
            "HeaderMaterial": r.get("Material"), "HeaderDescription": r.get("MaterialDescription"),
            "OrderQty": r.get("TotalOrderQty"), "ConfirmedYield": r.get("ConfirmedYield"),
            "ConfirmedScrap": r.get("ConfirmedScrap"), "Component": r["Component"],
            "StorageLocation": r["StorageLocation"], "Unit": r.get("Unit"), "QtyPer": r.get("QtyPer"),
            "ScrapPct": r.get("ComponentScrapPct"), "TheoUsageNoScrap": r.get("TheoUsageNoScrap"),
            "TheoUsageWithScrap": r.get("TheoUsageWithScrap"), "IssuedCO11N": r.get("NetGICO11N"),
            "IssuedOther": r.get("NetGIOther"), "ActualUsage": r.get("ActualUsage"),
            "BOMTheoreticalQty": r.get("SMEBOMTheoreticalQty"), "RemainingQty": r.get("SMERemainingQty"),
            "HasSMEFormula": "yes" if r.get("HasSMEFormula") == "X" else "no",
        } for r in recs]
        rows.sort(key=lambda r: (-(r["RemainingQty"] or 0), r["WorkOrder"], r["Component"]))
        over = [r for r in rows if (r["RemainingQty"] or 0) < 0]
        n_orders = len({r["WorkOrder"] for r in rows})
        scope = _scope(orders=orders, components=components, slocs=slocs, mrp=mrp, order_types=order_types)
        if rows:
            headline = (f"The 14C report has {_plural(len(rows), 'component line')} on "
                        f"{_plural(n_orders, 'work order')}{scope}.")
            if over:
                headline += (f" {_plural(len(over), 'line')} {'has' if len(over) == 1 else 'have'} more issued than "
                             "the BOM requires (negative remaining quantity).")
        else:
            headline = f"No 14C component issuance lines found{scope}."
        lines = [f"**{len(rows)} 14C line(s)** on {n_orders} work order(s){scope}", ""]
        lines += _md_table(["Work order", "Component", "SLoc", "BOM qty", "Issued CO11N", "Issued other", "Remaining"],
                           [[r["WorkOrder"], r["Component"], r["StorageLocation"], fmt(r["BOMTheoreticalQty"]),
                             fmt(r["IssuedCO11N"]), fmt(r["IssuedOther"]), f"{fmt(r['RemainingQty'])} {r['Unit']}"]
                            for r in rows])
        return Section("component_issuance_14c", "14C component issuance", "\n".join(lines),
                       data={"filters": _filters(orders=orders, components=components, slocs=slocs, mrp=mrp,
                                                 order_types=order_types),
                             "lines": len(rows), "workOrders": n_orders, "linesOverIssued": len(over)},
                       rows=rows, headline=headline, severity="warning" if over else "info" if rows else "ok")

    def issuance_summary_14c(self, materials=(), slocs=()) -> Section:
        """Need vs. stock vs. issuable per storage location + material (RowLabel = SLoc + material)."""
        recs = self.source.report_rows("issuance_14c_summary", extra=_label_filter(slocs, materials))
        rows = []
        for r in recs:
            label = (r["RowLabel"] or "").strip()
            if not label:
                continue  # total / blank line of the Excel sheet
            sloc, mat = (label[:4], label[4:]) if len(label) > 4 else ("", label)
            if slocs and sloc not in slocs or materials and mat not in materials:
                continue  # the server filter is a prefix/suffix match - keep exact hits only
            rows.append({
                "StorageLocation": sloc, "Material": mat,
                "NeedQty": None if r.get("NeedIsBlank") == "X" else r.get("NeedQty"),
                "StockQty": None if r.get("StockIsNA") == "X" else r.get("StockQty"),
                "IssuableQty": None if r.get("IssuableIsNA") == "X" else r.get("IssuableQty"),
            })
        # IssuableQty = need - stock: > 0 still has to be issued, < 0 more stock than the orders need.
        rows.sort(key=lambda r: (-(r["IssuableQty"] or 0), r["StorageLocation"], r["Material"]))
        to_issue = [r for r in rows if (r["IssuableQty"] or 0) > 0]
        surplus = [r for r in rows if (r["IssuableQty"] or 0) < 0]
        no_stock = [r for r in rows if (r["NeedQty"] or 0) > 0 and not r["StockQty"]]
        scope = _scope(materials=materials, slocs=slocs)
        if rows:
            headline = (f"The 14C summary has {_plural(len(rows), 'storage location / material position')}{scope}: "
                        f"{len(to_issue)} still need material issued (need above stock, {len(no_stock)} of them with "
                        f"no stock at all) and {len(surplus)} hold more stock than the open orders need.")
        else:
            headline = f"No 14C summary positions found{scope}."
        lines = [f"**{len(rows)} position(s)**{scope}", ""]
        lines += _md_table(["SLoc", "Material", "Need", "Stock", "Issuable"],
                           [[r["StorageLocation"], r["Material"], fmt(r["NeedQty"]), fmt(r["StockQty"]),
                             fmt(r["IssuableQty"])] for r in rows])
        return Section("issuance_summary_14c", "14C need vs. stock vs. issuable", "\n".join(lines),
                       data={"filters": _filters(materials=materials, slocs=slocs), "positions": len(rows),
                             "positionsToIssue": len(to_issue), "positionsWithoutStock": len(no_stock),
                             "positionsWithSurplus": len(surplus),
                             "meaning": "IssuableQty = NeedQty - StockQty; negative = more stock than needed"},
                       rows=rows, headline=headline, severity="warning" if no_stock else "info" if rows else "ok")

    # ================================================================ COOIS user status
    def order_user_status(self, orders=(), excluded_only: bool = False) -> Section:
        recs = self.source.report_rows("order_user_status", {"OrderNumber": orders},
                                       "IsExcluded eq 'X'" if excluded_only else None)
        rows = [{"WorkOrder": r["OrderNumber"], "UserStatus": r.get("UserStatus"),
                 "Excluded": "yes" if r.get("IsExcluded") == "X" else "no"} for r in recs]
        rows.sort(key=lambda r: r["WorkOrder"])
        excluded = [r for r in rows if r["Excluded"] == "yes"]
        missing = sorted(set(orders) - {r["WorkOrder"] for r in rows})
        scope = _scope(orders=orders)
        if rows:
            statuses = Counter(r["UserStatus"] or "-" for r in rows)
            headline = (f"{_plural(len(rows), 'work order')}{scope} {'has' if len(rows) == 1 else 'have'} a user status in "
                        f"COOIS ({', '.join(f'{n}x {s}' for s, n in statuses.most_common())}); "
                        f"{_plural(len(excluded), 'order')} {'is' if len(excluded) == 1 else 'are'} excluded from the "
                        "material checks (NMVT = no material movement).")
        else:
            headline = f"No COOIS user status found{scope}."
        if missing:
            headline += f" No user status recorded for {', '.join(missing)}."
        lines = [f"**{len(rows)} work order(s)** with a COOIS user status{scope}", ""]
        lines += _md_table(["Work order", "User status", "Excluded"],
                           [[r["WorkOrder"], r["UserStatus"], r["Excluded"]] for r in rows])
        return Section("order_user_status", "Work-order user status (COOIS)", "\n".join(lines),
                       data={"filters": _filters(orders=orders), "orders": len(rows), "excluded": len(excluded),
                             "notInCOOIS": missing},
                       rows=rows, headline=headline, severity="info" if rows else "ok")

    # ================================================================ PP134 order master
    # output field -> candidate OData property names (matched ignoring case, spaces and punctuation).
    _OM_FIELDS = {
        "WorkOrder": ("ManufacturingOrder", "OrderNumber", "Order"),
        "OrderType": ("ManufacturingOrderType", "OrderType", "Type"),
        "EnteredBy": ("EnteredBy", "CreatedByUser", "CreatedBy"),
        "CreatedOn": ("CreatedOn", "CreationDate", "ManufacturingOrderCreationDate"),
        "StorageLocation": ("IssuanceLocation", "StorageLocation", "IssuingStorageLocation"),
        "Material": ("Material", "Product"),
        "Description": ("MaterialDescription", "ProductDescription"),
        "ExtMatGroup": ("ExtMatGrp", "ExternalMaterialGroup"),
        "MRPController": ("MRPController", "MRPResponsible"),
        "OrderQty": ("OrderQty", "TotalQuantity", "OrderQuantity"),
        "YieldQty": ("YieldQty", "YieldQuantity", "ConfirmedYieldQuantity"),
        "ScrapQty": ("ScrapQty", "ScrapQuantity", "ConfirmedScrapQuantity"),
        "Unit": ("BUn", "BaseUnit", "ProductionUnit"),
        "OrderStart": ("OrdStart", "OrderStart", "MfgOrderScheduledStartDate", "ScheduledStartDate"),
        "OrderFinish": ("OrdFinish", "OrderFinish", "MfgOrderScheduledEndDate", "ScheduledEndDate"),
        "ReleasedOn": ("ReleaseOn", "ReleasedOn", "MfgOrderActualReleaseDate"),
        "ActualStart": ("ActualStart", "MfgOrderActualStartDate"),
        "MaterialGroup": ("MaterialGroup",),
        "ProfitCenter": ("ProfitCtr", "ProfitCenter"),
        "WorkCenter": ("WorkCenter",),
    }

    def order_master(self, orders=(), materials=(), slocs=(), mrp=(), order_types=()) -> Section:
        recs = self.source.report_rows("order_master")
        if not recs:
            return Section("order_master", "PP134 order master", "No orders found.", data={"positions": 0},
                           rows=[], headline="PP134_OrderMaster returned no orders.", severity="ok")
        rows = [_om_row(r, self._OM_FIELDS) for r in recs]
        if not any(r["WorkOrder"] for r in rows):
            raise DataLoadError("PP134_OrderMaster: no order-number property found; fields returned: "
                                + ", ".join(sorted(recs[0])))
        # Filtered here (the OData property names are not confirmed): the set is ~2000 rows by design.
        want = {"WorkOrder": set(orders), "Material": set(materials), "StorageLocation": set(slocs),
                "MRPController": set(mrp), "OrderType": set(order_types)}
        rows = [r for r in rows if all(not v or str(r[k] or "").upper() in v for k, v in want.items())]
        rows.sort(key=lambda r: (r["OrderStart"] or "", r["WorkOrder"] or ""))
        total_qty = sum(r["OrderQty"] or 0 for r in rows)
        by_type = Counter(r["OrderType"] or "-" for r in rows)
        scope = _scope(orders=orders, materials=materials, slocs=slocs, mrp=mrp, order_types=order_types)
        if rows:
            split = ", ".join(f"{n}x {t}" for t, n in by_type.most_common(5))
            headline = (f"{_plural(len(rows), 'order')}{scope} in the PP134 order master ({split}); "
                        f"total order quantity {fmt(total_qty)}.")
        else:
            headline = f"No orders{scope} in the PP134 order master."
        lines = [f"**{len(rows)} order(s)**{scope} - PP134 order master", ""]
        lines += _md_table(["Order", "Type", "SLoc", "Material", "Description", "MRP", "Order qty", "Start", "Finish"],
                           [[r["WorkOrder"], r["OrderType"], r["StorageLocation"], r["Material"], r["Description"],
                             r["MRPController"], fmt(r["OrderQty"]), r["OrderStart"], r["OrderFinish"]] for r in rows])
        return Section("order_master", "PP134 order master", "\n".join(lines),
                       data={"filters": _filters(orders=orders, materials=materials, slocs=slocs, mrp=mrp,
                                                 order_types=order_types),
                             "orders": len(rows), "totalOrderQty": total_qty, "byType": dict(by_type)},
                       rows=rows, headline=headline, severity="info" if rows else "ok")


# ==================================================================== helpers
def _label_filter(slocs, materials) -> str | None:
    """RowLabel = SLoc + material: prefix match for locations, suffix match for materials."""
    parts = []
    for name, values in (("startswith", slocs), ("endswith", materials)):
        values = sorted(set(values))
        if values:
            terms = [f"{name}(RowLabel,{literal(v)})" for v in values]
            parts.append(terms[0] if len(terms) == 1 else "(" + " or ".join(terms) + ")")
    return " and ".join(parts) or None


_LABELS = {"orders": "work order", "components": "component", "materials": "material", "slocs": "storage location",
           "mrp": "MRP controller", "order_types": "order type", "statuses": "status"}


def _filters(**kw) -> dict:
    out = {k: sorted(v) for k, v in kw.items() if k != "min_aging" and v}
    if kw.get("min_aging"):
        out["minAgingDays"] = int(kw["min_aging"])
    return out


def _scope(**kw) -> str:
    """Filters as plain words, e.g. ' for material ACPE21670 in storage location A201'."""
    parts = []
    for key, values in kw.items():
        if not values:
            continue
        if key == "min_aging":
            parts.append(f"not used for at least {int(values)} days")
            continue
        values = sorted(values)
        word = _LABELS[key] + ("s" if len(values) > 1 else "")
        prep = "in" if key == "slocs" else "with" if key == "statuses" else "for"
        parts.append(f"{prep} {word} {', '.join(values[:5])}{' ...' if len(values) > 5 else ''}")
    return (" " + " ".join(parts)) if parts else ""


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _om_row(rec: dict, fields: dict) -> dict:
    """One PP134_OrderMaster record -> the output row; property names are matched ignoring case/punctuation."""
    flat = {_norm(k): v for k, v in rec.items() if not k.startswith("_")}
    out = {}
    for name, candidates in fields.items():
        value = next((flat[_norm(c)] for c in candidates if _norm(c) in flat), None)
        out[name] = _d(str(value)) if value and name in ("CreatedOn", "OrderStart", "OrderFinish", "ReleasedOn", "ActualStart") else value
    return out
