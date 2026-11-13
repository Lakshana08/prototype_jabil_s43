"""Backend data access layer - live, per question.

Nothing is loaded at start-up and nothing is kept between questions. Every question reads
only the records it needs from the S43 OData V2 service ``ZSB_SMC_MATERIAL_V2`` with
server-side ``$filter`` (CDS views ZC_SMC_*, see ``SMC_All_OData_URLs_Complete 3.xlsx``) and
puts them in a small ``DataRepository`` *snapshot*. The business rules in ``app.planner.service``
run on that snapshot, so the rule logic does not depend on how the data was fetched.

``LiveSource`` is the entry point: ID look-ups (does this order / material exist?) and one
``for_*`` loader per planner skill.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from app.data.odata import ODataClient, ODataError, all_of, any_of, eq

log = logging.getLogger(__name__)

CHUNK = 25  # keys per OData $filter (V2 has no "in"; each key is an "or" term)

ORDER_SET = "ProductionOrder"
SHORTAGE_SET = "WorkOrderShortage"
COMPONENT_SET = "ComponentShortage"
MASTER_SET = "MaterialMaster"
INVENTORY_SET = "Inventory"
SLOC_SET = "MaterialStorageLocation"

# Entity sets + $expand, so records arrive in the nested shape the rules expect.
ODATA_SOURCES = {
    "work_order_shortage": (SHORTAGE_SET, ["to_ComponentShortage"]),
    "production_order": (ORDER_SET, ["to_ProductionOrderComponent", "to_ProductionOrderOperation",
                                     "to_ProductionOrderStatus"]),
    "material_master": (MASTER_SET, ["to_Plant", "to_Valuation", "to_StorageLocation", "to_BillOfMaterialItem"]),
    "inventory": (INVENTORY_SET, []),
    "demand": (COMPONENT_SET, []),
}

# Minimal header contract per source; a response missing these is rejected rather than half-used.
REQUIRED_FIELDS = {
    "work_order_shortage": ["ManufacturingOrder", "Material", "OrderShortageStatus", "to_ComponentShortage"],
    "production_order": ["ManufacturingOrder", "Material", "TotalQuantity", "to_ProductionOrderStatus"],
    "material_master": ["Product", "ProductDescription", "BaseUnit"],
    "inventory": ["Material", "StorageLocation", "Unrestricted", "AgingDays"],
    "demand": ["ManufacturingOrder", "Material"],
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


# What this question read from S43 - shown as "data as of" in the answer. Per request, never shared.
_FETCHES: ContextVar[dict[str, SourceInfo] | None] = ContextVar("s43_fetches", default=None)


def begin_request() -> None:
    _FETCHES.set({})


def _record_fetch(key: str, info: SourceInfo) -> None:
    fetches = _FETCHES.get()
    if fetches is None:
        fetches = {}
        _FETCHES.set(fetches)
    prev = fetches.get(key)
    if prev:
        info.record_count += prev.record_count
    fetches[key] = info


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class DataRepository:
    """The records ONE question needs, indexed for the rules. Built by ``LiveSource.for_*``."""
    shortage_by_order: dict[str, dict] = field(default_factory=dict)
    production_order_by_order: dict[str, dict] = field(default_factory=dict)
    material_master: dict[str, dict] = field(default_factory=dict)
    inventory: list[dict] = field(default_factory=list)
    inventory_by_material: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    storage_location_names: dict[str, str] = field(default_factory=dict)
    # Component lines read without their work order (open demand for a stock position).
    demand_components: list[dict] = field(default_factory=list)
    order_range: tuple[str, str] | None = None  # valid order numbers, only set when an order was not found

    def add_inventory(self, rows: Iterable[dict]) -> None:
        for rec in rows:
            self.inventory.append(rec)
            self.inventory_by_material[rec["Material"].upper()].append(rec)

    def add_master(self, rows: Iterable[dict]) -> None:
        for rec in rows:
            self.material_master[rec["Product"].upper()] = rec
            for sl in nav(rec, "to_StorageLocation"):
                if sl.get("StorageLocation"):
                    self.storage_location_names.setdefault(sl["StorageLocation"], sl.get("StorageLocationName", ""))

    def is_line_side(self, storage_location: str) -> bool:
        return "line-side" in self.storage_location_names.get(storage_location, "").lower()

    def components(self, order: str) -> list[dict]:
        return nav(self.shortage_by_order.get(order, {}), "to_ComponentShortage")

    def all_components(self) -> Iterable[dict]:
        for order in self.shortage_by_order:
            yield from self.components(order)
        yield from self.demand_components

    def locations(self) -> set[str]:
        return ({i["StorageLocation"] for i in self.inventory if i.get("StorageLocation")}
                | {c["StorageLocation"] for c in self.all_components() if c.get("StorageLocation")})


def nav(record: dict, prop: str) -> list[dict]:
    """Read an OData navigation property ({"results": [...]}) safely."""
    value = record.get(prop) or {}
    return value.get("results", []) if isinstance(value, dict) else list(value)


def _chunks(values: Iterable[str], size: int = CHUNK):
    values = sorted(set(values))
    for i in range(0, len(values), size):
        yield values[i:i + size]


class LiveSource:
    """Reads S43 on demand. Stateless: no record is kept after a question is answered."""

    def __init__(self, odata: ODataClient):
        self.odata = odata

    @classmethod
    def from_config(cls) -> "LiveSource":
        from app import config  # local import: config is optional for library use
        if config.SAP_DESTINATION_NAME:
            return cls(_destination_client(config))
        if not config.SAP_USER:
            log.warning("SAP_USER is not set - S43 will most likely answer 401 Unauthorized")
        return cls(ODataClient(
            base_url=config.SAP_ODATA_BASE_URL, client=config.SAP_CLIENT, user=config.SAP_USER,
            password=config.SAP_PASSWORD, verify_ssl=config.SAP_VERIFY_SSL,
            timeout=config.SAP_TIMEOUT_SECONDS))

    is_live = True

    def check(self) -> None:
        """Fail fast at start-up when S43 cannot be reached (one record, nothing kept)."""
        self._read(SHORTAGE_SET, top=1, select=["ManufacturingOrder"])

    def provenance(self) -> dict:
        fetches = _FETCHES.get() or {}
        return {
            "dataSource": "Live SAP S43 (OData ZSB_SMC_MATERIAL_V2), read for this question",
            "dataAsOf": min((s.generated_on for s in fetches.values()), default=_now()),
            "sources": {k: vars(v) for k, v in fetches.items()},
        }

    # ================================================================== low level
    def _read(self, entity: str, *, expand=None, filter=None, top=None, orderby=None, select=None) -> list[dict]:
        try:
            return self.odata.read(entity, expand or None, filter, top, orderby, select)
        except ODataError as exc:
            raise DataLoadError(f"S43 OData {entity}: {exc}") from exc

    def _fetch(self, key: str, filter: str | None = None, top: int | None = None) -> list[dict]:
        """Records of a source matching ``filter``; logged for the answer's data-as-of line."""
        entity, expand = ODATA_SOURCES[key]
        rows = self._read(entity, expand=expand, filter=filter, top=top)
        missing = sorted({f for rec in rows for f in REQUIRED_FIELDS[key] if f not in rec})
        if missing:
            raise DataLoadError(f"{entity}: records missing required fields {missing}")
        now = _now()
        _record_fetch(key, SourceInfo(file=self.odata.url(entity, expand, filter, top),
                                      entity=f"{entity} (ZSB_SMC_MATERIAL_V2, S43 client {self.odata.client})",
                                      record_count=len(rows), generated_on=now, loaded_at=now))
        return rows

    def _fetch_keys(self, key: str, field_name: str, values: Iterable[str], extra: str | None = None) -> list[dict]:
        out: list[dict] = []
        for chunk in _chunks(values):
            out += self._fetch(key, all_of(any_of(field_name, chunk), extra))
        return out

    def _exists(self, entity: str, field_name: str, value: str) -> str | None:
        """The stored spelling of ``value`` in ``entity.field`` (None when absent) - one record is read."""
        rows = self._read(entity, filter=eq(field_name, value), top=1, select=[field_name])
        return rows[0][field_name] if rows else None

    # ================================================================== ID look-ups (validation)
    def find_order(self, value: str) -> str | None:
        return self._safe(lambda: self._exists(ORDER_SET, "ManufacturingOrder", value))

    def order_range(self) -> tuple[str, str] | None:
        lo = self._read(ORDER_SET, top=1, orderby="ManufacturingOrder asc", select=["ManufacturingOrder"])
        hi = self._read(ORDER_SET, top=1, orderby="ManufacturingOrder desc", select=["ManufacturingOrder"])
        return (lo[0]["ManufacturingOrder"], hi[0]["ManufacturingOrder"]) if lo and hi else None

    def find_material(self, value: str) -> str | None:
        for candidate in dict.fromkeys([str(value).strip().upper(), str(value).strip()]):
            for entity, field_name in ((MASTER_SET, "Product"), (INVENTORY_SET, "Material"),
                                       (COMPONENT_SET, "Material"), (SHORTAGE_SET, "Material")):
                hit = self._safe(lambda: self._exists(entity, field_name, candidate))
                if hit:
                    return hit.upper()
        return None

    def find_storage_location(self, value: str) -> str | None:
        for candidate in dict.fromkeys([str(value).strip().upper(), str(value).strip()]):
            for entity in (SLOC_SET, INVENTORY_SET):
                hit = self._safe(lambda: self._exists(entity, "StorageLocation", candidate))
                if hit:
                    return hit.upper()
        return None

    def find_mrp_controller(self, value: str) -> str | None:
        for candidate in dict.fromkeys([str(value).strip(), str(value).strip().upper()]):
            hit = self._safe(lambda: self._exists(SHORTAGE_SET, "MRPController", candidate))
            if hit:
                return hit
        return None

    def find_order_type(self, value: str) -> str | None:
        hit = self._safe(lambda: self._exists(SHORTAGE_SET, "ManufacturingOrderType", str(value).strip().upper()))
        return hit.upper() if hit else None

    @staticmethod
    def _safe(fn):
        """A value that is not a plain SAP key (quotes, operators...) simply does not exist."""
        try:
            return fn()
        except DataLoadError as exc:
            if "Not a valid key" in str(exc.__cause__ or ""):
                return None
            raise
        except ODataError:
            return None

    # ================================================================== snapshot loaders, one per skill
    def for_order(self, order: str) -> DataRepository:
        """Work order shortage view: the order, its production order and stock of its components."""
        snap = DataRepository()
        snap.shortage_by_order = {r["ManufacturingOrder"]: r for r in self._fetch("work_order_shortage", eq("ManufacturingOrder", order))}
        snap.production_order_by_order = {r["ManufacturingOrder"]: r for r in self._fetch("production_order", eq("ManufacturingOrder", order))}
        if not snap.shortage_by_order:
            snap.order_range = self.order_range()
            return snap
        mats = {c["Material"] for c in snap.components(order)}
        snap.add_inventory(self._fetch_keys("inventory", "Material", mats))
        self._names(snap)
        return snap

    def for_production_order(self, order: str) -> DataRepository:
        snap = DataRepository()
        snap.production_order_by_order = {r["ManufacturingOrder"]: r for r in self._fetch("production_order", eq("ManufacturingOrder", order))}
        if not snap.production_order_by_order:
            snap.order_range = self.order_range()
            return snap
        snap.shortage_by_order = {r["ManufacturingOrder"]: r for r in self._fetch("work_order_shortage", eq("ManufacturingOrder", order))}
        return snap

    def for_shortage(self, mrp=(), types=(), materials=()) -> DataRepository:
        snap = DataRepository()
        flt = all_of(any_of("MRPController", mrp) if mrp else None,
                     any_of("ManufacturingOrderType", types) if types else None)
        snap.shortage_by_order = self._work_orders(flt, self._orders_with_material(materials, header_too=True, flt=flt)
                                                   if materials else None)
        return snap

    def for_over_issued(self, orders=(), mrp=(), types=(), materials=()) -> DataRepository:
        snap = DataRepository()
        flt = all_of(any_of("ManufacturingOrder", orders) if orders else None,
                     any_of("MRPController", mrp) if mrp else None,
                     any_of("ManufacturingOrderType", types) if types else None)
        snap.shortage_by_order = self._work_orders(flt, self._orders_with_material(materials) if materials else None)
        return snap

    def for_machine_head(self, orders=()) -> DataRepository:
        snap = DataRepository()
        snap.shortage_by_order = self._work_orders(any_of("ManufacturingOrder", orders) if orders else None)
        return snap

    def for_aged(self, threshold: float, materials=(), slocs=()) -> DataRepository:
        """Stock older than the threshold (filtered by S43) and the open demand for those positions."""
        snap = DataRepository()
        snap.add_inventory(self._fetch("inventory", all_of(
            f"AgingDays gt {int(threshold)}", any_of("Material", materials) if materials else None,
            any_of("StorageLocation", slocs) if slocs else None)))
        self._demand(snap, materials)
        self._names(snap)
        return snap

    def for_material_status(self, materials=(), slocs=()) -> DataRepository:
        """Stock by material/location, narrowed to the line-side locations once their names are known."""
        snap = DataRepository()
        snap.add_inventory(self._fetch("inventory", all_of(
            any_of("Material", materials) if materials else None,
            any_of("StorageLocation", slocs) if slocs else None)))
        self._names(snap)
        line_side = {i["StorageLocation"] for i in snap.inventory if snap.is_line_side(i["StorageLocation"])}
        snap.inventory = [i for i in snap.inventory if i["StorageLocation"] in line_side]
        snap.inventory_by_material = defaultdict(list)
        for i in snap.inventory:
            snap.inventory_by_material[i["Material"].upper()].append(i)
        self._demand(snap, materials, line_side)
        return snap

    def for_material(self, material: str) -> DataRepository:
        snap = DataRepository()
        snap.add_master(self._fetch("material_master", eq("Product", material)))
        snap.add_inventory(self._fetch("inventory", eq("Material", material)))
        snap.shortage_by_order = self._work_orders(None, self._orders_with_material([material]))
        self._names(snap)
        return snap

    def for_help(self) -> DataRepository:
        snap = DataRepository()
        for rec in self._read(SHORTAGE_SET, top=1, orderby="ManufacturingOrder asc", select=["ManufacturingOrder"]):
            snap.shortage_by_order[rec["ManufacturingOrder"]] = rec
        snap.add_master(self._read(MASTER_SET, top=1, orderby="Product asc", select=["Product", "ProductDescription", "BaseUnit"]))
        return snap

    # ------------------------------------------------------------------ helpers
    def _work_orders(self, flt: str | None, ids: Iterable[str] | None = None) -> dict[str, dict]:
        """Work orders (with components) matching ``flt``; when ``ids`` is given, only those orders."""
        if ids is None:
            rows = self._fetch("work_order_shortage", flt)
        else:
            rows = self._fetch_keys("work_order_shortage", "ManufacturingOrder", ids, flt)
        return {r["ManufacturingOrder"]: r for r in rows}

    def _orders_with_material(self, materials: Iterable[str], header_too: bool = False,
                              flt: str | None = None) -> set[str]:
        """Orders that consume one of the materials (and, with ``header_too``, also build it)."""
        ids: set[str] = set()
        materials = {m.upper() for m in materials}
        for rec in self._fetch_keys("demand", "Material", materials):
            ids.add(rec["ManufacturingOrder"])
        if header_too:
            for rec in self._fetch_keys("work_order_shortage", "Material", materials, flt):
                ids.add(rec["ManufacturingOrder"])
        return ids

    def _demand(self, snap: DataRepository, materials=(), slocs: Iterable[str] | None = None) -> None:
        """Component lines that make up the open demand for the stock positions already in ``snap``."""
        locs = set(slocs) if slocs is not None else {i["StorageLocation"] for i in snap.inventory}
        if not locs:
            return
        extra = any_of("Material", materials) if materials else None
        for chunk in _chunks(locs):
            snap.demand_components += self._fetch("demand", all_of(any_of("StorageLocation", chunk), extra))

    def _names(self, snap: DataRepository) -> None:
        """Storage-location names (warehouse vs. line-side) for the locations in ``snap`` - one record each."""
        todo = [loc for loc in snap.locations() if loc not in snap.storage_location_names]
        if not todo:
            return

        def one(loc: str) -> tuple[str, str]:
            rows = self._read(SLOC_SET, filter=eq("StorageLocation", loc), top=1,
                              select=["StorageLocation", "StorageLocationName"])
            return loc, (rows[0].get("StorageLocationName") or "") if rows else ""

        with ThreadPoolExecutor(max_workers=min(4, len(todo))) as pool:
            snap.storage_location_names.update(pool.map(one, todo))


def _destination_client(config) -> ODataClient:
    """ODataClient for S43 reached through the SAP BTP destination ``SAP_DESTINATION_NAME``."""
    from app.data.destination import Destination, DestinationError
    try:
        dest = Destination(config.SAP_DESTINATION_NAME, timeout=config.SAP_TIMEOUT_SECONDS)
    except DestinationError as exc:
        raise DataLoadError(str(exc)) from exc
    path = "/" + config.SAP_ODATA_SERVICE_PATH.strip("/")
    base_url = dest.url if dest.url.endswith(path) else dest.url + path
    client = dest.config.get("sap-client") or config.SAP_CLIENT
    return ODataClient(base_url=base_url, client=client, verify_ssl=config.SAP_VERIFY_SSL,
                       timeout=config.SAP_TIMEOUT_SECONDS, destination=dest)
