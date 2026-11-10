"""Runtime configuration (environment-variable driven)."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Folder holding the backend extracts (WorkOrderShortage / ProductionOrder / MaterialMaster / Inventory).
DATA_DIR = Path(os.getenv("SAMPLE_DATA_DIR", PROJECT_ROOT / "Sample_data"))

# Aging threshold in days. Default 14 (global KPI); changed only by an authorized administrator
# through configuration, never by the end user in chat.
AGING_THRESHOLD_DAYS = int(os.getenv("AGING_THRESHOLD_DAYS", "14"))

# Public base URL advertised in the A2A agent card.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

PLANT = os.getenv("PLANT", "CN16")
SITE_NAME = "Jabil Suzhou GP"
