"""LLM layer of the Planner agent (GPT-4o on SAP AI Core).

The LLM does two things only:
1. understands the question and decides which business-rule tools to call, with which parameters;
2. writes the answer for the user from the tool results.

Every number still comes from S43 through the deterministic rules in ``PlannerService``; the LLM
never sees raw tables it could misread without the computed result, and every ID it passes is
validated against S43 before a tool runs. Each tool call reads only the records it needs from S43
(server-side filters); nothing is held in memory between questions.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict, deque
from datetime import date, datetime, timezone

from app.llm.aicore import AICoreChat, LLMError
from app.planner import intents, paging, rules
from app.planner.reports import ISSUABLE_VIEWS, clean_keys
from app.planner.service import Section

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5
MAX_TOOL_RESULT_CHARS = 24000
MAX_CONVERSATIONS = 500


def _arr(desc: str, enum: list[str] | None = None) -> dict:
    item = {"type": "string", **({"enum": enum} if enum else {})}
    return {"type": "array", "items": item, "description": desc}


def _tool(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    if name != "attach_report":
        props = {**props, "skip": SKIP}
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required or [],
                       "additionalProperties": False}}}


WO = {"type": "string", "description": "9-digit work order / production order number, e.g. 121060001"}
ANY_WO = _arr("9-digit work order numbers. These reports cover their own set of orders (e.g. 120921757), "
              "not only the production orders")
SKIP = {"type": "integer", "minimum": 0,
        "description": "Rows already shown. One page is 20 rows: 'rows 21-40' -> skip=20. Omit for the first page."}
TOOLS = [
    _tool("work_order_shortage",
          "Material situation of ONE work order: which components are short and why (no warehouse stock -> "
          "expedite supplier; stock only in warehouse -> move to line), aged stock on its components, "
          "over-issued components incl. Z67/Z68 adjustments.",
          {"work_order": WO,
           "topics": _arr("Limit to these topics; omit for all", ["shortage", "aged", "over_issued"])},
          ["work_order"]),
    _tool("production_order",
          "Header status of ONE production order: material, quantities, confirmed/scrap, schedule, "
          "operations and system status.", {"work_order": WO}, ["work_order"]),
    _tool("shortage_overview",
          "Which work orders are short of material, across the plant. Optional filters.",
          {"mrp_controllers": _arr("MRP controller codes, e.g. ZMA"),
           "order_types": _arr("Order types, e.g. ZPPC"),
           "materials": _arr("Component material numbers")}),
    _tool("aged_materials",
          "Stock not used for more than the aging threshold, with value and whether open work orders still need it.",
          {"materials": _arr("Material numbers"), "storage_locations": _arr("Storage locations, e.g. A301")}),
    _tool("over_issued",
          "Components issued beyond what work orders require, matched with Z67/Z68 adjustment postings.",
          {"work_orders": _arr("Work order numbers"), "mrp_controllers": _arr("MRP controller codes"),
           "order_types": _arr("Order types"), "materials": _arr("Material numbers")}),
    _tool("material_status",
          "Material control status per storage location + material (business rule spec 6.1/6.2, the SME's "
          "Z_AGEDINV logic, every location of the plant): Available = Unrestricted - Controlled remaining issuance; "
          "no open work-order requirement = non-controlled / uncontrolled (无管制料), < 0 = over-control (领超管制), "
          ">= 0 = available to issue (可发量). Over-control rows carry Z67/Z68 adjustment context. Use it for any "
          "count, list or yes/no question about non-controlled, over-control or available-to-issue material.",
          {"materials": _arr("Material numbers"), "storage_locations": _arr("Storage locations"),
           "status": {"type": "string", "enum": ["over_control", "non_controlled"],
                      "description": "Only rows with this status. Use for list/count questions only - for a "
                                     "yes/no question about a named material leave it out so every location "
                                     "is returned"}}),
    _tool("material_report",
          "Everything about ONE material: master data (MRP type, procurement, lead time), stock by storage "
          "location and open work-order demand.",
          {"material": {"type": "string", "description": "Material number"}}, ["material"]),
    _tool("machine_head_review",
          "Non-production (machine-head) orders that already consumed resin, paint or ink.",
          {"work_orders": _arr("Limit to these work orders")}),
    # ---- Excel-based S43 reports (SMC_OData_V4_URLs.xlsx); quantities are calculated in S43
    _tool("stock_list",
          "Full stock list from the aged-inventory export (Z_AGEDINV4): every plant / storage location / "
          "material with aging days, last placement date, unrestricted, consignment, blocked, quality-inspection "
          "and in-transfer stock and stock value. Use for 'stock list', 'inventory export', stock categories "
          "(blocked, QI, consignment) or stock older than N days.",
          {"materials": _arr("Material numbers"), "storage_locations": _arr("Storage locations"),
           "min_aging_days": {"type": "integer", "minimum": 0,
                              "description": "Only stock not used for at least this many days"}}),
    _tool("issuable_stock",
          "SME's WIP issuable list (Z_AGEDINV4): per storage location + material the stock on hand, the "
          "controlled quantity still to issue and the quantity available to issue, with status AV (available "
          "to issue), OC (over-control / over-issued) or NC (non-controlled). view: 'all' = the full list, "
          "'over_control' = the over-control sheet, 'putaway_pending' = stock waiting for put-away (receiving "
          "locations), 'suffix_s' = materials whose number ends in -S. Use it when the user asks for these SME "
          "sheets (WIP issuable list, put-away pending, -S list); for plain non-controlled / over-control / "
          "available counts and lists use material_status.",
          {"view": {"type": "string", "enum": list(ISSUABLE_VIEWS), "description": "Which list; default all"},
           "materials": _arr("Material numbers"), "storage_locations": _arr("Storage locations"),
           "statuses": _arr("Only these status codes", ["AV", "OC", "NC"])}),
    _tool("wo_component_remaining",
          "Work-order component list from the aged-inventory snapshot (Z_AGEDINV4 sheet 2): per order and "
          "component the BOM theoretical quantity, actual usage, remaining quantity to issue and the total "
          "remaining per storage location + material.",
          {"work_orders": ANY_WO, "components": _arr("Component material numbers"),
           "storage_locations": _arr("Storage locations"), "mrp_controllers": _arr("MRP controller codes"),
           "order_types": _arr("Order types, e.g. ZPPC, ZNPC, ZRWC")}),
    _tool("component_issuance_14c",
          "14C component issuance report (PP-GP-014C-NSU, SME-modified): per work order and component the "
          "quantity per, scrap %, theoretical usage, goods issued via CO11N and other movements, actual usage, "
          "BOM theoretical quantity and remaining quantity (negative = issued more than the BOM requires).",
          {"work_orders": ANY_WO, "components": _arr("Component material numbers"),
           "storage_locations": _arr("Storage locations"), "mrp_controllers": _arr("MRP controller codes"),
           "order_types": _arr("Order types")}),
    _tool("issuance_summary_14c",
          "14C summary (PP-GP-014C-NSU Sheet1): per storage location + material the open need, the stock and "
          "the issuable quantity (= need - stock; negative = more stock than the open orders need).",
          {"materials": _arr("Material numbers"), "storage_locations": _arr("Storage locations")}),
    _tool("order_user_status",
          "COOIS user status of work orders, e.g. NMVT (no material movement) - such orders are excluded "
          "from the material checks. Use for 'user status', 'COOIS', 'NMVT' or 'which orders are excluded'.",
          {"work_orders": ANY_WO, "excluded_only": {"type": "boolean", "description": "Only excluded orders"}}),
    _tool("order_master",
          "PP134 order master (PP-1341_1): one row per order with type, issuance location, material, MRP "
          "controller, order/yield/scrap quantity, start/finish/release dates and work center. Use for "
          "'PP134', 'order master' or questions on order dates / quantities / work center.",
          {"work_orders": ANY_WO, "materials": _arr("Material numbers"),
           "storage_locations": _arr("Issuance locations"), "mrp_controllers": _arr("MRP controller codes"),
           "order_types": _arr("Order types")}),
    _tool("attach_report",
          "Attach the detailed rows of the data fetched IN THIS TURN as a downloadable file. Always call the "
          "data tool(s) first in the same turn - also for a follow-up like 'give me that as Excel'. "
          "Call only when the user asks for a file, export, download, CSV or Excel.",
          {"format": {"type": "string", "enum": ["csv", "xlsx"]}}, ["format"]),
]

SYSTEM_PROMPT = """You are the Planner Agent of the Smart Material AI Chat for {site} (plant {plant}).
You help production planners and material controllers with work-order material shortage, aged stock,
over-issued material (incl. Z67/Z68 adjustment postings), line-side control status, production order
status and material master / inventory - using live data from SAP S43.

How to work:
- Always get facts by calling the tools. Never invent or estimate numbers, order numbers, materials,
  dates or values. Never say an order, material or location exists or does not exist without calling a
  tool first - the tools check every ID against S43. If a tool returns an error (e.g. order not found),
  say so plainly and answer the rest of the question with what was found.
- Call several tools if the question needs it. For follow-up questions, use the conversation so far to
  resolve references like "that order" or "those materials".
- Filters (materials, storage locations, MRP controllers, order types) are OPTIONAL. A list question without
  filters ("show non-controlled material", "any over-issued materials?", "which orders are short?") means ALL:
  call the tool with no filters. Never ask the user for a material number or location in that case.
- If the question is ambiguous and the tools cannot answer it, ask one short clarifying question.
- If the question is outside your scope, say briefly what you can help with.

How to answer - clear and to the point for a busy planner:
- Answer in the user's language (English or Chinese).
- Structure:
  1. First paragraph: ONE sentence that directly answers the question with the key number
     (e.g. "6 of the 12 work orders for MRP controller ZMA are short of material.").
     A yes/no question ("is X aged?", "is X short?", "is X non-controlled?") starts with "Yes" or "No"
     and answers only about the item asked - never replace it with a plant-wide list. If the answer differs
     by storage location, say so per location (e.g. "Yes in A340, but in A341 it is available to issue").
  2. Second paragraph: 1-3 short sentences that explain the situation and its cause, with the most
     important action woven into the text (e.g. "...so supply for it has to be expedited from the
     supplier"). Do not repeat facts from the first sentence.
  3. When the answer covers more than one item (orders, components, materials, operations), add a
     compact markdown table of the items that matter: never more than 5 columns (merge yes/no flags into
     one "Action" column, e.g. "Expedite from supplier" / "Move from warehouse"), max 8 rows (EXCEPT for a paged result, see below), most
     urgent first (needs expedite before move-from-warehouse, then earliest date).
     Use short, plain column headers and the same rounding rules as the text.
- Never add a "What to do", "Next steps", "Recommendations" or similar heading or bullet list.
- Never end with an offer or invitation ("Ask about...", "Let me know...", "Ask for details...",
  "download the report...") - the app already offers the report.
- No headings in the answer.
- Mention each relevant material once, by its description (material number in brackets the first time),
  and connect the facts into one clear story, e.g. "The resin is 11.5 KG short and there is none in the
  warehouse, so it has to come from the supplier; the 5.9 KG already on the line is 35 days old, use it
  first." Never list numbers that look contradictory without saying how they fit together.
- Overview / list questions (which work orders are short, aged stock, over-issue, ...): give the total in
  the first sentence, explain the split (e.g. expedite vs. move from warehouse) in the second paragraph,
  and list the most urgent items in the table rather than in the text.
- If the user asks for details, a breakdown, a list or "all" items, the table may have up to 15 rows.
- Paged results (has_more is true, or skip > 0): do NOT write a table - the app adds the table with all rows
  of the page itself. Write only the one-sentence answer and the short explanation, based on row_count and
  the rows of this page (say what the page contains, e.g. "rows 21-40 are all covered by adjustments").
- Tool results hold ONE page of at most 20 rows; row_count is the true total. When has_more is true, never
  present the page as the full result: base totals on row_count, say the table shows the first rows only.
  For "rows 21-40" or "next rows", call the same tool with the same filters and skip = first row - 1.
- recommended_actions in a tool result are hints: use them to explain what has to happen in the
  sentences, never copy them as a list.
- Round every quantity: KG, M, L to 1 decimal (below 1 use 2 decimals); pieces (EA, PC) as whole
  numbers with thousands separators; money as whole CNY. Always give the unit.
- Plain words: "on the production line", "in the warehouse", "order from the supplier" (expedite),
  "not used for N days" (aged), "issued more than needed" (over-issued). No SAP jargon unless asked.
- Do not mention tools, JSON, functions or internal field names.
- Never write links, URLs or file paths. When a file is attached, just say "The Excel/CSV file is attached
  below." - the app shows the download button.

Meaning of the result fields:
- remainingIssueQty: quantity the order still needs; lineSideStock: stock already on the production line;
  allocatedQty: part of the line stock counted for this order; shortageQty: still missing after that;
  warehouseStock: stock in the warehouse that could be moved to the line.
- shortageStatus/rootCause: the conclusion of the business rules - follow it.
- agedStock: stock not used for more than the threshold (location, days, quantity). It can be the same
  stock as the line-side stock already counted - check the storage location and say so.
- storage_location_names: what each location code is (warehouse vs. a production line). Use it to say
  correctly where stock is; never call a line-side location "the warehouse".
- overIssuedQty / z67z68Adjustment: issued beyond requirement and the adjustment postings for it.
- Report tools (stock_list, issuable_stock, wo_component_remaining, component_issuance_14c,
  issuance_summary_14c, order_user_status, order_master) show the SME's Excel reports as calculated in S43. Their work
  orders are a different, larger set than the production orders: never call an order "not found" because
  work_order_shortage did not know it - try the report tool. StockQty = stock on hand, StillToIssue =
  controlled quantity still to issue, AvailableToIssue = stock minus still-to-issue (negative = deficit).

Context: today is {today}. Data as of {as_of}. Aged = not used for more than {threshold} days."""


class _Unresolved(Exception):
    """Every key the user named for a filter is unknown in S43."""


class LLMPlanner:
    def __init__(self, agent, chat: AICoreChat, history_turns: int = 6):
        self.agent = agent  # PlannerAgent: gives the live S43 service and source
        self.chat = chat
        self.history_turns = history_turns
        self._history: OrderedDict[str, deque] = OrderedDict()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def answer(self, text: str, context_id: str | None):
        """Returns (answer_markdown, sections, query, tool_calls). Raises LLMError on AI Core failure."""
        svc = self.agent.service
        system = SYSTEM_PROMPT.format(site=_cfg("SITE_NAME"), plant=_cfg("PLANT"), today=date.today().isoformat(),
                                      as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC (live from S43)"),
                                      threshold=svc.aging_threshold)
        messages = [{"role": "system", "content": system}, *self._past(context_id),
                    {"role": "user", "content": text}]
        query = intents.ParsedQuery(text=text)
        requested_skip = paging.skip_from_text(text)  # "rows 21-40" -> 20, read from the text, not the model
        sections: list[Section] = []
        calls: list[dict] = []

        for _ in range(MAX_TOOL_ROUNDS):
            msg = self.chat.chat(messages, tools=TOOLS)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                answer = _lead_paragraph(_strip_fake_links((msg.get("content") or "").strip()))
                if not answer:
                    raise LLMError("AI Core returned an empty answer")
                answer = _with_page_table(answer, calls)
                if not query.export_format:  # the model sometimes claims a file that was never attached
                    answer = _FILE_CLAIM.sub("", answer).strip()
                self._remember(context_id, text, answer)
                return answer, sections, query, calls
            messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": tool_calls})
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self._run_tool(name, args, query, sections, requested_skip)
                page = result.pop("_page", None)
                if not (name == "attach_report" and "error" in result):  # retried after fetching the data
                    calls.append({"tool": name, "args": args, **({"page": page} if page else {})})
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": json.dumps(result, ensure_ascii=False, default=str)[:MAX_TOOL_RESULT_CHARS]})
        raise LLMError(f"No answer after {MAX_TOOL_ROUNDS} tool rounds")

    # ------------------------------------------------------------------ tools
    def _run_tool(self, name: str, args: dict, q: intents.ParsedQuery, sections: list[Section],
                  requested_skip: int | None = None) -> dict:
        svc, source = self.agent.service, self.agent.source
        errors: list[str] = []

        def orders(values) -> list[str]:
            out = []
            for v in _as_list(values):
                v = str(v).strip()
                if source.find_order(v):
                    out.append(v)
                    _add(q.work_orders, v)
                else:
                    _add(q.unknown_orders, v)
                    rng = source.order_range()
                    errors.append(f"Work order {v} not found in S43" + (f" (valid orders run from {rng[0]} to {rng[1]})." if rng else "."))
            return out

        def keys(values, find, label: str, target: list[str]) -> set[str]:
            out = set()
            for v in _as_list(values):
                hit = find(str(v).strip())
                if hit:
                    out.add(hit)
                    _add(target, hit)
                else:
                    errors.append(f"{label} {v} not found in S43.")
            if _as_list(values) and not out:
                # Never fall back to an unfiltered (plant-wide) answer when every requested key is unknown.
                raise _Unresolved(" ".join(errors))
            return out

        mats = lambda a: keys(a.get("materials"), source.find_material, "Material", q.materials)  # noqa: E731
        slocs = lambda a: keys(a.get("storage_locations"), source.find_storage_location,  # noqa: E731
                               "Storage location", q.storage_locations)
        mrp = lambda a: keys(a.get("mrp_controllers"), source.find_mrp_controller,  # noqa: E731
                             "MRP controller", q.mrp_controllers)
        types = lambda a: keys(a.get("order_types"), source.find_order_type, "Order type", q.order_types)  # noqa: E731

        try:
            section = self._dispatch(name, args, q, sections, svc, source, orders, keys, mats, slocs, mrp, types)
        except _Unresolved as exc:
            return {"error": str(exc) + " Say so plainly; do not answer with data for other materials or locations."}
        if isinstance(section, dict):  # attach_report / unknown tool
            return section
        if section is None:
            return {"error": " ".join(errors) or "Nothing found."}
        sections.append(section)
        skip = paging.resolve_skip(args.get("skip"), requested_skip)
        result = _compact(section, section.location_names, skip)
        meta = {k: result[k] for k in ("count", "skip", "returned", "has_more")}
        section.skip = skip
        section.next_page_prompt = paging.next_page_prompt(_page_subject(name, args), meta)
        result["_page"] = {**meta, "table": _page_table(name, paging.page_slice(section.rows, skip)),
                           "next_page_prompt": section.next_page_prompt}
        if errors:
            result["warnings"] = errors
        return result

    def _dispatch(self, name, args, q, sections, svc, source, orders, keys, mats, slocs, mrp, types):
        section: Section | None = None
        if name == "attach_report":
            if not any(sec.rows for sec in sections):
                # Files are built from the data fetched in THIS turn - earlier answers are only text.
                return {"error": "No data has been fetched in this turn yet. First call the tool that returns "
                                 "the requested data (again, even if it was shown earlier), then call attach_report."}
            q.export_format = "xlsx" if args.get("format") == "xlsx" else "csv"
            return {"ok": True, "note": "The file will appear as a download button below your answer. "
                                        "Do not write any link or file path."}
        if name in ("work_order_shortage", "production_order"):
            found = orders([args.get("work_order")])
            if found:
                if name == "work_order_shortage":
                    topics = set(args.get("topics") or []) & set(svc.WO_TOPICS)
                    section = svc.work_order_shortage(found[0], topics or None)
                else:
                    section = svc.production_order(found[0])
        elif name == "shortage_overview":
            section = svc.shortage_overview(mrp(args), types(args), mats(args))
        elif name == "aged_materials":
            section = svc.aged_materials(mats(args), slocs(args))
        elif name == "over_issued":
            section = svc.over_issued(set(orders(args.get("work_orders"))), mrp(args), types(args), mats(args))
        elif name == "material_status":
            status = {"over_control": rules.STATUS_OVER_CONTROL,
                      "non_controlled": rules.STATUS_NON_CONTROLLED}.get(args.get("status") or "")
            section = svc.material_status(mats(args), slocs(args), status)
        elif name == "material_report":
            found = keys([args.get("material")], source.find_material, "Material", q.materials)
            if found:
                section = svc.material_report(next(iter(found)))
        elif name == "machine_head_review":
            section = svc.machine_head_review(set(orders(args.get("work_orders"))))
        elif name in REPORT_TOOLS:
            section = self._run_report(name, args, q, svc)
        else:
            return {"error": f"Unknown tool {name}"}
        return section

    @staticmethod
    def _run_report(name: str, args: dict, q: intents.ParsedQuery, svc) -> Section:
        """Report tools filter S43 directly: keys are only cleaned, an unknown key simply returns no rows."""
        for key in ("work_orders", "materials", "components", "storage_locations", "mrp_controllers",
                    "order_types", "statuses"):
            if args.get(key) not in (None, "", []):
                args[key] = clean_keys(args[key])  # cleaned in place: the next-page prompt repeats them
        wos, mats = args.get("work_orders") or [], args.get("materials") or []
        comps, slocs = args.get("components") or [], args.get("storage_locations") or []
        mrp, types = args.get("mrp_controllers") or [], args.get("order_types") or []
        for target, values in ((q.work_orders, wos), (q.materials, mats + comps), (q.storage_locations, slocs),
                               (q.mrp_controllers, mrp), (q.order_types, types)):
            for v in values:
                _add(target, v)
        if name == "stock_list":
            try:
                min_age = int(args.get("min_aging_days") or 0) or None
            except (TypeError, ValueError):
                min_age = None
            return svc.stock_list(mats, slocs, min_age)
        if name == "issuable_stock":
            view = args.get("view") if args.get("view") in ISSUABLE_VIEWS else "all"
            return svc.issuable_stock(view, mats, slocs, args.get("statuses") or [])
        if name == "wo_component_remaining":
            return svc.wo_component_remaining(wos, comps or mats, slocs, mrp, types)
        if name == "component_issuance_14c":
            return svc.component_issuance_14c(wos, comps or mats, slocs, mrp, types)
        if name == "issuance_summary_14c":
            return svc.issuance_summary_14c(mats, slocs)
        if name == "order_master":
            return svc.order_master(wos, mats, slocs, mrp, types)
        return svc.order_user_status(wos, bool(args.get("excluded_only")))

    # ------------------------------------------------------------------ memory
    def _past(self, context_id: str | None) -> list[dict]:
        if not context_id:
            return []
        with self._lock:
            turns = list(self._history.get(context_id) or [])
        return [m for user, assistant in turns
                for m in ({"role": "user", "content": user}, {"role": "assistant", "content": assistant})]

    def _remember(self, context_id: str | None, question: str, answer: str) -> None:
        if not context_id or self.history_turns <= 0:
            return
        with self._lock:
            turns = self._history.setdefault(context_id, deque(maxlen=self.history_turns))
            turns.append((question, answer))
            self._history.move_to_end(context_id)
            while len(self._history) > MAX_CONVERSATIONS:
                self._history.popitem(last=False)


def _compact(s: Section, location_names: dict[str, str] | None = None, skip: int = 0) -> dict:
    """Tool result for the LLM: ONE page of rows (fixed size) plus the true total."""
    page = paging.page_slice(s.rows, skip)
    meta = paging.page_meta(len(s.rows), skip, len(page))
    out = {"title": s.title, "result": s.result_type, "severity": s.severity, "summary": s.headline,
           "recommended_actions": s.actions, "facts": _round(s.data), "row_count": len(s.rows),
           **meta, "rows": _round(page)}
    if meta["has_more"]:
        out["rows_note"] = (f"Showing rows {skip + 1}-{skip + len(page)} of {len(s.rows)}. More rows exist: say so "
                            "and do not present this page as the full result.")
    elif skip and not page:
        out["rows_note"] = f"There are only {len(s.rows)} rows; nothing after row {skip}."
    if location_names:
        # Names tell the LLM whether a location is a warehouse or a production line (e.g. M401 = line-side).
        used = _location_codes(out) & set(location_names)
        if used:
            out["storage_location_names"] = {c: location_names[c] for c in sorted(used) if location_names[c]}
    return out


REPORT_TOOLS = {"stock_list", "issuable_stock", "wo_component_remaining", "component_issuance_14c",
                "issuance_summary_14c", "order_user_status", "order_master"}

_PAGE_COLUMNS = {  # tool -> [(header, row key, unit key or None)]
    "over_issued": [("Work order", "WorkOrder", None), ("Component", "Component", None),
                    ("Description", "Description", None), ("Required", "RequiredQty", "BaseUnit"),
                    ("Actual", "ActualUsage", "BaseUnit"), ("Over-issued", "OverIssuedQty", "BaseUnit"),
                    ("Classification", "Classification", None)],
    "aged_materials": [("Material", "Material", None), ("Description", "Description", None),
                       ("Location", "StorageLocationName", None), ("Aging days", "AgingDays", None),
                       ("Quantity", "Unrestricted", "BaseUnit"), ("Value", "StockValue", "Currency")],
    "shortage_overview": [("Work order", "WorkOrder", None), ("Material", "Material", None),
                          ("Description", "Description", None), ("Start", "ScheduledStart", None),
                          ("Short components", "ShortComponents", None), ("Expedite", "NeedExpedite", None)],
    "material_status": [("Material", "Material", None), ("Description", "Description", None),
                        ("Location", "StorageLocation", None), ("Quantity", "Unrestricted", "BaseUnit"),
                        ("Aging days", "AgingDays", None), ("Still to issue", "ControlledRemainingIssueQty", "BaseUnit"),
                        ("Status", "Status", None)],
    "machine_head_review": [("Work order", "WorkOrder", None), ("Component", "Component", None),
                            ("Description", "Description", None), ("Required", "RequiredQty", "BaseUnit"),
                            ("Actual", "ActualUsage", "BaseUnit"), ("Status", "Status", None)],
    "stock_list": [("Material", "Material", None), ("Description", "Description", None),
                   ("Location", "StorageLocation", None), ("Aging days", "AgingDays", None),
                   ("Unrestricted", "Unrestricted", "BaseUnit"), ("Consignment", "UnrestrictedConsignment", "BaseUnit"),
                   ("Value", "StockValue", "Currency")],
    "issuable_stock": [("Location", "StorageLocation", None), ("Material", "Material", None),
                       ("Description", "Description", None), ("Stock", "StockQty", "BaseUnit"),
                       ("Still to issue", "StillToIssue", "BaseUnit"), ("Available", "AvailableToIssue", "BaseUnit"),
                       ("Status", "Status", None)],
    "wo_component_remaining": [("Work order", "WorkOrder", None), ("Component", "Component", None),
                               ("Location", "StorageLocation", None), ("BOM qty", "BOMTheoreticalQty", "Unit"),
                               ("Used", "ActualUsage", "Unit"), ("Remaining", "ActualRemainingQty", "Unit")],
    "component_issuance_14c": [("Work order", "WorkOrder", None), ("Component", "Component", None),
                               ("Location", "StorageLocation", None), ("BOM qty", "BOMTheoreticalQty", "Unit"),
                               ("Issued CO11N", "IssuedCO11N", "Unit"), ("Issued other", "IssuedOther", "Unit"),
                               ("Remaining", "RemainingQty", "Unit")],
    "issuance_summary_14c": [("Location", "StorageLocation", None), ("Material", "Material", None),
                             ("Need", "NeedQty", None), ("Stock", "StockQty", None), ("Issuable", "IssuableQty", None)],
    "order_master": [("Order", "WorkOrder", None), ("Type", "OrderType", None), ("Location", "StorageLocation", None),
                     ("Material", "Material", None), ("MRP", "MRPController", None), ("Order qty", "OrderQty", "Unit"),
                     ("Start", "OrderStart", None), ("Finish", "OrderFinish", None)],
    "order_user_status": [("Work order", "WorkOrder", None), ("User status", "UserStatus", None),
                          ("Excluded", "Excluded", None)],
}


def _cell(v) -> str:
    if v is None or v == "":
        return "–"  # no value (e.g. no open demand), shown as a dash rather than a blank cell
    if isinstance(v, float):
        v = round(v, 2) if abs(v) >= 1 else round(v, 3)
        return f"{v:,.3f}".rstrip("0").rstrip(".") if abs(v) < 1 else f"{v:,.2f}".rstrip("0").rstrip(".")
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}"
    return str(v).replace("|", "/").replace("\n", " ")


def _page_table(tool: str, rows: list[dict]) -> str:
    """Markdown table of EVERY row on the page, built here so the model cannot shorten it."""
    if not rows:
        return ""
    cols = _PAGE_COLUMNS.get(tool)
    if cols is None:  # generic: first scalar columns
        keys = [k for k in rows[0] if not isinstance(rows[0][k], (dict, list))][:7]
        cols = [(k, k, None) for k in keys]
    head = "| " + " | ".join(c[0] for c in cols) + " |"
    sep = "|" + "|".join(" --- " for _ in cols) + "|"
    body = []
    for r in rows:
        cells = []
        for _, key, unit in cols:
            text = _cell(r.get(key))
            if unit and text != "–" and r.get(unit):
                text += f" {r[unit]}"
            cells.append(text)
        body.append("| " + " | ".join(cells) + " |")
    return "\n".join([head, sep, *body])


def _with_page_table(answer: str, calls: list[dict]) -> str:
    """Paged answer = the model's text (any table it wrote is dropped) + the server-built page table."""
    page = next((c["page"] for c in calls if c.get("page", {}).get("has_more")), None) \
        or next((c["page"] for c in calls if c.get("page", {}).get("skip")), None)
    if not page or not page.get("table"):
        return answer
    text = "\n".join(ln for ln in answer.splitlines() if not ln.lstrip().startswith("|")).strip()
    return f"{text}\n\n{page['table']}"


_PAGE_LABELS = {
    "work_order_shortage": "shortage check", "production_order": "production order status",
    "shortage_overview": "shortage overview", "aged_materials": "aged stock", "over_issued": "over-issued material",
    "material_status": "line-side control status", "material_report": "material report",
    "machine_head_review": "machine-head review",
    "stock_list": "stock list", "issuable_stock": "WIP issuable list",
    "wo_component_remaining": "work-order components remaining", "component_issuance_14c": "14C component issuance",
    "issuance_summary_14c": "14C need vs. stock", "order_user_status": "COOIS user status",
    "order_master": "PP134 order master",
}
_PAGE_FILTERS = {
    "work_order": "work order", "work_orders": "work orders", "material": "material", "materials": "materials",
    "storage_locations": "storage location", "mrp_controllers": "MRP controller", "order_types": "order type",
    "topics": "topics", "status": "status",
    "view": "list", "statuses": "status", "components": "components", "min_aging_days": "aged at least (days)",
    "excluded_only": "excluded only",
}


def _page_subject(tool: str, args: dict) -> str:
    """'shortage overview for MRP controller ZMA, order type ZPPC' - repeats EVERY filter of the question,
    otherwise page 2 would quietly be a different query."""
    parts = []
    for key, label in _PAGE_FILTERS.items():
        value = args.get(key)
        if value in (None, "", []):
            continue
        value = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
        parts.append(f"{label} {value}")
    base = _PAGE_LABELS.get(tool, tool.replace("_", " "))
    return f"{base} for {'; '.join(parts)}" if parts else base


_FILE_CLAIM = re.compile(r"[^\n.]*\b(?:file|report)\b[^\n.]*\battached\b[^\n.]*\.?[ \t]*", re.IGNORECASE)
_FAKE_LINK = re.compile(r"\[([^\]]+)\]\((?!https?://)[^)]*\)")


def _strip_fake_links(text: str) -> str:
    """Drop markdown links that do not point to a web page (e.g. 'sandbox:/file.xlsx'); keep their text."""
    return _FAKE_LINK.sub(r"\1", text)


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"(])|(?<=[。！？])")


def _lead_paragraph(text: str) -> str:
    """Keep only the first sentence in the first paragraph (the UI shows it as the bold one-line answer)."""
    first, sep, rest = text.partition("\n\n")
    if first.lstrip().startswith(("|", "#", "-", "*")):
        return text
    parts = _SENTENCE_END.split(first.strip(), maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return text
    return parts[0].strip() + "\n\n" + parts[1].strip() + (sep + rest if sep else "")


def _round(value):
    """Round floats for the LLM (exact values stay in the Excel/CSV export)."""
    if isinstance(value, float):
        return round(value, 2) if abs(value) >= 1 else round(value, 3)
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_round(v) for v in value]
    return value


def _location_codes(value) -> set[str]:
    codes: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if k.lower() == "storagelocation" and isinstance(v, str):
                codes.add(v)
            else:
                codes |= _location_codes(v)
    elif isinstance(value, list):
        for v in value:
            codes |= _location_codes(v)
    elif isinstance(value, str) and ":" in value and "@" in value:  # "M401:4529.0@35d" in flattened rows
        codes |= {part.split(":")[0].strip() for part in value.split(",") if ":" in part}
    return codes


def _as_list(v) -> list:
    if v is None or v == "":
        return []
    return [v] if isinstance(v, (str, int)) else list(v)


def _add(target: list, value) -> None:
    if value not in target:
        target.append(value)


def _cfg(name: str) -> str:
    from app import config
    return getattr(config, name)
