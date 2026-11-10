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


# Where backend data comes from:
#   "odata" (default) - live S43 OData V2 service ZSB_SMC_MATERIAL_V2 (SMC_All_OData_URLs_Complete 3.xlsx)
#   "file"            - offline JSON snapshot in DATA_DIR (used by the unit tests)
DATA_SOURCE = os.getenv("DATA_SOURCE", "odata").strip().lower()

# Live S43 OData service (client 100).
SAP_ODATA_BASE_URL = os.getenv(
    "SAP_ODATA_BASE_URL", "https://192.168.8.69:44300/sap/opu/odata/sap/ZSB_SMC_MATERIAL_V2")
SAP_CLIENT = os.getenv("SAP_CLIENT", "100")
SAP_USER = os.getenv("SAP_USER", "")
SAP_PASSWORD = os.getenv("SAP_PASSWORD", "")
# true (default) | false (accept self-signed cert - dev only) | path to CA bundle
SAP_VERIFY_SSL = _bool_or_path(os.getenv("SAP_VERIFY_SSL", "true"))
SAP_TIMEOUT_SECONDS = float(os.getenv("SAP_TIMEOUT_SECONDS", "60"))
# Re-read S43 every N minutes while the server runs (0 = load once at startup). Ignored for "file".
DATA_REFRESH_MINUTES = float(os.getenv("DATA_REFRESH_MINUTES", "15"))

# Folder holding the offline extracts (WorkOrderShortage / ProductionOrder / MaterialMaster / Inventory).
DATA_DIR = Path(os.getenv("SAMPLE_DATA_DIR", PROJECT_ROOT / "Sample_data"))

# Aging threshold in days. Default 14 (global KPI); changed only by an authorized administrator
# through configuration, never by the end user in chat.
AGING_THRESHOLD_DAYS = int(os.getenv("AGING_THRESHOLD_DAYS", "14"))

# Public base URL advertised in the A2A agent card.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

PLANT = os.getenv("PLANT", "CN16")
SITE_NAME = "Jabil Suzhou GP"
