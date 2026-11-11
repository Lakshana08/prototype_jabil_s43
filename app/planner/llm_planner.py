"""LLM layer of the Planner agent (GPT-4o on SAP AI Core).

The LLM does two things only:
1. understands the question and decides which business-rule tools to call, with which parameters;
2. writes the answer for the user from the tool results.

Every number still comes from S43 through the deterministic rules in ``PlannerService``; the LLM
never sees raw tables it could misread without the computed result, and every ID it passes is
validated against the S43 keys before a tool runs.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict, deque
from datetime import date

from app.llm.aicore import AICoreChat, LLMError
from app.planner import intents, rules
from app.planner.service import Section

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5
MAX_ROWS_TO_LLM = 40
MAX_TOOL_RESULT_CHARS = 24000
MAX_CONVERSATIONS = 500


def _arr(desc: str, enum: list[str] | None = None) -> dict:
    item = {"type": "string", **({"enum": enum} if enum else {})}
    return {"type": "array", "items": item, "description": desc}


def _tool(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required or [],
                       "additionalProperties": False}}}


WO = {"type": "string", "description": "9-digit work order / production order number, e.g. 121060001"}
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
          "Line-side control status: stock on the production line vs quantity still to issue "
          "(over-control / non-controlled / issuable).",
          {"materials": _arr("Material numbers"), "storage_locations": _arr("Storage locations"),
           "status": {"type": "string", "enum": ["over_control", "non_controlled"],
                      "description": "Only rows with this status"}}),
    _tool("material_report",
          "Everything about ONE material: master data (MRP type, procurement, lead time), stock by storage "
          "location and open work-order demand.",
          {"material": {"type": "string", "description": "Material number"}}, ["material"]),
    _tool("machine_head_review",
          "Non-production (machine-head) orders that already consumed resin, paint or ink.",
          {"work_orders": _arr("Limit to these work orders")}),
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
- If the question is ambiguous and the tools cannot answer it, ask one short clarifying question.
- If the question is outside your scope, say briefly what you can help with.

How to answer - clear and to the point for a busy planner:
- Answer in the user's language (English or Chinese).
- Structure:
  1. First paragraph: ONE sentence that directly answers the question with the key number
     (e.g. "6 of the 12 work orders for MRP controller ZMA are short of material.").
  2. Second paragraph: 1-3 short sentences that explain the situation and its cause, with the most
     important action woven into the text (e.g. "...so supply for it has to be expedited from the
     supplier"). Do not repeat facts from the first sentence.
  3. When the answer covers more than one item (orders, components, materials, operations), add a
     compact markdown table of the items that matter: never more than 5 columns (merge yes/no flags into
     one "Action" column, e.g. "Expedite from supplier" / "Move from warehouse"), max 8 rows, most
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

Context: today is {today}. Data as of {as_of}. Aged = not used for more than {threshold} days."""


class LLMPlanner:
    def __init__(self, agent, chat: AICoreChat, history_turns: int = 6):
        self.agent = agent  # PlannerAgent: gives the current service/repo (replaced on data refresh)
        self.chat = chat
        self.history_turns = history_turns
        self._history: OrderedDict[str, deque] = OrderedDict()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def answer(self, text: str, context_id: str | None):
        """Returns (answer_markdown, sections, query, tool_calls). Raises LLMError on AI Core failure."""
        svc, repo = self.agent.service, self.agent.repo
        system = SYSTEM_PROMPT.format(site=_cfg("SITE_NAME"), plant=_cfg("PLANT"), today=date.today().isoformat(),
                                      as_of=repo.data_as_of, threshold=svc.aging_threshold)
        messages = [{"role": "system", "content": system}, *self._past(context_id),
                    {"role": "user", "content": text}]
        query = intents.ParsedQuery(text=text)
        sections: list[Section] = []
        calls: list[dict] = []

        for _ in range(MAX_TOOL_ROUNDS):
            msg = self.chat.chat(messages, tools=TOOLS)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                answer = _lead_paragraph(_strip_fake_links((msg.get("content") or "").strip()))
                if not answer:
                    raise LLMError("AI Core returned an empty answer")
                self._remember(context_id, text, answer)
                return answer, sections, query, calls
            messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": tool_calls})
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self._run_tool(name, args, query, sections)
                if not (name == "attach_report" and "error" in result):  # retried after fetching the data
                    calls.append({"tool": name, "args": args})
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": json.dumps(result, ensure_ascii=False, default=str)[:MAX_TOOL_RESULT_CHARS]})
        raise LLMError(f"No answer after {MAX_TOOL_ROUNDS} tool rounds")

    # ------------------------------------------------------------------ tools
    def _run_tool(self, name: str, args: dict, q: intents.ParsedQuery, sections: list[Section]) -> dict:
        svc, repo = self.agent.service, self.agent.repo
        errors: list[str] = []

        def orders(values) -> list[str]:
            out = []
            for v in _as_list(values):
                v = str(v).strip()
                if v in repo.production_order_by_order:
                    out.append(v)
                    _add(q.work_orders, v)
                else:
                    _add(q.unknown_orders, v)
                    keys = repo.production_order_by_order
                    errors.append(f"Work order {v} not found in S43 (valid orders run from {min(keys)} to {max(keys)}).")
            return out

        def keys(values, known: set[str], label: str, target: list[str], upper=True) -> set[str]:
            lookup = {k.upper(): k for k in known}
            out = set()
            for v in _as_list(values):
                hit = lookup.get(str(v).strip().upper())
                if hit:
                    out.add(hit.upper() if upper else hit)
                    _add(target, hit.upper() if upper else hit)
                else:
                    errors.append(f"{label} {v} not found in S43.")
            return out

        mats = lambda a: keys(a.get("materials"), repo.known_materials, "Material", q.materials)  # noqa: E731
        slocs = lambda a: keys(a.get("storage_locations"), repo.known_storage_locations,  # noqa: E731
                               "Storage location", q.storage_locations)
        mrp = lambda a: keys(a.get("mrp_controllers"), repo.known_mrp_controllers,  # noqa: E731
                             "MRP controller", q.mrp_controllers, upper=False)
        types = lambda a: keys(a.get("order_types"), repo.known_order_types, "Order type", q.order_types)  # noqa: E731

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
            found = keys([args.get("material")], repo.known_materials, "Material", q.materials)
            if found:
                section = svc.material_report(next(iter(found)))
        elif name == "machine_head_review":
            section = svc.machine_head_review(set(orders(args.get("work_orders"))))
        else:
            return {"error": f"Unknown tool {name}"}

        if section is None:
            return {"error": " ".join(errors) or "Nothing found."}
        sections.append(section)
        result = _compact(section, repo.storage_location_names)
        if errors:
            result["warnings"] = errors
        return result

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


def _compact(s: Section, location_names: dict[str, str] | None = None) -> dict:
    out = {"title": s.title, "result": s.result_type, "severity": s.severity, "summary": s.headline,
           "recommended_actions": s.actions, "facts": _round(s.data), "row_count": len(s.rows),
           "rows": _round(s.rows[:MAX_ROWS_TO_LLM])}
    if len(s.rows) > MAX_ROWS_TO_LLM:
        out["rows_note"] = f"Only the first {MAX_ROWS_TO_LLM} of {len(s.rows)} rows are shown here."
    if location_names:
        # Names tell the LLM whether a location is a warehouse or a production line (e.g. M401 = line-side).
        used = _location_codes(out) & set(location_names)
        if used:
            out["storage_location_names"] = {c: location_names[c] for c in sorted(used) if location_names[c]}
    return out


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
