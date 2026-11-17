"""A2A agent card of the Planner agent.

Served at GET /a2a/planner/.well-known/agent-card.json (also /a2a/planner). Same layout as the other
Smart Material agents (supportedInterfaces, per-skill input/output modes). Add a skill to SKILLS when a
new planner skill is added.
"""
from app import config
from app.a2a.server import PROTOCOL_VERSION

PLANNER_PATH = "/a2a/planner"

INPUT_MODES = ["application/json", "text/plain"]
OUTPUT_MODES = ["application/json", "text/plain", "text/csv",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"]

DESCRIPTION = (
    f"Answers planner questions for {config.SITE_NAME} (plant {config.PLANT}) from live SAP S43: work order "
    f"material shortage with root cause, aged material (> {config.AGING_THRESHOLD_DAYS} days), over-issued WIP "
    "incl. Z67/Z68 adjustments, production order status, material master, inventory and the S43 Excel reports "
    "(stock list, WIP issuable, 14C, PP134, COOIS). Business rules are deterministic; every answer carries a "
    "data-as-of timestamp."
)

# id, name, description, tags, examples - input/output modes are added in planner_card().
SKILLS: list[dict] = [
    {"id": "work_order_shortage", "name": "Work order shortage",
     "description": "Shortage status per component for a work order with root cause, aged stock, "
                    "over-issued WIP and Z67/Z68 adjustments. Params: workOrder.",
     "tags": ["shortage", "work order", "aging", "over-issue"],
     "examples": ["What is short for work order 121060001, and what is aged?"]},
    {"id": "shortage_overview", "name": "Shortage overview",
     "description": "All work orders with missing parts. Filters: mrpController, orderType, material.",
     "tags": ["shortage"], "examples": ["Which work orders are short for MRP controller ZMA?"]},
    {"id": "aged_materials", "name": "Aged materials",
     "description": f"Stock with aging days > {config.AGING_THRESHOLD_DAYS}. Filters: material, storageLocation.",
     "tags": ["aging", "inventory"], "examples": ["Show aged material in A301"]},
    {"id": "over_issued", "name": "Over-issued WIP",
     "description": "Components issued beyond theoretical requirement, classified using Z67/Z68 adjustments.",
     "tags": ["over-issue", "WIP", "Z67", "Z68"], "examples": ["Any over-issued materials?"]},
    {"id": "material_status", "name": "Line-side control status",
     "description": "Available-to-issue = unrestricted - controlled remaining issue; flags over-control "
                    "and non-controlled material.",
     "tags": ["over-control", "non-controlled"], "examples": ["Show non-controlled material in A201"]},
    {"id": "production_order", "name": "Production order",
     "description": "Production order header, status, operations and components. Params: workOrder.",
     "tags": ["production order"], "examples": ["Production order status 121060010"]},
    {"id": "material_report", "name": "Material master & inventory",
     "description": "Material master data, stock by storage location with aging, and work-order demand.",
     "tags": ["material master", "inventory"], "examples": ["Where is material MTINS29300 and how much is available?"]},
    {"id": "machine_head_review", "name": "Machine-head review",
     "description": "Candidate machine-head material lines - always marked 'needs review'.",
     "tags": ["machine-head", "review"], "examples": ["Show machine-head material needing review"]},
    # Excel-based S43 reports (SMC_OData_V4_URLs.xlsx)
    {"id": "stock_list", "name": "Stock list (aged inventory export)",
     "description": "Z_AGEDINV4 stock per storage location and material with aging and all stock categories. "
                    "Filters: material, storageLocation.",
     "tags": ["inventory", "aging"], "examples": ["Show the stock list for A201"]},
    {"id": "issuable_stock", "name": "WIP issuable list",
     "description": "SME WIP issuable list: stock vs. controlled quantity still to issue (AV / OC / NC), "
                    "incl. over-control, put-away pending and '-S' views.",
     "tags": ["issuable", "over-control", "putaway"], "examples": ["Which stock is waiting for put-away?"]},
    {"id": "wo_component_remaining", "name": "Work-order components remaining",
     "description": "Z_AGEDINV4 sheet 2: BOM quantity, usage and remaining quantity per order component. "
                    "Params: workOrder, material, storageLocation, mrpController, orderType.",
     "tags": ["work order", "components"], "examples": ["Remaining quantity to issue for order 121040317"]},
    {"id": "component_issuance_14c", "name": "14C component issuance",
     "description": "PP-GP-014C-NSU: issued (CO11N / other) vs. BOM quantity per order component.",
     "tags": ["14C", "issuance"], "examples": ["Show the 14C issuance for order 120921757"]},
    {"id": "issuance_summary_14c", "name": "14C need vs. stock",
     "description": "PP-GP-014C-NSU summary: need, stock and issuable per storage location and material.",
     "tags": ["14C", "issuable"], "examples": ["14C summary for A201"]},
    {"id": "order_master", "name": "PP134 order master",
     "description": "PP-1341_1 order master: order type, issuance location, material, MRP controller, "
                    "order/yield/scrap quantity, dates and work center. "
                    "Params: workOrder, material, storageLocation, mrpController, orderType.",
     "tags": ["PP134", "order master"], "examples": ["Show the PP134 order master for MRP controller ZDD"]},
    {"id": "order_user_status", "name": "COOIS user status",
     "description": "User status per work order (NMVT = excluded from the material checks).",
     "tags": ["COOIS", "NMVT"], "examples": ["Which orders have user status NMVT?"]},
]


def planner_card() -> dict:
    return {
        "name": "Planner Agent - Smart Material AI Chat",
        "description": DESCRIPTION,
        "supportedInterfaces": [
            {
                "url": config.PUBLIC_BASE_URL + PLANNER_PATH,
                "protocolBinding": "JSONRPC",
                "protocolVersion": PROTOCOL_VERSION,
            }
        ],
        "version": "1.0.0",
        "capabilities": {
            "streaming": True,          # message/stream is supported
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": INPUT_MODES,
        "defaultOutputModes": OUTPUT_MODES,
        "skills": [{**skill, "inputModes": INPUT_MODES, "outputModes": OUTPUT_MODES} for skill in SKILLS],
    }
