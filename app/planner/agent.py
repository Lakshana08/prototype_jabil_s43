"""Planner agent: routes a question to service skills and assembles one consolidated answer."""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from dataclasses import dataclass, field

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from app.data.repository import LiveSource, begin_request
from app.llm.aicore import LLMError
from app.planner import intents, rules
from app.planner.live import LiveService
from app.planner.service import Section

log = logging.getLogger(__name__)

# Structured (DataPart) requests can name a skill directly instead of free text.
SKILLS = {
    "work_order_shortage", "shortage_overview", "aged_materials", "over_issued",
    "material_status", "production_order", "material_report", "machine_head_review", "help",
}


# Backend data sources each skill reads, and the rule it applies - shown to the user as agent steps.
SOURCES = {
    "shortage": "Work Order Shortage",
    "production": "Production Order",
    "master": "Material Master",
    "inventory": "Inventory",
}
# Per skill: the data sources it reads, then the checks it runs as (title, while running, detail).
SKILL_TRACE = {
    "work_order_shortage": (["shortage", "production", "inventory"], [
        ("Checked material shortage", "Checking material shortage",
         "Compared each component's open demand with line-side and warehouse stock"),
        ("Checked stock age", "Checking stock age", "Flagged stock not used for more than {T} days"),
        ("Checked over-issue", "Checking over-issue",
         "Compared issued with required quantity and matched Z67/Z68 adjustments"),
    ]),
    "shortage_overview": (["shortage"], [
        ("Checked material shortage", "Checking material shortage",
         "Looked for components whose demand is not covered by stock"),
    ]),
    "aged_materials": (["inventory", "shortage"], [
        ("Checked stock age", "Checking stock age", "Flagged stock not used for more than {T} days"),
        ("Checked open demand", "Checking open demand", "Checked whether open work orders still need the aged stock"),
    ]),
    "over_issued": (["shortage"], [
        ("Checked over-issue", "Checking over-issue",
         "Compared issued with required quantity, skipped bulk and dummy parts, matched Z67/Z68 adjustments"),
    ]),
    "material_status": (["inventory", "shortage"], [
        ("Checked line-side control status", "Checking line-side control status",
         "Compared stock on the production line with the quantity still to be issued"),
    ]),
    "production_order": (["production"], [
        ("Checked order status", "Checking order status", "Read quantities, schedule, operations and status"),
    ]),
    "material_report": (["master", "inventory", "shortage"], [
        ("Combined the material view", "Combining the material view",
         "Joined material details, stock by location and open work-order demand"),
    ]),
    "machine_head_review": (["shortage"], [
        ("Screened for machine-head material", "Screening for machine-head material",
         "Looked at non-production orders with resin, paint or ink already issued"),
    ]),
}


@dataclass
class AgentReply:
    text: str
    data: dict
    files: list[tuple[str, str, bytes]] = field(default_factory=list)  # (filename, mime type, content)


class PlannerAgent:
    name = "planner"

    def __init__(self, source: LiveSource, aging_threshold_days: int, llm=None):
        # Live S43 access: every question reads only the records it needs, nothing is kept in memory.
        self.source = source
        self.service = LiveService(source, aging_threshold_days)
        # Optional LLMPlanner (GPT-4o on SAP AI Core); None -> keyword router + answer templates.
        self.llm = llm

    # ------------------------------------------------------------------ entry point
    def handle(self, text: str, structured: dict | None = None, context_id: str | None = None) -> AgentReply:
        structured = structured or {}
        begin_request()  # start the list of S43 reads that this answer's "data as of" line reports
        # Free-text questions go to the LLM; structured skill calls (e.g. from Joule) stay deterministic.
        llm_failed = False
        if self.llm is not None and (text or "").strip() and not structured.get("skill"):
            try:
                answer, sections, query, calls = self.llm.answer(text, context_id)
                if structured:
                    _merge_structured(query, structured, self.source)
                return self._reply(answer, sections, query, llm_calls=calls)
            except LLMError:
                log.exception("LLM unavailable - answering with the rule-based router")
                llm_failed = True

        query = intents.parse(text or "", self.source)
        _merge_structured(query, structured, self.source)
        sections = self._route(query, structured.get("skill"))
        body = "\n\n---\n\n".join(f"### {s.title}\n{_summary_md(s)}{s.text}" for s in sections)
        if query.unknown_orders:
            body = (f"Work order(s) {', '.join(query.unknown_orders)} not found in the production order universe.\n\n"
                    + body)
        reply = self._reply(body, sections, query)
        reply.data["llmFallback"] = llm_failed
        return reply

    def _reply(self, body: str, sections: list[Section], query: intents.ParsedQuery,
               llm_calls: list[dict] | None = None) -> AgentReply:
        provenance = self.source.provenance()
        footer = (f"\n\n_Data as of {provenance['dataAsOf']} - {provenance['dataSource']}. "
                  f"Aging threshold {self.service.aging_threshold} days._")

        page = next((c["page"] for c in llm_calls or [] if c.get("page", {}).get("has_more")), None) \
            or next((c["page"] for c in llm_calls or [] if c.get("page", {}).get("skip")), None)
        if page:
            last = page["skip"] + page["returned"]
            footer += f"\n\n_Showing rows {page['skip'] + 1}-{last} of {page['count']:,}._"

        files = []
        with_rows = [s for s in sections if s.rows]
        if query.export_format == "xlsx" and with_rows:
            name = f"{with_rows[0].skill}.xlsx" if len(with_rows) == 1 else "planner_report.xlsx"
            files.append((name, XLSX_MIME, _to_xlsx(with_rows)))
        elif query.export_format == "csv":
            # BOM so Excel opens UTF-8 (Chinese status labels) correctly
            files += [(f"{s.skill}.csv", "text/csv", ("\ufeff" + _to_csv(s.rows)).encode("utf-8")) for s in with_rows]
        if files:
            footer += f"\n\n_Attached: {', '.join(n for n, _, _ in files)}._"

        data = {
            "agent": self.name,
            "query": query.as_dict(),
            "results": [{"skill": s.skill, "title": s.title, "resultType": s.result_type, "severity": s.severity,
                         "headline": s.headline, "actions": s.actions, "rowCount": len(s.rows), "data": s.data}
                        for s in sections],
            # Flat, top level: the UI's "Show next 20" button only needs this one value (null on the last page).
            "next_page_prompt": page.get("next_page_prompt") if page else None,
            "trace": self._trace(query, sections, provenance, llm_calls),
            "answeredBy": "llm" if llm_calls is not None else "rules",
            **provenance,
        }
        log.info("planner answeredBy=%s skills=%s entities=%s", data["answeredBy"],
                 [s.skill for s in sections], query.as_dict())
        return AgentReply(text=body + footer, data=data, files=files)

    # ------------------------------------------------------------------ agent steps
    def _trace(self, q: intents.ParsedQuery, sections: list[Section], provenance: dict,
               llm_calls: list[dict] | None = None) -> list[dict]:
        """What the agent did, in order: understand the question, read each data source, apply rules, answer."""
        asked = {"work_orders": "work order", "materials": "material", "storage_locations": "storage location",
                 "mrp_controllers": "planner", "order_types": "order type"}
        found = [f"{label} {', '.join(getattr(q, attr))}" for attr, label in asked.items() if getattr(q, attr)]
        topics = [t.replace("_", " ") for t in q.intents]
        steps = [{"title": "Understood the question", "active": "Reading your question", "source": "Planner Agent",
                  "kind": "plan",
                  "detail": "; ".join(filter(None, [("Topics: " + ", ".join(topics)) if topics else "",
                                                    ("Looking for " + ", ".join(found)) if found else ""]))
                  or "General question"}]
        if llm_calls is not None:
            steps[0].update(source=LLM_SOURCE, detail="; ".join(_describe_call(c) for c in llm_calls)
                            or "No S43 lookup needed - replied directly or asked for clarification")
            if q.unknown_orders:
                steps[0]["detail"] += f" (not found in S43: {', '.join(q.unknown_orders)})"
        # Work-order checks the LLM limited to some topics: only list the checks that actually ran.
        wo_topics = {t for c in llm_calls or [] if c.get("tool") == "work_order_shortage"
                     for t in (c.get("args") or {}).get("topics") or []}
        skip = {title for topic, title in _WO_TOPIC_CHECKS.items() if wo_topics and topic not in wo_topics}
        read, checks = [], {}
        for s in sections:
            keys, rule_steps = SKILL_TRACE.get(s.skill, ([], []))
            read += [k for k in keys if k not in read]
            for title, active, detail in rule_steps:
                if s.skill == "work_order_shortage" and title in skip:
                    continue
                checks.setdefault(title, (active, detail.replace("{T}", str(self.service.aging_threshold))))
        for k in read:
            steps.append({"title": f"Read {SOURCES[k]} data", "active": f"Reading {SOURCES[k]} data",
                          "source": "SAP S43", "kind": "data",
                          "detail": self._scope(k, q, {s.skill for s in sections})})
        for title, (active, detail) in checks.items():
            steps.append({"title": title, "active": active, "source": "Business rules", "kind": "rule",
                          "detail": detail})
        steps.append({"title": "Prepared the answer", "active": "Preparing the answer",
                      "source": LLM_SOURCE if llm_calls is not None else "Answer templates",
                      "kind": "answer", "detail": "Summarised the findings and recommended next actions"})
        return steps

    @staticmethod
    def _scope(source: str, q: intents.ParsedQuery, skills: set[str]) -> str:
        """Which slice of a data source the question needed."""
        orders = ", ".join(q.work_orders)
        slocs = f" in storage location {', '.join(q.storage_locations)}" if q.storage_locations else ""
        if source == "shortage" and not orders and skills & {"aged_materials", "material_status"}:
            # these skills only read work orders to see whether the stock is still needed
            what = "the aged stock" if "aged_materials" in skills else "line-side stock"
            return f"Open work-order demand for {what}{slocs}"
        if source == "shortage":
            if orders:
                return f"Component lines for work order {orders}"
            if q.mrp_controllers:
                return f"Open work orders for MRP controller {', '.join(q.mrp_controllers)}"
            if q.order_types:
                return f"Open work orders of type {', '.join(q.order_types)}"
            if q.materials:
                return f"Open work orders using {', '.join(q.materials)}"
            return "All open work orders"
        if source == "production":
            return f"Header, operations and components for {orders}" if orders else "Production order headers"
        if source == "inventory":
            if q.materials:
                return f"Stock for material {', '.join(q.materials)}"
            if q.storage_locations:
                return f"Stock in storage location {', '.join(q.storage_locations)}"
            if orders:
                return "Stock for the order's components by storage location"
            return "Stock by material and storage location"
        if source == "master":
            return f"Material details for {', '.join(q.materials)}" if q.materials else "Material details"
        return ""

    # ------------------------------------------------------------------ routing
    def _route(self, q: intents.ParsedQuery, skill: str | None) -> list[Section]:
        svc = self.service
        mats, slocs = set(q.materials), set(q.storage_locations)
        mrp, types, orders = set(q.mrp_controllers), set(q.order_types), set(q.work_orders)

        if skill:
            return [self._run_skill(skill, q)]
        if "help" in q.intents and not (orders or mats):
            return [svc.help()]

        sections: list[Section] = []
        intents_ = set(q.intents)

        # Work order(s) named: the consolidated planner view (shortage + aged + over-issued) per order.
        if orders:
            wants_po = "production_order" in intents_
            wants_wo_view = bool(intents_ & {"shortage", "aged", "over_issued"}) or not intents_ - {"material"}
            focus = intents_ & set(svc.WO_TOPICS)
            for o in q.work_orders:
                if wants_wo_view or not wants_po:
                    sections.append(svc.work_order_shortage(o, focus))
                if wants_po:
                    sections.append(svc.production_order(o))
            if "machine_head" in intents_:
                sections.append(svc.machine_head_review(orders))
            if "material_status" in intents_:
                sections.append(svc.material_status(mats, slocs))
            return sections

        if "shortage" in intents_:
            sections.append(svc.shortage_overview(mrp, types, mats))
        if "aged" in intents_:
            sections.append(svc.aged_materials(mats, slocs))
        if "over_issued" in intents_:
            sections.append(svc.over_issued(orders, mrp, types, mats))
        if "material_status" in intents_:
            sections.append(svc.material_status(mats, slocs, _status_filter(q.text)))
        if "machine_head" in intents_:
            sections.append(svc.machine_head_review())
        if mats and (not sections or "material" in intents_ or "production_order" in intents_):
            sections += [svc.material_report(m) for m in q.materials]
        if not sections and (slocs or "material" in intents_):
            sections.append(svc.material_status(mats, slocs))
        if not sections and (mrp or types):
            sections.append(svc.shortage_overview(mrp, types, mats))
        if not sections:
            sections.append(svc.help())
        return sections

    def _run_skill(self, skill: str, q: intents.ParsedQuery) -> Section:
        svc = self.service
        mats, slocs = set(q.materials), set(q.storage_locations)
        mrp, types, orders = set(q.mrp_controllers), set(q.order_types), set(q.work_orders)
        if skill not in SKILLS:
            raise ValueError(f"Unknown skill '{skill}'. Supported: {sorted(SKILLS)}")
        if skill in {"work_order_shortage", "production_order"}:
            if not q.work_orders:
                raise ValueError(f"Skill '{skill}' requires 'workOrder'")
            return getattr(svc, skill)(q.work_orders[0])
        if skill == "material_report":
            if not q.materials:
                raise ValueError("Skill 'material_report' requires 'material'")
            return svc.material_report(q.materials[0])
        return {
            "shortage_overview": lambda: svc.shortage_overview(mrp, types, mats),
            "aged_materials": lambda: svc.aged_materials(mats, slocs),
            "over_issued": lambda: svc.over_issued(orders, mrp, types, mats),
            "material_status": lambda: svc.material_status(mats, slocs, _status_filter(q.text)),
            "machine_head_review": lambda: svc.machine_head_review(orders),
            "help": svc.help,
        }[skill]()


LLM_SOURCE = "LLM - GPT-4o on SAP AI Core"
_WO_TOPIC_CHECKS = {"shortage": "Checked material shortage", "aged": "Checked stock age",
                    "over_issued": "Checked over-issue"}
_CALL_LABELS = {
    "work_order_shortage": "Shortage check", "production_order": "Production order status",
    "shortage_overview": "Shortage overview", "aged_materials": "Aged stock", "over_issued": "Over-issue check",
    "material_status": "Line-side control status", "material_report": "Material report",
    "machine_head_review": "Machine-head review", "attach_report": "Attach file",
}
_ARG_LABELS = {
    "work_order": "work order", "work_orders": "work orders", "material": "material", "materials": "materials",
    "storage_locations": "storage location", "mrp_controllers": "MRP controller", "order_types": "order type",
    "topics": "topics", "status": "status", "format": "format",
}


def _describe_call(call: dict) -> str:
    """'Shortage check (work order 121060001)' - what the LLM decided to look up."""
    args = []
    for key, value in (call.get("args") or {}).items():
        if key == "skip" or value in (None, "", []):
            continue
        value = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
        args.append(f"{_ARG_LABELS.get(key, key)} {value.replace('_', ' ')}")
    label = _CALL_LABELS.get(call.get("tool"), str(call.get("tool")).replace("_", " "))
    return f"{label} ({'; '.join(args)})" if args else label


def _merge_structured(q: intents.ParsedQuery, s: dict, source: LiveSource) -> None:
    """Fold DataPart parameters (e.g. sent by Joule after its own NLU) into the parsed query."""
    def as_list(v):
        return [v] if isinstance(v, str) else list(v or [])

    for wo in as_list(s.get("workOrder")):
        wo = str(wo)
        if wo in q.work_orders or wo in q.unknown_orders:
            continue
        (q.work_orders if source.find_order(wo) else q.unknown_orders).append(wo)
    q.materials += [m.upper() for m in as_list(s.get("material")) if m.upper() not in q.materials]
    q.storage_locations += [x.upper() for x in as_list(s.get("storageLocation")) if x.upper() not in q.storage_locations]
    q.mrp_controllers += [x for x in as_list(s.get("mrpController")) if x not in q.mrp_controllers]
    q.order_types += [x.upper() for x in as_list(s.get("orderType")) if x.upper() not in q.order_types]
    fmt = str(s.get("format") or "").lower()
    if fmt in ("xlsx", "excel"):
        q.export_format = "xlsx"
    elif fmt == "csv" or (s.get("export") and not q.export_format):
        q.export_format = "csv"


def _summary_md(s: Section) -> str:
    """Plain-language answer, placed before the detail tables."""
    if not s.headline:
        return ""
    return f"**Answer:** {s.headline}\n\n**Details:**\n\n"


def _status_filter(text: str) -> str | None:
    low = (text or "").lower()
    if any(w in low for w in ["non-controlled", "non controlled", "uncontrolled"]):
        return rules.STATUS_NON_CONTROLLED
    if any(w in low for w in ["over-control", "over control", "overcontrol"]):
        return rules.STATUS_OVER_CONTROL
    return None


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _headers(rows: list[dict]) -> list[str]:
    headers: list[str] = []
    for r in rows:
        headers += [k for k in r if k not in headers]
    return headers


def _to_xlsx(sections: list[Section]) -> bytes:
    """One sheet per answer section: bold frozen header, filters, columns sized to their content."""
    wb = Workbook()
    wb.remove(wb.active)
    for s in sections:
        ws = wb.create_sheet((s.title or s.skill)[:31].translate(str.maketrans("", "", "[]:*?/\\")))
        headers = _headers(s.rows)
        ws.append(headers)
        for r in s.rows:
            ws.append([v if v is None or isinstance(v, (str, int, float, datetime)) else str(v)
                       for v in (r.get(h) for h in headers)])
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E79")
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for i, h in enumerate(headers, 1):
            width = max([len(str(h))] + [len(str(r.get(h) or "")) for r in s.rows])
            ws.column_dimensions[get_column_letter(i)].width = min(width + 2, 60)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _to_csv(rows: list[dict]) -> str:
    headers = _headers(rows)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()
