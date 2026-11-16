"""Registry of the S/4 (S43) OData APIs the Planner agent calls (see SMC_OData_V4_URLs.xlsx).

All entity sets belong to ONE OData V4 service (CDS views ZC_SMC_*). If a $filter returns a 400,
check the property name on the live system's own ``GET {SERVICE_PATH}/$metadata`` before assuming
the client code is wrong.

Confirmed live:
* The service pages by 100 rows unless the request carries ``$top`` (then up to 5000 per page), so
  ``ODataClient.read`` always sends ``$top`` and follows ``@odata.nextLink``.
* Navigation properties start with ``_`` (``_ComponentShortage``) and arrive as plain lists.
"""
from __future__ import annotations

from dataclasses import dataclass

SERVICE_PATH = "/sap/opu/odata4/sap/zsb_smc_material/srvd_a2x/sap/zsd_smc_material/0001"


@dataclass(frozen=True)
class S4Api:
    entity_set: str
    # V4 navigation properties read with $expand.
    expand: tuple[str, ...] = ()
    # A response missing one of these fields is rejected rather than half-used.
    required_fields: tuple[str, ...] = ()


# ------------------------------------------------------------------ planner JSON files (nested)

# WorkOrderShortage.json - key ManufacturingOrder. No Plant field (the view is plant CN16 only).
WORK_ORDER_SHORTAGE = S4Api(
    "WorkOrderShortage", expand=("_ComponentShortage",),
    required_fields=("ManufacturingOrder", "Material", "OrderShortageStatus", "_ComponentShortage"))

# Component lines of the shortage view, read on their own as open demand for a stock position.
# Key ManufacturingOrder + ReservationItem.
COMPONENT_SHORTAGE = S4Api("ComponentShortage", required_fields=("ManufacturingOrder", "Material"))

# ProductionOrder.json - key ManufacturingOrder (not "ProductionOrder").
PRODUCTION_ORDER = S4Api(
    "ProductionOrder",
    expand=("_ProductionOrderComponent", "_ProductionOrderOperation", "_ProductionOrderStatus"),
    required_fields=("ManufacturingOrder", "Material", "TotalQuantity", "_ProductionOrderStatus"))

# MaterialMaster.json - keyed by "Product", not "Material"; plant data is in the _Plant association.
MATERIAL_MASTER = S4Api(
    "MaterialMaster", expand=("_Plant", "_Valuation", "_StorageLocation", "_BillOfMaterialItem"),
    required_fields=("Product", "ProductDescription", "BaseUnit"))

# Storage-location names (warehouse vs. line-side), read one record per location code.
# Key Product + Plant + StorageLocation.
MATERIAL_STORAGE_LOCATION = S4Api("MaterialStorageLocation")

# Inventory.json - key Plant + StorageLocation + Material. Read without its _MaterialStock detail.
INVENTORY = S4Api("Inventory", required_fields=("Material", "StorageLocation", "Unrestricted", "AgingDays"))

# ------------------------------------------------------------------ Excel files (flat rows, calculated in CDS)

# Z_AGEDINV4 "Data" sheet - stock / aging export. Key Plant + StorageLocation + Material.
STOCK_LIST = S4Api("Sheet1_Data", required_fields=("Material", "StorageLocation", "AgingDays"))

# Z_AGEDINV4 sheet "2" - work-order components with the SME's remaining-quantity formulas. Key RowNo.
# Its OrderNumber values are a larger set than ProductionOrder: never validate them against it.
WO_COMPONENT = S4Api("Sheet2_WOComponent", required_fields=("OrderNumber", "Component", "StorageLocation"))

# Z_AGEDINV4 WIP issuable sheet and its three filtered views. Key StorageLocation + Material.
# MaterialStatus: AV = available to issue, OC = over-control / over-issued, NC = non-controlled.
_ISSUABLE = ("Material", "StorageLocation", "MaterialStatus")
ISSUABLE_ALL = S4Api("Sheet3_WIPIssuable", required_fields=_ISSUABLE)
ISSUABLE_OVER_CONTROL = S4Api("Sheet4_OverControl", required_fields=_ISSUABLE)
ISSUABLE_PUTAWAY_PENDING = S4Api("Sheet5_PutawayPending", required_fields=_ISSUABLE)
ISSUABLE_SUFFIX_S = S4Api("Sheet6_SuffixS", required_fields=_ISSUABLE)

# COOIS2_1.xlsx - user status per order (NMVT = excluded). Key OrderNumber + UserStatus.
ORDER_USER_STATUS = S4Api("COOIS_OrderStatus", required_fields=("OrderNumber", "UserStatus"))

# PP-GP-014C-NSU - component issuance lines. Key RowNo; own set of orders like Sheet2.
ISSUANCE_14C = S4Api("MOD14C_Data", required_fields=("OrderNumber", "Component", "StorageLocation"))

# PP-GP-014C-NSU Sheet1 - need / stock / issuable. No StorageLocation / Material fields:
# RowLabel = 4-char storage location + material, so filter with startswith / endswith.
# IssuableQty = NeedQty - StockQty.
ISSUANCE_14C_SUMMARY = S4Api("MOD14C_Sheet1", required_fields=("RowLabel",))

# PP-1341_1.xlsx has no OData service.

# Source name used by the repository and the planner skills -> API.
BY_NAME: dict[str, S4Api] = {
    "work_order_shortage": WORK_ORDER_SHORTAGE,
    "demand": COMPONENT_SHORTAGE,
    "production_order": PRODUCTION_ORDER,
    "material_master": MATERIAL_MASTER,
    "inventory": INVENTORY,
    "stock_list": STOCK_LIST,
    "wo_component": WO_COMPONENT,
    "issuable_all": ISSUABLE_ALL,
    "issuable_over_control": ISSUABLE_OVER_CONTROL,
    "issuable_putaway_pending": ISSUABLE_PUTAWAY_PENDING,
    "issuable_suffix_s": ISSUABLE_SUFFIX_S,
    "order_user_status": ORDER_USER_STATUS,
    "issuance_14c": ISSUANCE_14C,
    "issuance_14c_summary": ISSUANCE_14C_SUMMARY,
}
