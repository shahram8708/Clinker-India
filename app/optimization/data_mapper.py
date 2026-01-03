"""Translate database entities into canonical optimization datasets."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import Inventory, PlanningScenario, Plant, TransportRoute


@dataclass
class CanonicalDataset:
    """Structured, solver-agnostic dataset consumed by model builders."""

    organization_id: int
    scenario_id: int
    periods: int
    plants: list[dict]
    routes: list[dict]
    inventory: dict[int, float]
    demand: dict[int, list[float]]
    safety_stock: dict[int, float]
    metadata: dict[str, Any] = field(default_factory=dict)


def _to_float(value) -> float:
    return float(value) if value is not None else 0.0


class DataMapper:
    """Loads tenant-safe data and normalizes it for optimization."""

    def __init__(self, session):
        self.session = session

    def load_dataset(self, organization_id: int, scenario: PlanningScenario) -> CanonicalDataset:
        plants = Plant.for_org(organization_id).filter_by(status="active").all()
        routes = TransportRoute.for_org(organization_id).filter_by(status="active").all()
        inventories = Inventory.for_org(organization_id).all()

        inventory_map = {inv.plant_id: _to_float(inv.current_inventory) for inv in inventories}
        safety_stock_map = {plant.id: _to_float(plant.safety_stock_level) for plant in plants}

        demand = self._extract_demand(scenario, plants)

        plant_payload = [
            {
                "id": plant.id,
                "name": plant.plant_name,
                "type": plant.plant_type,
                "location": plant.location,
                "production_capacity": _to_float(plant.production_capacity),
                "production_cost": _to_float(getattr(plant, "production_cost", None)),
                "consumption_capacity": _to_float(plant.consumption_capacity),
                "holding_cost": _to_float(getattr(plant, "holding_cost", None)),
                "max_inventory_capacity": _to_float(plant.max_inventory_capacity),
            }
            for plant in plants
        ]

        route_payload = [
            {
                "id": route.id,
                "source": route.source_plant_id,
                "destination": route.destination_plant_id,
                "mode": route.mode,
                "trip_capacity": _to_float(route.trip_capacity),
                "min_batch_quantity": _to_float(route.min_batch_quantity),
                "max_trips_per_period": route.max_trips_per_period,
                "cost_per_trip": _to_float(route.cost_per_trip),
                "cost_per_ton": _to_float(getattr(route, "cost_per_ton", None)),
                "lead_time": getattr(route, "lead_time", None),
            }
            for route in routes
        ]

        return CanonicalDataset(
            organization_id=organization_id,
            scenario_id=scenario.id,
            periods=scenario.periods,
            plants=plant_payload,
            routes=route_payload,
            inventory=inventory_map,
            demand=demand,
            safety_stock=safety_stock_map,
            metadata={"scenario_name": scenario.scenario_name},
        )

    def _extract_demand(self, scenario: PlanningScenario, plants: list[Plant]) -> dict[int, list[float]]:
        """Prefer explicit per-period demand; fallback to legacy consumption-capacity logic."""
        periods = max(getattr(scenario, "periods", 0) or 0, 1)

        raw = None
        for attr in ("demand", "demands", "demand_plan", "demand_profile"):
            candidate = getattr(scenario, attr, None)
            if candidate:
                raw = candidate
                break

        if raw is None:
            meta = getattr(scenario, "summary", {}) or {}
            if not isinstance(meta, dict):
                meta = {}
            raw = meta.get("demand") or meta.get("demands") or meta.get("demand_plan")

        demand: dict[int, list[float]] = {}
        if isinstance(raw, dict):
            for plant in plants:
                series = raw.get(str(plant.id)) or raw.get(plant.id) or []
                cleaned = [_to_float(v) for v in list(series)[:periods]]
                if len(cleaned) < periods:
                    cleaned.extend([0.0] * (periods - len(cleaned)))
                demand[plant.id] = cleaned

        if not demand:
            # Fallback: treat GU consumption capacity as demand each period
            for plant in plants:
                per_period = [_to_float(plant.consumption_capacity)] * periods if plant.plant_type == "GU" else [0.0] * periods
                demand[plant.id] = per_period

        return demand
