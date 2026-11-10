"""Backend data access layer.

Loads the OData-shaped extracts from the sample data folder and builds lookup indexes.
This stands in for the CDS view / OData path described in the Technical Spec (§4, §5):
the business rules in ``app.planner.rules`` only see this repository, so swapping the
JSON files for live OData calls does not change any rule logic.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SOURCES = {
    "work_order_shortage": "WorkOrderShortage.json",
    "production_order": "ProductionOrder.json",
    "material_master": "MaterialMaster.json",
    "inventory": "Inventory.json",
}

# Minimal header contract per source; a file missing these is rejected rather than half-loaded.
REQUIRED_FIELDS = {
    "work_order_shortage": ["ManufacturingOrder", "Material", "OrderShortageStatus", "to_ComponentShortage"],
    "production_order": ["ManufacturingOrder", "Material", "TotalQuantity", "to_ProductionOrderStatus"],
    "material_master": ["Product", "ProductDescription", "BaseUnit"],
    "inventory": ["Material", "StorageLocation", "Unrestricted", "AgingDays"],
}


class DataLoadError(RuntimeError):
    pass


@dataclass
class SourceInfo:
    file: str
    entity: str
    record_count: int
    generated_on: str
    loaded_at: str


@dataclass
class DataRepository:
    data_dir: Path
    shortage_by_order: dict[str, dict] = field(default_factory=dict)
    production_order_by_order: dict[str, dict] = field(default_factory=dict)
    material_master: dict[str, dict] = field(default_factory=dict)
    inventory: list[dict] = field(default_factory=list)
    inventory_by_material: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    storage_location_names: dict[str, str] = field(default_factory=dict)
    sources: dict[str, SourceInfo] = field(default_factory=dict)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, data_dir: Path) -> "DataRepository":
        repo = cls(data_dir=Path(data_dir))
        raw = {key: repo._read_source(key, name) for key, name in SOURCES.items()}

        for rec in raw["work_order_shortage"]:
            repo.shortage_by_order[rec["ManufacturingOrder"]] = rec
        for rec in raw["production_order"]:
            repo.production_order_by_order[rec["ManufacturingOrder"]] = rec
        for rec in raw["material_master"]:
            repo.material_master[rec["Product"].upper()] = rec
            for sl in _nav(rec, "to_StorageLocation"):
                if sl.get("StorageLocation"):
                    repo.storage_location_names.setdefault(sl["StorageLocation"], sl.get("StorageLocationName", ""))
        repo.inventory = raw["inventory"]
        for rec in repo.inventory:
            repo.inventory_by_material[rec["Material"].upper()].append(rec)

        log.info("Loaded backend data from %s: %s", data_dir,
                 {k: v.record_count for k, v in repo.sources.items()})
        return repo

    def _read_source(self, key: str, filename: str) -> list[dict]:
        path = self.data_dir / filename
        if not path.exists():
            raise DataLoadError(f"Missing source file: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            envelope = payload["d"]
            results = envelope["results"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise DataLoadError(f"{filename}: not an OData 'd.results' payload ({exc})") from exc

        missing = sorted({f for rec in results for f in REQUIRED_FIELDS[key] if f not in rec})
        if missing:
            raise DataLoadError(f"{filename}: records missing required fields {missing}")

        loaded_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        self.sources[key] = SourceInfo(
            file=filename,
            entity=envelope.get("entity", ""),
            record_count=len(results),
            generated_on=envelope.get("generatedOn", ""),
            loaded_at=loaded_at,
        )
        return results

    # ------------------------------------------------------------------ lookups
    @property
    def data_as_of(self) -> str:
        """Oldest extraction date across sources - the honest 'as of' for a combined answer."""
        dates = [s.generated_on for s in self.sources.values() if s.generated_on]
        return min(dates) if dates else ""

    def provenance(self) -> dict:
        return {
            "dataSource": "Uploaded snapshot (sample_data) - not live SAP",
            "dataAsOf": self.data_as_of,
            "sources": {k: vars(v) for k, v in self.sources.items()},
        }

    def is_line_side(self, storage_location: str) -> bool:
        return "line-side" in self.storage_location_names.get(storage_location, "").lower()

    @cached_property
    def line_side_locations(self) -> list[str]:
        return sorted(s for s in self.storage_location_names if self.is_line_side(s))

    @cached_property
    def known_materials(self) -> set[str]:
        mats = set(self.material_master) | set(self.inventory_by_material)
        for wo in self.shortage_by_order.values():
            mats.add(wo["Material"].upper())
            mats.update(c["Material"].upper() for c in _nav(wo, "to_ComponentShortage"))
        return mats

    @cached_property
    def known_storage_locations(self) -> set[str]:
        return set(self.storage_location_names) | {i["StorageLocation"] for i in self.inventory}

    @cached_property
    def known_mrp_controllers(self) -> set[str]:
        return {wo["MRPController"] for wo in self.shortage_by_order.values() if wo.get("MRPController")}

    @cached_property
    def known_order_types(self) -> set[str]:
        return {wo["ManufacturingOrderType"] for wo in self.shortage_by_order.values()}

    def components(self, order: str) -> list[dict]:
        return _nav(self.shortage_by_order.get(order, {}), "to_ComponentShortage")


def _nav(record: dict, prop: str) -> list[dict]:
    """Read an OData navigation property ({"results": [...]}) safely."""
    value = record.get(prop) or {}
    return value.get("results", []) if isinstance(value, dict) else list(value)


nav = _nav
