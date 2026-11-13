"""Planner query service.

Each public method answers one planner question type and returns a ``Section``:
a markdown narration for the chat, a structured payload for the caller, and flat
rows for CSV export. All numbers come from the repository + ``rules``; nothing is
estimated.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.data.repository import DataRepository, nav
from app.planner import rules
from app.planner.rules import fmt

MAX_TABLE_ROWS = 25  # chat tables are truncated; CSV export carries everything


@dataclass
class Section:
    skill: str
    title: str
    text: str
    data: dict = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    # deterministic | needs_review | not_found | info
    result_type: str = "deterministic"
    # Plain-language conclusion + recommended next steps, shown before the details.
    headline: str = ""
    actions: list[str] = field(default_factory=list)
    # ok | warning | critical | info - drives how prominently the answer is flagged
    severity: str = "info"
    # Storage-location code -> name, from the data this answer was built on (warehouse vs. line-side).
    location_names: dict = field(default_factory=dict)


class PlannerService:
    def __init__(self, repo: DataRepository, aging_threshold_days: int):
        self.repo = repo
        self.aging_threshold = aging_threshold_days
        self._controlled_requirement = self._build_controlled_requirement()

    # ================================================================ helpers
    def _build_controlled_requirement(self) -> dict[tuple[str, str], float]:
        """Σ ControlledRemainingIssueQty per (SLoc, Material) over controlled components
        (ExcludeReason empty) - the lookup table behind spec §6.1/§6.2."""
        req: dict[tuple[str, str], float] = defaultdict(float)
        for c in self.repo.all_components():
            if not c.get("ExcludeReason") and c.get("StorageLocation"):
                req[(c["StorageLocation"], c["Material"].upper())] += c.get("ControlledRemainingIssueQty") or 0
        return dict(req)

    def _aged_stock(self, material: str) -> list[dict]:
        return [
            {"storageLocation": i["StorageLocation"], "agingDays": i["AgingDays"],
             "unrestricted": i["Unrestricted"], "lastPlacementDate": _d(i.get("LastPlacementDate"))}
            for i in self.repo.inventory_by_material.get(material.upper(), [])
            if rules.is_aged(i.get("AgingDays"), self.aging_threshold)
        ]

    def _order_status_text(self, order: str) -> str:
        wo = self.repo.shortage_by_order.get(order)
        if wo and wo.get("OrderStatus"):
            return wo["OrderStatus"]
        po = self.repo.production_order_by_order.get(order, {})
        return " ".join(s["StatusShortName"] for s in nav(po, "to_ProductionOrderStatus"))

    def _component_view(self, order_type: str, c: dict) -> dict:
        aged = self._aged_stock(c["Material"])
        return {
            "reservationItem": c.get("ReservationItem"),
            "material": c["Material"],
            "description": c.get("ProductDescription"),
            "storageLocation": c.get("StorageLocation"),
            "baseUnit": c.get("BaseUnit"),
            "requirementDate": _d(c.get("MatlCompRequirementDate")),
            "requiredQty": c.get("RequiredQuantity"),
            "theoreticalUsageWithScrap": c.get("TheoreticalUsageWithScrap"),
            "actualUsage": c.get("ActualUsage"),
            "remainingIssueQty": c.get("RemainingIssueQty"),
            "controlledRemainingIssueQty": c.get("ControlledRemainingIssueQty"),
            "lineSideStock": c.get("LineSideStock"),
            "allocatedQty": c.get("AllocatedQty"),
            "shortageQty": c.get("ShortageQty"),
            "warehouseStock": c.get("WarehouseStock"),
            "shortageStatus": c.get("ShortageStatus"),
            "excludeReason": c.get("ExcludeReason") or None,
            "rootCause": rules.shortage_root_cause(c),
            "isShort": rules.is_short(c),
            "overIssuedQty": rules.over_issued_qty(c),
            "overIssueClass": rules.classify_over_issue(c),
            "z67z68Adjustment": rules.z67_z68_adjustment(c),
            "isAged": bool(aged),
            "agedStock": aged,
            "needsReview": rules.needs_machine_head_review(order_type, c),
            "dataQualityIssues": rules.data_quality_issues(c),
        }

    def _order_matches(self, wo: dict, mrp: set[str], order_types: set[str], materials: set[str]) -> bool:
        if mrp and wo.get("MRPController") not in mrp:
            return False
        if order_types and wo.get("ManufacturingOrderType") not in order_types:
            return False
        if materials:
            mats = {wo["Material"].upper()} | {c["Material"].upper() for c in nav(wo, "to_ComponentShortage")}
            if not mats & materials:
                return False
        return True

    # ================================================================ skills
    WO_TOPICS = ("shortage", "aged", "over_issued")

    def work_order_shortage(self, order: str, focus: set[str] | None = None) -> Section:
        """Work order view. ``focus`` limits the answer to the topics asked about (default: all)."""
        focus = set(focus or self.WO_TOPICS)
        wo = self.repo.shortage_by_order.get(order)
        if not wo:
            return self._order_not_found("work_order_shortage", order)
        po = self.repo.production_order_by_order.get(order, {})
        comps = [self._component_view(wo["ManufacturingOrderType"], c) for c in nav(wo, "to_ComponentShortage")]
        short = [c for c in comps if c["isShort"]]
        aged = [c for c in comps if c["isAged"]]
        over = [c for c in comps if c["overIssuedQty"] > 0]
        review = [c for c in comps if c["needsReview"]]
        dq = [c for c in comps if c["dataQualityIssues"]]
        # Only the topics asked about appear in the answer.
        short_v = short if "shortage" in focus else []
        aged_v = aged if "aged" in focus else []
        over_v = over if "over_issued" in focus else []

        header = {
            "workOrder": order,
            "orderType": wo["ManufacturingOrderType"],
            "material": wo["Material"],
            "description": wo.get("ProductDescription"),
            "storageLocation": wo.get("StorageLocation"),
            "mrpController": wo.get("MRPController"),
            "workCenter": po.get("WorkCenter"),
            "totalQuantity": wo.get("TotalQuantity"),
            "confirmedYield": wo.get("MfgOrderConfirmedYieldQty"),
            "scheduledStart": _d(wo.get("MfgOrderScheduledStartDate")),
            "scheduledEnd": _d(po.get("MfgOrderScheduledEndDate")),
            "orderStatus": wo.get("OrderStatus"),
            "orderShortageStatus": wo.get("OrderShortageStatus"),
            "missingPartsCount": wo.get("MissingPartsCount"),
        }

        lines = [
            f"**Work order {order}** ({header['orderType']}) - {header['material']} *{header['description']}*",
            f"Qty {fmt(header['totalQuantity'])} (confirmed {fmt(header['confirmedYield'])}) | "
            f"Start {header['scheduledStart']} -> End {header['scheduledEnd'] or '-'} | "
            f"MRP {header['mrpController']} | Status `{header['orderStatus']}`",
            "",
        ]
        if "shortage" in focus:
            if short:
                lines.append(f"**Shortage: {len(short)} component(s) short** (of {len(comps)})")
                lines += _md_table(
                    ["Component", "Description", "SLoc", "Short qty", "Line-side", "Warehouse", "Root cause"],
                    [[c["material"], c["description"], c["storageLocation"] or "-",
                      f"{fmt(c['shortageQty'])} {c['baseUnit']}", fmt(c["lineSideStock"]),
                      fmt(c["warehouseStock"]), c["rootCause"]] for c in short])
            else:
                lines.append(f"**No shortage** - all {len(comps)} components covered or without open demand.")
            lines.append("")

        if "aged" in focus:
            if aged:
                lines.append(f"**Aged material (> {self.aging_threshold} days): {len(aged)} component(s)**")
                lines += [f"- {c['material']} *{c['description']}*: " + ", ".join(
                    f"{a['storageLocation']} {fmt(a['unrestricted'])} {c['baseUnit']} @ {a['agingDays']} d"
                    for a in c["agedStock"]) for c in aged]
            else:
                lines.append(f"**Aged material:** none of this order's components hold stock aged > {self.aging_threshold} days.")
            lines.append("")

        if "over_issued" in focus:
            if over:
                lines.append(f"**Over-issued WIP: {len(over)} component(s)**")
                for c in over:
                    adj = c["z67z68Adjustment"]
                    adj_txt = f"; Z67/Z68 {adj['direction']} {fmt(adj['quantity'])}" if adj else ""
                    excl = f" (order excluded: {c['excludeReason']})" if c["excludeReason"] else ""
                    lines.append(f"- {c['material']}: issued {fmt(c['actualUsage'])} vs required "
                                 f"{fmt(c['requiredQty'])} -> over by {fmt(c['overIssuedQty'])} {c['baseUnit']} "
                                 f"- {c['overIssueClass']}{adj_txt}{excl}")
            else:
                lines.append("**Over-issued WIP:** none.")

        if review:
            lines += ["", f"**Needs review (machine-head material candidate):** "
                      + ", ".join(c["material"] for c in review)
                      + " - flagged for human review, not a resolved status."]
        if dq:
            lines += ["", "**Data quality warnings:**"] + [
                f"- {c['material']}: {'; '.join(c['dataQualityIssues'])}" for c in dq]

        expedite = [c for c in short_v if c["shortageStatus"].startswith("Short - warehouse")]
        unexplained = [c for c in over_v if not (c["overIssueClass"] or "").startswith("Over-control")]
        # Plain-language paragraph: what the order makes, then one sentence per topic asked about.
        name = lambda c: f"{c['description']} ({c['material']})"  # noqa: E731
        qty = lambda q, c: f"{fmt(q)} {c['baseUnit']}"  # noqa: E731
        heads = [f"Work order {order} is making {fmt(header['totalQuantity'])} x {header['description']}, "
                 f"with {fmt(header['confirmedYield'])} done so far"
                 + (f" and a due date of {header['scheduledEnd']}." if header["scheduledEnd"] else ".")]
        movable = [c for c in short if c not in expedite]
        if "shortage" in focus:
            if short:
                heads.append(f"{_count(len(short))} of its {len(comps)} materials {'is' if len(short) == 1 else 'are'} short.")
                if expedite:
                    heads.append(f"{_names([c['description'] for c in expedite])} "
                                 f"{'has' if len(expedite) == 1 else 'have'} no stock in the warehouse and must be "
                                 f"expedited from the supplier.")
                if movable:
                    names_ = _names([c['description'] for c in movable])
                    heads.append((f"The rest ({names_})" if expedite and len(movable) > 1 else names_)
                                 + " can be moved from the warehouse to the production line.")
            else:
                heads.append(f"All {len(comps)} materials are available, so nothing is short.")
        if "aged" in focus:
            if aged:
                oldest = max(a["agingDays"] for c in aged for a in c["agedStock"])
                heads.append(f"{_count(len(aged))} of its materials {'has' if len(aged) == 1 else 'have'} stock older than "
                             f"{self.aging_threshold} days (up to {oldest} days)"
                             + (", including " if len(aged) > 3 else ": ")
                             + f"{_names([c['description'] for c in aged])}. Use this older stock first.")
            else:
                heads.append(f"None of its materials has stock older than {self.aging_threshold} days.")
        if "over_issued" in focus:
            if over:
                heads.append(f"{_count(len(over))} material{' was' if len(over) == 1 else 's were'} issued more than the order needs"
                             + (f", and {len(unexplained)} of them {'has' if len(unexplained) == 1 else 'have'} "
                                f"no stock adjustment to explain it." if unexplained else
                                ", all explained by stock adjustments."))
            else:
                heads.append("No material was issued more than the order needs.")
        headline = " ".join(heads)

        actions = []
        for c in short_v:
            if c in expedite:
                actions.append(f"Expedite {qty(c['shortageQty'], c)} of {name(c)} from the supplier.")
            else:
                actions.append(f"Move {qty(c['shortageQty'], c)} of {name(c)} from the warehouse to the production line.")
        if aged_v:
            actions.append(f"Use the older stock first for {_names([name(c) for c in aged_v], limit=5)}.")
        if unexplained:
            actions.append("Check why these were over-issued: " + ", ".join(name(c) for c in unexplained) + ".")
        if review:
            actions.append("Have someone check these possible machine-head materials: "
                           + ", ".join(name(c) for c in review) + ".")
        for c in dq:
            actions.append(f"Correct the material data for {name(c)}: {'; '.join(c['dataQualityIssues'])}.")
        if not actions:
            actions.append("No action needed.")

        return Section(
            skill="work_order_shortage",
            title=f"Work order {order} - " + " & ".join(
                {"shortage": "shortage", "aged": "aged material", "over_issued": "over-issue"}[t]
                for t in self.WO_TOPICS if t in focus).replace(" & ", ", ", len(focus) - 2),
            text="\n".join(lines),
            data={"header": header, "components": comps,
                  "summary": {"components": len(comps), "short": len(short), "aged": len(aged),
                              "overIssued": len(over), "needsReview": len(review),
                              "dataQualityIssues": len(dq)}},
            rows=[{"WorkOrder": order, **_flat(c)} for c in comps],
            result_type="needs_review" if review else "deterministic",
            headline=headline, actions=actions,
            severity="critical" if expedite else "warning" if short_v or aged_v or unexplained or dq else "ok",
        )

    def shortage_overview(self, mrp: set[str] = frozenset(), order_types: set[str] = frozenset(),
                          materials: set[str] = frozenset()) -> Section:
        orders = [wo for wo in self.repo.shortage_by_order.values()
                  if self._order_matches(wo, mrp, order_types, materials)]
        short_orders = []
        for wo in orders:
            comps = [c for c in nav(wo, "to_ComponentShortage") if rules.is_short(c)
                     and (not materials or c["Material"].upper() in materials or wo["Material"].upper() in materials)]
            if comps:
                short_orders.append((wo, comps))
        short_orders.sort(key=lambda x: (x[0].get("MfgOrderScheduledStartDate") or "", x[0]["ManufacturingOrder"]))

        filt = _filter_text(mrp=mrp, order_types=order_types, materials=materials)
        lines = [f"**{len(short_orders)} of {len(orders)} work orders have missing parts**{filt}", ""]
        rows = []
        for wo, comps in short_orders:
            expedite = sum(1 for c in comps if c["ShortageStatus"].startswith("Short - warehouse"))
            rows.append({
                "WorkOrder": wo["ManufacturingOrder"], "OrderType": wo["ManufacturingOrderType"],
                "Material": wo["Material"], "Description": wo.get("ProductDescription"),
                "MRPController": wo.get("MRPController"),
                "ScheduledStart": _d(wo.get("MfgOrderScheduledStartDate")),
                "ShortComponents": len(comps), "NeedExpedite": expedite,
                "ReplenishFromWarehouse": len(comps) - expedite,
                "ShortMaterials": ", ".join(c["Material"] for c in comps),
            })
        lines += _md_table(
            ["Work order", "Type", "Material", "MRP", "Start", "Short", "Expedite", "Short components"],
            [[r["WorkOrder"], r["OrderType"], r["Material"], r["MRPController"], r["ScheduledStart"],
              r["ShortComponents"], r["NeedExpedite"], r["ShortMaterials"]] for r in rows])
        if rows:
            lines += ["", "_Expedite = warehouse stock insufficient; others can be replenished from warehouse via ZPL._"]
        exp_orders = [r for r in rows if r["NeedExpedite"]]
        actions = []
        if exp_orders:
            actions.append("Expedite supply first for " + ", ".join(r["WorkOrder"] for r in exp_orders[:8])
                           + (" ..." if len(exp_orders) > 8 else "") + " - the warehouse cannot cover them.")
        if rows:
            actions.append(f"Create pull lists (ZPL) for the remaining "
                           f"{sum(r['ReplenishFromWarehouse'] for r in rows)} short line(s) - stock is in the warehouse.")
            actions.append(f"Earliest affected start: work order {rows[0]['WorkOrder']} on {rows[0]['ScheduledStart']}.")
        scope = _scope(mrp=mrp, order_types=order_types, materials=materials)
        if rows:
            para = [f"{fmt(len(rows))} of the {_plural(len(orders), 'work order')}{scope} "
                    f"{'is' if len(rows) == 1 else 'are'} short of material."]
            if exp_orders:
                para.append(f"{'Work order' if len(exp_orders) == 1 else 'Work orders'} "
                            f"{_names([r['WorkOrder'] for r in exp_orders], limit=4)} "
                            f"{'needs' if len(exp_orders) == 1 else 'need'} parts that the warehouse "
                            f"does not have, so supply for {'it' if len(exp_orders) == 1 else 'them'} must be expedited first.")
            if len(exp_orders) < len(rows):
                para.append(("For the rest" if exp_orders else "For all of them")
                            + ", the missing parts are in the warehouse and only need to be moved to the production line.")
            para.append(f"The earliest affected order is {rows[0]['WorkOrder']}, starting on {rows[0]['ScheduledStart']}.")
            headline = " ".join(para)
        else:
            headline = f"Good news: none of the {_plural(len(orders), 'work order')}{scope} is short of material."
        return Section("shortage_overview", "Work order shortage overview", "\n".join(lines),
                       data={"filters": _filters(mrp, order_types, materials), "ordersChecked": len(orders),
                             "ordersShort": rows},
                       rows=rows,
                       headline=headline,
                       actions=actions or ["No action needed."],
                       severity="critical" if exp_orders else "warning" if rows else "ok")

    def aged_materials(self, materials: set[str] = frozenset(), slocs: set[str] = frozenset()) -> Section:
        rows = []
        for i in self.repo.inventory:
            if not rules.is_aged(i.get("AgingDays"), self.aging_threshold):
                continue
            if materials and i["Material"].upper() not in materials:
                continue
            if slocs and i["StorageLocation"] not in slocs:
                continue
            demand = self._controlled_requirement.get((i["StorageLocation"], i["Material"].upper()))
            rows.append({
                "Material": i["Material"], "Description": i.get("ProductDescription"),
                "StorageLocation": i["StorageLocation"],
                "StorageLocationName": self.repo.storage_location_names.get(i["StorageLocation"], ""),
                "AgingDays": i["AgingDays"], "LastPlacementDate": _d(i.get("LastPlacementDate")),
                "Unrestricted": i.get("Unrestricted"), "BaseUnit": i.get("BaseUnit"),
                "StockValue": i.get("UnrestrictedStockValue"), "Currency": i.get("Currency"),
                "OpenControlledDemand": round(demand, 3) if demand else 0,
            })
        rows.sort(key=lambda r: -r["AgingDays"])
        total_value = round(sum(r["StockValue"] or 0 for r in rows), 2)
        currency = rows[0]["Currency"] if rows else ""
        filt = _filter_text(materials=materials, slocs=slocs)
        lines = [f"**{len(rows)} aged stock position(s) (> {self.aging_threshold} days)**{filt} - "
                 f"value {total_value:,.2f} {currency}", ""]
        lines += _md_table(
            ["Material", "Description", "SLoc", "Aging days", "Unrestricted", "Value", "Open demand"],
            [[r["Material"], r["Description"], r["StorageLocation"], r["AgingDays"],
              f"{fmt(r['Unrestricted'])} {r['BaseUnit']}", fmt(r["StockValue"]),
              fmt(r["OpenControlledDemand"]) if r["OpenControlledDemand"] else "none"] for r in rows])
        no_demand = sum(1 for r in rows if not r["OpenControlledDemand"])
        if no_demand:
            lines += ["", f"_{no_demand} aged position(s) have no open controlled demand - candidates for return-to-stock._"]
        actions = []
        if rows:
            oldest = rows[0]
            actions.append(f"Oldest: {oldest['Material']} ({oldest['Description']}) in {oldest['StorageLocation']} - "
                           f"{oldest['AgingDays']} days, {fmt(oldest['Unrestricted'])} {oldest['BaseUnit']}.")
            if no_demand:
                actions.append(f"Return to warehouse or scrap review: {no_demand} aged position(s) have no open "
                               "work-order demand.")
            if len(rows) - no_demand:
                actions.append(f"Consume first: {len(rows) - no_demand} aged position(s) are still needed by open "
                               "work orders - issue them before newer stock.")
        scope = _scope(materials=materials, slocs=slocs)
        if rows:
            oldest = rows[0]
            needed = len(rows) - no_demand
            para = [f"There {'is' if len(rows) == 1 else 'are'} {_plural(len(rows), 'stock item')}{scope} that "
                    f"{'has' if len(rows) == 1 else 'have'} not been used for more than {self.aging_threshold} days, "
                    f"worth {total_value:,.0f} {currency} in total.",
                    f"The oldest is {oldest['Description']} ({oldest['Material']}) in storage location "
                    f"{oldest['StorageLocation']}, unused for {oldest['AgingDays']} days."]
            top = list(dict.fromkeys(r["Description"] for r in sorted(rows, key=lambda r: -(r["StockValue"] or 0))
                                     if r["StockValue"]))[:3]
            if len(rows) > 2 and len(top) > 1:
                para.append("Most of the value sits in " + _names(top) + ".")
            if no_demand:
                para.append(("All of them are" if no_demand == len(rows) else
                             f"{_plural(no_demand, 'item')} {'is' if no_demand == 1 else 'are'}")
                            + " not needed by any open work order and could be returned to the warehouse or reviewed for scrap.")
            if needed:
                para.append(("All of them are" if not no_demand else
                             f"The other {_plural(needed, 'item')} {'is' if needed == 1 else 'are'}")
                            + " still needed by open work orders and should be used before newer stock.")
            headline = " ".join(para)
        else:
            headline = f"Good news: there is no stock{scope} older than {self.aging_threshold} days."
        return Section("aged_materials", "Aged materials", "\n".join(lines),
                       data={"thresholdDays": self.aging_threshold, "filters": _filters(materials=materials, slocs=slocs),
                             "totalValue": total_value, "currency": currency, "positions": rows},
                       rows=rows,
                       headline=headline,
                       actions=actions or ["No action needed."],
                       severity="warning" if rows else "ok")

    def over_issued(self, orders: set[str] = frozenset(), mrp: set[str] = frozenset(),
                    order_types: set[str] = frozenset(), materials: set[str] = frozenset()) -> Section:
        rows = []
        for wo in self.repo.shortage_by_order.values():
            if orders and wo["ManufacturingOrder"] not in orders:
                continue
            if not self._order_matches(wo, mrp, order_types, set()):
                continue
            for c in nav(wo, "to_ComponentShortage"):
                if materials and c["Material"].upper() not in materials:
                    continue
                qty = rules.over_issued_qty(c)
                if qty <= 0:
                    continue
                adj = rules.z67_z68_adjustment(c)
                rows.append({
                    "WorkOrder": wo["ManufacturingOrder"], "OrderType": wo["ManufacturingOrderType"],
                    "OrderStatus": wo.get("OrderStatus"), "MRPController": wo.get("MRPController"),
                    "Component": c["Material"], "Description": c.get("ProductDescription"),
                    "StorageLocation": c.get("StorageLocation"), "BaseUnit": c.get("BaseUnit"),
                    "RequiredQty": c.get("RequiredQuantity"), "ActualUsage": c.get("ActualUsage"),
                    "NetGI_CO11N": c.get("NetGI_CO11N"), "Z67Z68Adjustment": adj["quantity"] if adj else 0,
                    "OverIssuedQty": qty, "Classification": rules.classify_over_issue(c),
                    "OrderExcludeReason": c.get("ExcludeReason") or "",
                })
        # Lines that need a decision (plain over-issue) first, so they are never pushed to a later page.
        rows.sort(key=lambda r: (r["Classification"].startswith("Over-control"), r["WorkOrder"], r["Component"]))
        reasonable = sum(1 for r in rows if r["Classification"].startswith("Over-control"))
        filt = _filter_text(orders=orders, mrp=mrp, order_types=order_types, materials=materials)
        lines = [f"**{len(rows)} over-issued WIP component line(s)**{filt} - "
                 f"{reasonable} explained by Z67/Z68 adjust-in (reasonable over-control), "
                 f"{len(rows) - reasonable} plain over-issue.", ""]
        lines += _md_table(
            ["Work order", "Component", "Required", "Issued", "Over by", "Z67/Z68", "Classification"],
            [[r["WorkOrder"], r["Component"], fmt(r["RequiredQty"]), fmt(r["ActualUsage"]),
              f"{fmt(r['OverIssuedQty'])} {r['BaseUnit']}", fmt(r["Z67Z68Adjustment"]) if r["Z67Z68Adjustment"] else "-",
              r["Classification"] + (f" ({r['OrderExcludeReason']})" if r["OrderExcludeReason"] else "")]
             for r in rows])
        plain = [r for r in rows if not r["Classification"].startswith("Over-control")]
        actions = []
        if reasonable:
            actions.append(f"{reasonable} line(s) are explained by Z67/Z68 adjustments (reasonable over-control) - no action needed.")
        for r in plain[:10]:
            actions.append(f"Investigate {r['Component']} on work order {r['WorkOrder']}: issued {fmt(r['ActualUsage'])} "
                           f"vs required {fmt(r['RequiredQty'])} (+{fmt(r['OverIssuedQty'])} {r['BaseUnit']}) with no adjustment posted.")
        scope = _scope(orders=orders, mrp=mrp, order_types=order_types, materials=materials)
        if rows:
            para = [f"{_plural(len(rows), 'material line')}{scope} {'was' if len(rows) == 1 else 'were'} issued "
                    f"to production in larger quantities than the work order needs."]
            if reasonable:
                para.append(f"{'All of these are' if not plain else f'{fmt(reasonable)} of these are'} covered by "
                            f"approved stock adjustments, so no action is needed for "
                            f"{'them' if reasonable > 1 else 'it'}.")
            if plain:
                para.append(f"{'One has' if len(plain) == 1 else f'{_count(len(plain))} have'} no adjustment to explain "
                            f"the extra quantity and should be checked: " + _names(
                                [f"{r['Description']} on work order {r['WorkOrder']} "
                                 f"({fmt(r['OverIssuedQty'])} {r['BaseUnit']} extra)" for r in plain]) + ".")
            headline = " ".join(para)
        else:
            headline = f"Good news: no material{scope} has been issued beyond what the work order needs."
        return Section("over_issued", "Over-issued material", "\n".join(lines),
                       data={"filters": _filters(mrp, order_types, materials, orders=orders), "lines": rows},
                       rows=rows,
                       headline=headline,
                       actions=actions or ["No action needed."],
                       severity="warning" if plain else "ok")

    def material_status(self, materials: set[str] = frozenset(), slocs: set[str] = frozenset(),
                        status_filter: str | None = None) -> Section:
        """Line-side issuable / over-control / non-controlled check (spec §6.1-6.2, Z_AGEDINV logic)."""
        stock: dict[tuple[str, str], dict] = {}
        for i in self.repo.inventory:
            if not self.repo.is_line_side(i["StorageLocation"]):
                continue
            key = (i["StorageLocation"], i["Material"].upper())
            agg = stock.setdefault(key, {"Material": i["Material"], "Description": i.get("ProductDescription"),
                                         "StorageLocation": i["StorageLocation"], "BaseUnit": i.get("BaseUnit"),
                                         "Unrestricted": 0.0, "AgingDays": i.get("AgingDays")})
            agg["Unrestricted"] = round(agg["Unrestricted"] + (i.get("Unrestricted") or 0)
                                        + (i.get("UnrestrictedConsignment") or 0), 3)
            agg["AgingDays"] = max(agg["AgingDays"] or 0, i.get("AgingDays") or 0)

        rows = []
        for key, agg in sorted(stock.items()):
            if materials and key[1] not in materials:
                continue
            if slocs and key[0] not in slocs:
                continue
            req = self._controlled_requirement.get(key)
            status, available = rules.material_status(agg["Unrestricted"], req)
            if status_filter and status != status_filter:
                continue
            rows.append({**agg, "ControlledRemainingIssueQty": round(req, 3) if req is not None else None,
                         "AvailableToIssueQty": available, "Status": status,
                         "IsAged": rules.is_aged(agg["AgingDays"], self.aging_threshold)})

        counts = defaultdict(int)
        for r in rows:
            counts[r["Status"]] += 1
        filt = _filter_text(materials=materials, slocs=slocs)
        lines = [f"**Line-side material status** ({len(rows)} SLoc/material positions){filt}: "
                 + ", ".join(f"{v} {k}" for k, v in counts.items()), "",
                 "_Rule: Available = Unrestricted - Controlled remaining issue; < 0 = over-control; "
                 "no open controlled requirement = non-controlled._", ""]
        lines += _md_table(
            ["SLoc", "Material", "Description", "Unrestricted", "Ctrl remaining", "Available", "Status", "Aging"],
            [[r["StorageLocation"], r["Material"], r["Description"], fmt(r["Unrestricted"]),
              fmt(r["ControlledRemainingIssueQty"]), fmt(r["AvailableToIssueQty"]), r["Status"],
              r["AgingDays"]] for r in rows])
        n_over = counts.get(rules.STATUS_OVER_CONTROL, 0)
        n_non = counts.get(rules.STATUS_NON_CONTROLLED, 0)
        n_ok = counts.get(rules.STATUS_AVAILABLE, 0)
        actions = []
        if n_over:
            actions.append(f"{n_over} position(s) are over-control: open work orders need more than the line-side stock - "
                           "do not issue more without checking the order requirement.")
        if n_non:
            actions.append(f"{n_non} position(s) are non-controlled: stock on the line with no open work order - "
                           "consider returning it to the warehouse.")
        if n_ok:
            actions.append(f"{n_ok} position(s) can still be issued within the controlled allowance.")
        scope = _scope(materials=materials, slocs=slocs)
        if rows:
            para = [f"I checked {_plural(len(rows), 'stock item')} on the production line{scope}."]
            if n_ok:
                para.append(f"{_plural(n_ok, 'item')} can still be issued to work orders within the planned quantity.")
            if n_over:
                para.append(f"{_plural(n_over, 'item')} {'has' if n_over == 1 else 'have'} less stock on the line than "
                            f"open work orders still need, so check the order requirement before issuing more.")
            if n_non:
                para.append(f"{_plural(n_non, 'item')} {'is' if n_non == 1 else 'are'} sitting on the line with no open "
                            f"work order and could be returned to the warehouse.")
            headline = " ".join(para)
        else:
            headline = f"There is no stock on the production line{scope}."
        return Section("material_status", "Production line stock status", "\n".join(lines),
                       data={"filters": _filters(materials=materials, slocs=slocs), "counts": dict(counts),
                             "positions": rows},
                       rows=rows,
                       headline=headline,
                       actions=actions or ["No action needed."],
                       severity="warning" if n_over or n_non else "ok")

    def production_order(self, order: str) -> Section:
        po = self.repo.production_order_by_order.get(order)
        if not po:
            return self._order_not_found("production_order", order)
        statuses = [{"code": s["StatusShortName"], "name": s["StatusName"], "userStatus": bool(s.get("IsUserStatus"))}
                    for s in nav(po, "to_ProductionOrderStatus")]
        ops = [{"operation": o["ManufacturingOrderOperation"], "text": o.get("OperationText"),
                "workCenter": o.get("WorkCenter"), "plannedQty": o.get("OpPlannedTotalQuantity"),
                "confirmedYield": o.get("OpTotalConfirmedYieldQty"), "confirmedScrap": o.get("OpTotalConfirmedScrapQty"),
                "start": _d(o.get("OpErlstSchedldExecStrtDte")), "end": _d(o.get("OpErlstSchedldExecEndDte"))}
               for o in nav(po, "to_ProductionOrderOperation")]
        comps = [{"item": c["ReservationItem"], "material": c["Material"], "description": c.get("ProductDescription"),
                  "storageLocation": c.get("StorageLocation"), "requiredQty": c.get("RequiredQuantity"),
                  "withdrawnQty": c.get("WithdrawnQuantity"), "baseUnit": c.get("BaseUnit"),
                  "requirementDate": _d(c.get("MatlCompRequirementDate")),
                  "componentType": c.get("MaterialComponentType"),
                  "dataQualityIssue": c.get("DataQualityIssue") or None}
                 for c in nav(po, "to_ProductionOrderComponent")]
        header = {
            "workOrder": order, "orderType": po.get("ManufacturingOrderType"), "material": po.get("Material"),
            "description": po.get("ProductDescription"), "plant": po.get("ProductionPlant"),
            "storageLocation": po.get("StorageLocation"), "mrpController": po.get("MRPController"),
            "workCenter": po.get("WorkCenter"), "productionVersion": po.get("ProductionVersion"),
            "totalQuantity": po.get("TotalQuantity"), "unit": po.get("ProductionUnit"),
            "confirmedYield": po.get("MfgOrderConfirmedYieldQty"), "confirmedScrap": po.get("MfgOrderConfirmedScrapQty"),
            "deliveredQty": po.get("ActualDeliveredQuantity"),
            "plannedStart": _d(po.get("MfgOrderPlannedStartDate")), "plannedEnd": _d(po.get("MfgOrderPlannedEndDate")),
            "scheduledStart": _d(po.get("MfgOrderScheduledStartDate")),
            "scheduledEnd": _d(po.get("MfgOrderScheduledEndDate")),
            "releaseDate": _d(po.get("MfgOrderActualReleaseDate")), "createdBy": po.get("CreatedByUser"),
            "reservation": po.get("Reservation"),
        }
        progress = (header["confirmedYield"] or 0) / header["totalQuantity"] * 100 if header["totalQuantity"] else 0
        lines = [
            f"**Production order {order}** ({header['orderType']}) - {header['material']} *{header['description']}*",
            f"Qty {fmt(header['totalQuantity'])} {header['unit']} | confirmed {fmt(header['confirmedYield'])} "
            f"({progress:.0f}%) | scrap {fmt(header['confirmedScrap'])} | delivered {fmt(header['deliveredQty'])}",
            f"Scheduled {header['scheduledStart']} -> {header['scheduledEnd']} | Work center {header['workCenter']} | "
            f"MRP {header['mrpController']} | Released {header['releaseDate'] or 'not released'}",
            "Status: " + ", ".join(f"{s['code']} ({s['name']})" for s in statuses),
            "",
            f"**Components ({len(comps)})**",
        ]
        lines += _md_table(["Item", "Material", "Description", "SLoc", "Required", "Withdrawn", "Req. date"],
                           [[c["item"], c["material"], c["description"], c["storageLocation"] or "-",
                             f"{fmt(c['requiredQty'])} {c['baseUnit']}", fmt(c["withdrawnQty"]), c["requirementDate"]]
                            for c in comps])
        codes = {s["code"] for s in statuses}
        state = ("closed (technically completed)" if "TECO" in codes else "delivered" if "DLV" in codes
                 else "not released yet" if "CRTD" in codes and "REL" not in codes else "released and in progress")
        state_text = {"closed (technically completed)": "has been closed", "delivered": "has been delivered",
                      "not released yet": "has not been released yet"}.get(state, "is in progress")
        actions = []
        if "NMVT" in codes:
            actions.append("Order has user status NMVT (no movement) - it is excluded from shortage control.")
        if "MSPT" in codes:
            actions.append("Order has missing parts (MSPT).")
        dq_comps = [c for c in comps if c["dataQualityIssue"]]
        if dq_comps:
            actions.append("Fix master data: " + ", ".join(f"{c['material']} ({c['dataQualityIssue']})" for c in dq_comps) + ".")
        return Section("production_order", f"Production order {order}", "\n".join(lines),
                       data={"header": header, "statuses": statuses, "operations": ops, "components": comps},
                       rows=[{"WorkOrder": order, **c} for c in comps],
                       headline=" ".join(filter(None, [
                           f"Production order {order} is making {fmt(header['totalQuantity'])} {header['unit']} of "
                           f"{header['description']} and {state_text}.",
                           f"So far {fmt(header['confirmedYield'])} have been completed ({progress:.0f}%)"
                           + (f", with {fmt(header['confirmedScrap'])} scrapped." if header["confirmedScrap"] else "."),
                           f"It is scheduled to run from {header['scheduledStart']} to {header['scheduledEnd']}.",
                           "Some materials are still missing for this order." if "MSPT" in codes else "",
                           "It is marked as having no stock movement, so it is left out of shortage checks."
                           if "NMVT" in codes else "",
                           ("The material data needs correcting for " + _names(
                               [f"{c['description']} ({c['dataQualityIssue']})" for c in dq_comps]) + ".")
                           if dq_comps else "",
                       ])),
                       actions=actions,
                       severity="warning" if "MSPT" in codes or dq_comps else "ok")

    def material_report(self, material: str) -> Section:
        key = material.upper()
        mm = self.repo.material_master.get(key)
        stock = self.repo.inventory_by_material.get(key, [])
        if not mm and not stock:
            return Section("material_report", f"Material {material}",
                           f"Material **{material}** was not found in the material master or inventory for "
                           f"{self._plant_text()}.", result_type="not_found",
                           headline=f"I could not find material {material}. Please check the material number.")
        plant = (nav(mm, "to_Plant") or [{}])[0] if mm else {}
        val = (nav(mm, "to_Valuation") or [{}])[0] if mm else {}
        master = {
            "material": mm["Product"] if mm else material, "description": (mm or stock[0]).get("ProductDescription"),
            "materialType": mm.get("ProductType") if mm else stock[0].get("ProductType"),
            "materialGroup": mm.get("ProductGroup") if mm else stock[0].get("ProductGroup"),
            "baseUnit": (mm or stock[0]).get("BaseUnit"), "mrpType": plant.get("MRPType"),
            "mrpController": plant.get("MRPResponsible"), "procurementType": plant.get("ProcurementType"),
            "plannedDeliveryDays": plant.get("PlannedDeliveryDurationInDays"),
            "grProcessingDays": plant.get("GoodsReceiptDuration"), "abcIndicator": plant.get("ABCIndicator"),
            "batchManaged": bool(plant.get("IsBatchManagementRequired")), "bulk": bool(plant.get("IsBulkMaterial")),
            "standardPrice": val.get("StandardPrice"), "priceUnit": val.get("PriceUnitQty"), "currency": val.get("Currency"),
            "storageLocations": [s["StorageLocation"] for s in nav(mm, "to_StorageLocation")] if mm else [],
        }
        positions = []
        for i in sorted(stock, key=lambda x: x["StorageLocation"]):
            req = self._controlled_requirement.get((i["StorageLocation"], key))
            status = rules.material_status(i.get("Unrestricted") or 0, req)[0] if self.repo.is_line_side(i["StorageLocation"]) else "Warehouse stock"
            positions.append({
                "storageLocation": i["StorageLocation"],
                "storageLocationName": self.repo.storage_location_names.get(i["StorageLocation"], ""),
                "unrestricted": i.get("Unrestricted"), "blocked": i.get("Blocked"),
                "qualityInspection": i.get("QualityInspection"), "agingDays": i.get("AgingDays"),
                "isAged": rules.is_aged(i.get("AgingDays"), self.aging_threshold),
                "lastPlacementDate": _d(i.get("LastPlacementDate")), "controlStatus": status,
            })
        demand = []
        for wo in self.repo.shortage_by_order.values():
            for c in nav(wo, "to_ComponentShortage"):
                if c["Material"].upper() == key:
                    demand.append({"workOrder": wo["ManufacturingOrder"], "storageLocation": c.get("StorageLocation"),
                                   "requirementDate": _d(c.get("MatlCompRequirementDate")),
                                   "openDemand": c.get("OpenDemandQty"), "shortageQty": c.get("ShortageQty"),
                                   "shortageStatus": c.get("ShortageStatus"), "excludeReason": c.get("ExcludeReason") or None})
        total = sum(p["unrestricted"] or 0 for p in positions)
        open_demand = sum(d["openDemand"] or 0 for d in demand)
        unit = master["baseUnit"]
        proc = {"F": "External procurement", "E": "In-house production", "X": "Both"}.get(master["procurementType"], master["procurementType"])
        lines = [
            f"**Material {master['material']}** - *{master['description']}*",
            f"Type {master['materialType']} | Group {master['materialGroup']} | Unit {unit} | MRP type {master['mrpType'] or '-'} "
            f"| MRP ctrl {master['mrpController'] or '-'} | {proc or '-'} | Planned delivery {master['plannedDeliveryDays'] or '-'} d "
            f"| Std price {fmt(master['standardPrice'])} {master['currency'] or ''} / {master['priceUnit'] or 1}",
            "",
            f"**Stock: {fmt(total)} {unit} unrestricted across {len(positions)} location(s)**",
        ]
        lines += _md_table(["SLoc", "Name", "Unrestricted", "Blocked", "QI", "Aging", "Control status"],
                           [[p["storageLocation"], p["storageLocationName"], fmt(p["unrestricted"]), fmt(p["blocked"]),
                             fmt(p["qualityInspection"]), f"{p['agingDays']} d" + (" (AGED)" if p["isAged"] else ""),
                             p["controlStatus"]] for p in positions])
        lines += ["", f"**Demand: {len(demand)} work-order line(s), open demand {fmt(open_demand)} {unit}**"]
        lines += _md_table(["Work order", "SLoc", "Req. date", "Open demand", "Short", "Status"],
                           [[d["workOrder"], d["storageLocation"] or "-", d["requirementDate"], fmt(d["openDemand"]),
                             fmt(d["shortageQty"]), d["excludeReason"] or d["shortageStatus"]] for d in demand])
        short_orders = [d for d in demand if (d["shortageQty"] or 0) > 0]
        aged_pos = [p for p in positions if p["isAged"]]
        actions = []
        if short_orders:
            actions.append(f"{len(short_orders)} work order(s) are short of this material: "
                           + ", ".join(d["workOrder"] for d in short_orders) + ".")
        if aged_pos:
            actions.append("Use aged stock first: " + ", ".join(
                f"{p['storageLocation']} ({fmt(p['unrestricted'])} {unit}, {p['agingDays']} days)" for p in aged_pos) + ".")
        cover = "covers" if total >= open_demand else "does NOT cover"
        return Section("material_report", f"Material {master['material']}", "\n".join(lines),
                       data={"master": master, "stock": positions, "demand": demand,
                             "totals": {"unrestricted": total, "openDemand": open_demand}},
                       rows=[{"Material": master["material"], **p} for p in positions],
                       headline=" ".join(filter(None, [
                           f"{master['description']} ({master['material']}) has {fmt(total)} {unit} in stock across "
                           f"{_plural(len(positions), 'storage location')}"
                           + (f", most of it in {max(positions, key=lambda p: p['unrestricted'] or 0)['storageLocation']}."
                              if len(positions) > 1 else "."),
                           (f"Open work orders need {fmt(open_demand)} {unit}, so the stock "
                            + ("is enough to cover them." if total >= open_demand else
                               f"is not enough - {fmt(open_demand - total)} {unit} more is needed.")) if open_demand
                           else "No open work order needs it right now.",
                           (f"{_plural(len(short_orders), 'work order')} {'is' if len(short_orders) == 1 else 'are'} "
                            f"short of it: {_names([d['workOrder'] for d in short_orders], limit=4)}.") if short_orders else "",
                           (f"Some of this stock is older than {self.aging_threshold} days (in "
                            + _names([p["storageLocation"] for p in aged_pos]) + "), so use it first.") if aged_pos else "",
                           ({"F": "It is bought from a supplier", "E": "It is made in-house",
                             "X": "It can be bought or made in-house"}.get(master["procurementType"], "It has")
                            + f" with a planned delivery time of {master['plannedDeliveryDays']} days."
                            if master["plannedDeliveryDays"] else ""),
                       ])),
                       actions=actions,
                       severity="critical" if total < open_demand else "warning" if short_orders or aged_pos else "ok")

    def machine_head_review(self, orders: set[str] = frozenset()) -> Section:
        rows = []
        for wo in self.repo.shortage_by_order.values():
            if orders and wo["ManufacturingOrder"] not in orders:
                continue
            for c in nav(wo, "to_ComponentShortage"):
                if rules.needs_machine_head_review(wo["ManufacturingOrderType"], c):
                    rows.append({"WorkOrder": wo["ManufacturingOrder"], "OrderType": wo["ManufacturingOrderType"],
                                 "Component": c["Material"], "Description": c.get("ProductDescription"),
                                 "ActualUsage": c.get("ActualUsage"), "RequiredQty": c.get("RequiredQuantity"),
                                 "BaseUnit": c.get("BaseUnit"), "ExcludeReason": c.get("ExcludeReason") or "",
                                 "Status": "Needs review"})
        lines = [f"**{len(rows)} machine-head material candidate(s) - NEEDS REVIEW**", "",
                 "_Surfaced from ZNPC orders with moulding/painting materials already issued, or fixed-qty "
                 "machine-head lines. No deterministic rule exists; verify GR/GI material documents before deducting._", ""]
        lines += _md_table(["Work order", "Type", "Component", "Description", "Issued", "Required"],
                           [[r["WorkOrder"], r["OrderType"], r["Component"], r["Description"],
                             f"{fmt(r['ActualUsage'])} {r['BaseUnit']}", fmt(r["RequiredQty"])] for r in rows])
        return Section("machine_head_review", "Machine-head material review", "\n".join(lines),
                       data={"candidates": rows}, rows=rows, result_type="needs_review",
                       headline=(f"{_plural(len(rows), 'material line')} may be machine-head material: "
                                 + _names([f"{r['Description']} on work order {r['WorkOrder']}" for r in rows])
                                 + ". The system cannot decide this automatically, so someone should check the goods "
                                   "receipt and issue documents for each one before anything is deducted."
                                 if rows else "There is no material that looks like machine-head material."),
                       actions=(["Compare GR/GI material documents for each line before deducting prior issuance - "
                                 "the system does not decide this automatically."] if rows else []),
                       severity="warning" if rows else "ok")

    def help(self) -> Section:
        sample = sorted(self.repo.shortage_by_order)[:1] or ["<number>"]
        mat = next(iter(sorted(self.repo.material_master)), "<material>")
        text = "\n".join([
            f"For {self._plant_text()} I can answer:",
            f"- Work order shortage + aging + over-issue: *\"What is short for work order {sample[0]}, and what is aged?\"*",
            "- Shortage overview: *\"Which work orders are short?\"* (filter by MRP controller e.g. `ZMA`, or type `ZNPC`)",
            f"- Aged materials (> {self.aging_threshold} days): *\"Show aged material in A301\"*",
            "- Over-issued WIP incl. Z67/Z68: *\"Any over-issued materials?\"*",
            "- Line-side control status: *\"Show over-control / non-controlled material\"*",
            f"- Production order details: *\"Production order status {sample[0]}\"*",
            f"- Material master + inventory: *\"Material {mat}\"*",
            "- Machine-head candidates: *\"Show machine-head material needing review\"*",
            "Add **export** / **CSV** to any question to receive a CSV file.",
        ])
        headline = (f"I can tell you which materials are short for a work order and why, which stock has not been used "
                    f"for more than {self.aging_threshold} days, where material was issued beyond what an order needs, "
                    f"the status of a production order, and how much stock there is of a material and where. "
                    f"For example, ask \"What is short for work order {sample[0]}, and what is aged?\" or "
                    f"\"Any over-issued materials?\". Each answer can be downloaded as a report.")
        return Section("help", "What I can help with", text, result_type="info", headline=headline)

    # ================================================================ misc
    def _order_not_found(self, skill: str, order: str) -> Section:
        rng = self.repo.order_range
        span = f" - work orders available right now run from {rng[0]} to {rng[1]}" if rng else ""
        return Section(skill, f"Work order {order}",
                       f"Work order **{order}** was not found in the {self._plant_text()} production order universe. "
                       "Please check the number.",
                       data={"workOrder": order}, result_type="not_found",
                       headline=f"I could not find work order {order}. Please check the number{span}.",
                       actions=["Check the number" + (f" - valid orders in S43 run from {rng[0]} to {rng[1]}" if rng else "") + "."],
                       severity="info")

    def _plant_text(self) -> str:
        from app.config import PLANT, SITE_NAME
        return f"{SITE_NAME} (plant {PLANT})"


# ==================================================================== formatting helpers
def _d(value: str | None) -> str | None:
    return value[:10] if value else None


def _count(n: int) -> str:
    """Small counts as words at the start of a sentence ("Two of ...")."""
    words = ["None", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"]
    return words[n] if 0 <= n < len(words) else str(n)


def _names(items: list[str], limit: int = 3) -> str:
    """'A, B and C' - longer lists end with 'and N more'."""
    if len(items) > limit:
        return ", ".join(items[:limit]) + f" and {len(items) - limit} more"
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _md_table(headers: list[str], rows: list[list]) -> list[str]:
    if not rows:
        return ["_No matching records._"]
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows[:MAX_TABLE_ROWS]:
        out.append("| " + " | ".join(_cell(v) for v in r) + " |")
    if len(rows) > MAX_TABLE_ROWS:
        out.append(f"_... {len(rows) - MAX_TABLE_ROWS} more rows - ask to **export CSV** for the full list._")
    return out


def _cell(v) -> str:
    if v is None or v == "":
        return "-"
    return str(v).replace("|", "/").replace("\n", " ")


def _flat(c: dict) -> dict:
    out = {}
    for k, v in c.items():
        if k == "agedStock":
            out[k] = "; ".join(f"{a['storageLocation']}:{a['unrestricted']}@{a['agingDays']}d" for a in v)
        elif k == "z67z68Adjustment":
            out[k] = v["quantity"] if v else 0
        elif isinstance(v, list):
            out[k] = "; ".join(map(str, v))
        else:
            out[k] = v
    return out


def _filters(mrp=frozenset(), order_types=frozenset(), materials=frozenset(), slocs=frozenset(), orders=frozenset()) -> dict:
    return {k: sorted(v) for k, v in {"mrpController": mrp, "orderType": order_types, "material": materials,
                                       "storageLocation": slocs, "workOrder": orders}.items() if v}


def _filter_text(**kw) -> str:
    f = _filters(**kw)
    return (" (filter: " + "; ".join(f"{k}={', '.join(v)}" for k, v in f.items()) + ")") if f else ""


def _scope(**kw) -> str:
    """Filters as plain words for answer paragraphs, e.g. ' in storage location A301'."""
    words = {"mrpController": "for planner", "orderType": "of order type", "material": "for material",
             "storageLocation": "in storage location", "workOrder": "on work order"}
    return "".join(f" {words[k]} {_names(v)}" for k, v in _filters(**kw).items())


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{fmt(n)} {word if n == 1 else plural or word + 's'}"
