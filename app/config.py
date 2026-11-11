"""Runtime configuration (environment-variable driven)."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from .env (git-ignored); real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not os.environ.get(key):  # a non-empty real environment variable wins
            os.environ[key] = value.strip().strip('"').strip("'")


_load_dotenv(PROJECT_ROOT / ".env")


def _bool_or_path(value: str) -> bool | str:
    low = value.strip().lower()
    if low in ("", "1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    return value  # path to a CA bundle (.pem) for the S43 certificate


# Live S43 OData service (client 100).
SAP_ODATA_BASE_URL = os.getenv(
    "SAP_ODATA_BASE_URL", "https://192.168.8.69:44300/sap/opu/odata/sap/ZSB_SMC_MATERIAL_V2")
SAP_CLIENT = os.getenv("SAP_CLIENT", "100")
SAP_USER = os.getenv("SAP_USER", "")
SAP_PASSWORD = os.getenv("SAP_PASSWORD", "")
# true (default) | false (accept self-signed cert - dev only) | path to CA bundle
SAP_VERIFY_SSL = _bool_or_path(os.getenv("SAP_VERIFY_SSL", "true"))
SAP_TIMEOUT_SECONDS = float(os.getenv("SAP_TIMEOUT_SECONDS", "60"))
# SAP BTP (Cloud Foundry): name of the destination pointing to S43. When set, the URL, credentials
# and Cloud Connector routing come from the bound Destination + Connectivity services, and
# SAP_ODATA_BASE_URL / SAP_USER / SAP_PASSWORD are ignored. Leave empty for local runs.
SAP_DESTINATION_NAME = os.getenv("SAP_DESTINATION_NAME", "").strip()
# OData service path appended to the destination URL (unless the destination URL already contains it).
SAP_ODATA_SERVICE_PATH = os.getenv("SAP_ODATA_SERVICE_PATH", "/sap/opu/odata/sap/ZSB_SMC_MATERIAL_V2")

# Re-read S43 every N minutes while the server runs (0 = load once at startup).
DATA_REFRESH_MINUTES = float(os.getenv("DATA_REFRESH_MINUTES", "15"))


# Aging threshold in days. Default 14 (global KPI); changed only by an authorized administrator
# through configuration, never by the end user in chat.
AGING_THRESHOLD_DAYS = int(os.getenv("AGING_THRESHOLD_DAYS", "14"))

# Public base URL advertised in the A2A agent card.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

PLANT = os.getenv("PLANT", "CN16")
SITE_NAME = "Jabil Suzhou GP"

# SAP AI Core (generative AI hub). When all of these are set, an LLM understands the question
# (picks the business-rule tools and their parameters) and writes the answer from their results.
# Numbers always come from S43 via the deterministic rules. If unset or AI Core fails, the
# keyword router + answer templates are used instead.
AICORE_AUTH_URL = os.getenv("AICORE_AUTH_URL", "").strip()
AICORE_CLIENT_ID = os.getenv("AICORE_CLIENT_ID", "").strip()
AICORE_CLIENT_SECRET = os.getenv("AICORE_CLIENT_SECRET", "").strip()
AICORE_BASE_URL = os.getenv("AICORE_BASE_URL", "").strip()
AICORE_RESOURCE_GROUP = os.getenv("AICORE_RESOURCE_GROUP", "default").strip()
LLM_DEPLOYMENT_ID = os.getenv("LLM_DEPLOYMENT_ID", "").strip()
LLM_ENABLED = os.getenv("LLM_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off") and all(
    (AICORE_AUTH_URL, AICORE_CLIENT_ID, AICORE_CLIENT_SECRET, AICORE_BASE_URL, LLM_DEPLOYMENT_ID))
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "90"))
# Previous question/answer pairs kept per conversation (A2A contextId) for follow-up questions.
LLM_HISTORY_TURNS = int(os.getenv("LLM_HISTORY_TURNS", "6"))
