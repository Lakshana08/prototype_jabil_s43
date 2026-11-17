"""Entry point of the backend.

Run:  python -m uvicorn app.main:app --port 8000

Startup, in three steps:
  1. connect_to_s43()   - open the live S43 OData V4 service (stop if it cannot be reached)
  2. build_planner()    - create the Planner agent, with GPT-4o on SAP AI Core when it is configured
  3. create_app()       - FastAPI app with the A2A endpoint /a2a/planner and /health

The agent card (skills list) is in app/a2a/agent_card.py, the A2A protocol in app/a2a/server.py.
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import config
from app.a2a.agent_card import PLANNER_PATH, planner_card
from app.a2a.server import create_a2a_router
from app.data.repository import DataLoadError, LiveSource
from app.planner.agent import PlannerAgent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------- 1. SAP S43 connection
def connect_to_s43() -> LiveSource:
    """Open the S43 data source and make one test call, so a wrong URL / password fails at startup."""
    try:
        source = LiveSource.from_config()
        source.check()
    except DataLoadError as exc:
        raise SystemExit(f"\nCannot start - S43 could not be reached:\n  {exc}\n") from None
    return source


# ---------------------------------------------------------------- 2. Planner agent
def build_planner(source: LiveSource) -> PlannerAgent:
    """Planner agent. With AI Core configured, GPT-4o picks the tools; otherwise the keyword router is used."""
    planner = PlannerAgent(source, config.AGING_THRESHOLD_DAYS)

    if not config.LLM_ENABLED:
        log.info("LLM disabled (AICORE_* not set) - using the rule-based router")
        return planner

    from app.llm.aicore import AICoreChat
    from app.planner.llm_planner import LLMPlanner

    chat = AICoreChat(
        config.AICORE_AUTH_URL, config.AICORE_CLIENT_ID, config.AICORE_CLIENT_SECRET, config.AICORE_BASE_URL,
        config.LLM_DEPLOYMENT_ID, config.AICORE_RESOURCE_GROUP, config.LLM_TIMEOUT_SECONDS)
    planner.llm = LLMPlanner(planner, chat, history_turns=config.LLM_HISTORY_TURNS)
    log.info("LLM enabled: AI Core deployment %s", config.LLM_DEPLOYMENT_ID)
    return planner


# ---------------------------------------------------------------- 3. Web app (API only)
def create_app(source: LiveSource, planner: PlannerAgent) -> FastAPI:
    """FastAPI app: the A2A endpoint of the Planner agent and a health check. No UI (see frontend/)."""
    app = FastAPI(title="Smart Material AI Chat - A2A Agents", version="1.0.0")

    # The frontend is deployed separately, so allow calls from other origins.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])

    # POST /a2a/planner (JSON-RPC) and GET /a2a/planner/.well-known/agent-card.json
    app.include_router(create_a2a_router(PLANNER_PATH, planner_card(), planner.handle))

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "agents": [PLANNER_PATH],
            "dataSource": f"Live SAP S43 ({source.odata.service_name}), read per question",
        }

    return app


# ---------------------------------------------------------------- startup
_source = connect_to_s43()
app = create_app(_source, build_planner(_source))
