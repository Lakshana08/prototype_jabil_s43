"""Backend data access layer.

Reads the planner data from the live S43 OData V2 service ``ZSB_SMC_MATERIAL_V2``
(CDS views ZC_SMC_*, see ``SMC_All_OData_URLs_Complete 3.xlsx``) and builds lookup indexes.
Each source is read with ``$expand`` so it arrives in the same nested shape as the old
sample JSON files; an offline JSON snapshot can still be used with ``DATA_SOURCE=file``.
The business rules in ``app.planner.rules`` only see this repository, so the data source
does not change any rule logic.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property
from datetime import datetime, timezone
from pathlib import Path

from app.data.odata import ODataClient, ODataError

log = logging.getLogger(__name__)

# Offline snapshot files (DATA_SOURCE=file).
SOURCES = {
    "work_order_shortage": "WorkOrderShortage.json",
    "production_order": "ProductionOrder.json",
    "material_master": "MaterialMaster.json",
    "inventory": "Inventory.json",
}

# Live S43 OData V2 entity sets + $expand (rows 16-28 of SMC_All_OData_URLs_Complete 3.xlsx,
# column "Same nested shape as JSON ($expand) - V2").
ODATA_SOURCES = {
    "work_order_shortage": ("WorkOrderShortage", ["to_ComponentShortage"]),
    "production_order": ("ProductionOrder", ["to_ProductionOrderComponent", "to_ProductionOrderOperation",
                                             "to_ProductionOrderStatus"]),
    "material_master": ("MaterialMaster", ["to_Plant", "to_Valuation", "to_StorageLocation",
                                           "to_BillOfMaterialItem"]),
    "inventory": ("Inventory", ["to_MaterialStock"]),
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
    data_dir: Path | None = None
    odata: ODataClient | None = None
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
        """Load the offline JSON snapshot from ``data_dir``."""
        repo = cls(data_dir=Path(data_dir))
        raw = {key: repo._read_source(key, name) for key, name in SOURCES.items()}
        repo._index(raw)
        log.info("Loaded backend data from %s: %s", data_dir,
                 {k: v.record_count for k, v in repo.sources.items()})
        return repo

    @classmethod
    def from_odata(cls, client: ODataClient) -> "DataRepository":
        """Load live data from the S43 OData service."""
        repo = cls(odata=client)
        raw = {key: repo._read_odata(key, entity_set, expand)
               for key, (entity_set, expand) in ODATA_SOURCES.items()}
        repo._index(raw)
        log.info("Loaded live S43 data from %s: %s", client.base_url,
                 {k: v.record_count for k, v in repo.sources.items()})
        return repo

    @classmethod
    def from_config(cls) -> "DataRepository":
        from app import config  # local import: config is optional for library use
        if config.DATA_SOURCE == "file":
            return cls.load(config.DATA_DIR)
        if config.DATA_SOURCE != "odata":
            raise DataLoadError(f"Unknown DATA_SOURCE {config.DATA_SOURCE!r} (use 'odata' or 'file')")
        if not config.SAP_USER:
            log.warning("SAP_USER is not set - S43 will most likely answer 401 Unauthorized")
        return cls.from_odata(ODataClient(
            base_url=config.SAP_ODATA_BASE_URL, client=config.SAP_CLIENT, user=config.SAP_USER,
            password=config.SAP_PASSWORD, verify_ssl=config.SAP_VERIFY_SSL,
            timeout=config.SAP_TIMEOUT_SECONDS))

    @property
    def is_live(self) -> bool:
        return self.odata is not None

    def reload(self) -> None:
        """Re-read every source and swap the indexes in place (keeps existing references valid).

        On failure the current data is kept and the error is raised to the caller.
        """
        fresh = DataRepository.from_odata(self.odata) if self.is_live else DataRepository.load(self.data_dir)
        stale = set(self.__dict__) - set(fresh.__dict__)  # cached_property values
        self.__dict__.update(fresh.__dict__)
        for key in stale:
            self.__dict__.pop(key, None)

    def _index(self, raw: dict[str, list[dict]]) -> None:
        repo = self
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

    def _read_odata(self, key: str, entity_set: str, expand: list[str]) -> list[dict]:
        try:
            results = self.odata.read_all(entity_set, expand)
        except ODataError as exc:
            raise DataLoadError(f"S43 OData {entity_set}: {exc}") from exc
        self._check_required(key, entity_set, results)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.sources[key] = SourceInfo(
            file=self.odata.url(entity_set, expand),
            entity=f"{entity_set} (ZSB_SMC_MATERIAL_V2, S43 client {self.odata.client})",
            record_count=len(results),
            generated_on=now,
            loaded_at=now,
        )
        return results

    @staticmethod
    def _check_required(key: str, name: str, results: list[dict]) -> None:
        missing = sorted({f for rec in results for f in REQUIRED_FIELDS[key] if f not in rec})
        if missing:
            raise DataLoadError(f"{name}: records missing required fields {missing}")

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

        self._check_required(key, filename, results)

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
            "dataSource": ("Live SAP S43 (OData ZSB_SMC_MATERIAL_V2)" if self.is_live
                           else "Uploaded snapshot (sample_data) - not live SAP"),
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
