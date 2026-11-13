"""Planner skills on live S43 data: every call reads only what the question needs.

Same skill methods as ``PlannerService`` (which holds the business rules); each one first loads a
small snapshot from S43 with server-side filters, then runs the rules on it. Nothing is cached.
"""
from __future__ import annotations

from app.data.repository import DataRepository, LiveSource
from app.planner.service import PlannerService, Section


class LiveService:
    WO_TOPICS = PlannerService.WO_TOPICS

    def __init__(self, source: LiveSource, aging_threshold_days: int):
        self.source = source
        self.aging_threshold = aging_threshold_days

    def _run(self, snap: DataRepository, skill: str, *args) -> Section:
        section = getattr(PlannerService(snap, self.aging_threshold), skill)(*args)
        section.location_names = snap.storage_location_names
        return section

    def work_order_shortage(self, order: str, focus: set[str] | None = None) -> Section:
        return self._run(self.source.for_order(order), "work_order_shortage", order, focus)

    def production_order(self, order: str) -> Section:
        return self._run(self.source.for_production_order(order), "production_order", order)

    def shortage_overview(self, mrp=frozenset(), order_types=frozenset(), materials=frozenset()) -> Section:
        return self._run(self.source.for_shortage(mrp, order_types, materials),
                         "shortage_overview", mrp, order_types, materials)

    def aged_materials(self, materials=frozenset(), slocs=frozenset()) -> Section:
        return self._run(self.source.for_aged(self.aging_threshold, materials, slocs),
                         "aged_materials", materials, slocs)

    def over_issued(self, orders=frozenset(), mrp=frozenset(), order_types=frozenset(),
                    materials=frozenset()) -> Section:
        return self._run(self.source.for_over_issued(orders, mrp, order_types, materials),
                         "over_issued", orders, mrp, order_types, materials)

    def material_status(self, materials=frozenset(), slocs=frozenset(), status_filter: str | None = None) -> Section:
        return self._run(self.source.for_material_status(materials, slocs),
                         "material_status", materials, slocs, status_filter)

    def material_report(self, material: str) -> Section:
        return self._run(self.source.for_material(material), "material_report", material)

    def machine_head_review(self, orders=frozenset()) -> Section:
        return self._run(self.source.for_machine_head(orders), "machine_head_review", orders)

    def help(self) -> Section:
        return self._run(self.source.for_help(), "help")
