"""FastAPI entry point: exposes the Planner agent over A2A at /a2a/planner."""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.a2a.server import PROTOCOL_VERSION, create_a2a_router
from app.data.repository import DataLoadError, DataRepository
from app.planner.agent import PlannerAgent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

PLANNER_PATH = "/a2a/planner"

log = logging.getLogger(__name__)

try:
    repo = DataRepository.from_config()
except DataLoadError as exc:
    # One readable line instead of a long traceback; the server cannot start without data.
    raise SystemExit(f"\nCannot start - backend data could not be loaded:\n  {exc}\n") from None
planner = PlannerAgent(repo, config.AGING_THRESHOLD_DAYS)
if config.LLM_ENABLED:
    from app.llm.aicore import AICoreChat
    from app.planner.llm_planner import LLMPlanner
    planner.llm = LLMPlanner(planner, AICoreChat(
        config.AICORE_AUTH_URL, config.AICORE_CLIENT_ID, config.AICORE_CLIENT_SECRET, config.AICORE_BASE_URL,
        config.LLM_DEPLOYMENT_ID, config.AICORE_RESOURCE_GROUP, config.LLM_TIMEOUT_SECONDS),
        history_turns=config.LLM_HISTORY_TURNS)
    log.info("LLM enabled: AI Core deployment %s", config.LLM_DEPLOYMENT_ID)
else:
    log.info("LLM disabled (AICORE_* not set) - using the rule-based router")


async def _refresh_loop(minutes: float) -> None:
    """Periodically re-read live S43 data; a failed refresh keeps serving the last good data."""
    while True:
        await asyncio.sleep(minutes * 60)
        try:
            await asyncio.to_thread(planner.refresh_data)
            log.info("Refreshed S43 data (as of %s)", repo.data_as_of)
        except Exception:  # noqa: BLE001 - keep the server up on SAP outages
            log.exception("S43 data refresh failed - keeping data as of %s", repo.data_as_of)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = None
    if repo.is_live and config.DATA_REFRESH_MINUTES > 0:
        task = asyncio.create_task(_refresh_loop(config.DATA_REFRESH_MINUTES))
    yield
    if task:
        task.cancel()


def planner_card() -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "Planner Agent - Smart Material AI Chat",
        "description": (
            f"Answers planner questions for {config.SITE_NAME} (plant {config.PLANT}): work order material "
            f"shortage with root cause, aged material (> {config.AGING_THRESHOLD_DAYS} days), over-issued WIP "
            "incl. Z67/Z68 adjustments, production order status, material master and inventory. "
            "Business rules are deterministic; every answer carries a data-as-of timestamp."
        ),
        "url": config.PUBLIC_BASE_URL + PLANNER_PATH,
        "preferredTransport": "JSONRPC",
        "version": "1.0.0",
        "provider": {"organization": "Sierra Digital Inc.", "url": "https://www.sierradigitalinc.com"},
        "capabilities": {"streaming": True, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json", "text/csv",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
        "skills": [
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
        ],
    }


app = FastAPI(title="Smart Material AI Chat - A2A Agents", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])
app.include_router(create_a2a_router(PLANNER_PATH, planner_card(), planner.handle))


@app.middleware("http")
async def no_stale_ui(request, call_next):
    """Browsers must revalidate the chat UI files, so a redeploy is picked up without a hard refresh."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/ui"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "agents": [PLANNER_PATH], **repo.provenance()}


# Chat UI (frontend/) served at /ui
FRONTEND_DIR = config.PROJECT_ROOT / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/ui", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui/")
