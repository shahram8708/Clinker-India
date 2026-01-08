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
    
    # Extended multi-period parameters for new dataset structure
    period_specific_costs: dict[str, list[float]] = field(default_factory=dict)  # production_cost, holding_cost per period
    batch_multipliers: dict[int, float] = field(default_factory=dict)  # route_id -> multiplier (quantity multiplier)
    freight_costs: dict[int, list[float]] = field(default_factory=dict)  # route_id -> [cost_p1, cost_p2, cost_p3]
    handling_costs: dict[int, list[float]] = field(default_factory=dict)  # route_id -> [cost_p1, cost_p2, cost_p3]
    hub_codes: dict[int, str] = field(default_factory=dict)  # plant_id -> hub_code (e.g., "H1", "H2")
    iugu_constraints: dict[str, tuple[float, float]] = field(default_factory=dict)  # "IU1_GU1" -> (min, max)
    period_capacities: dict[int, list[float]] = field(default_factory=dict)  # plant_id -> [cap_p1, cap_p2, cap_p3]
    period_demands: dict[tuple[int, int], float] = field(default_factory=dict)  # (plant_id, period) -> demand
    # New parameters for dummy dataset support
    hub_opening_stocks: dict[tuple[int, int], float] = field(default_factory=dict)  # (hub_id, source_iu_id) -> opening_stock
    hub_source_counts: dict[int, int] = field(default_factory=dict)  # hub_id -> number of source IUs
    min_closing_stocks: dict[tuple[int, int], float] = field(default_factory=dict)  # (plant_id, period) -> min_close
    max_closing_stocks: dict[tuple[int, int], float] = field(default_factory=dict)  # (plant_id, period) -> max_close
    flow_constraints: list[dict] = field(default_factory=list)  # [{iu, mode, iugu, period, bound_type, value}, ...]
    transport_modes: list[str] = field(default_factory=list)  # ['T1', 'T2', ...]


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
        
        # Extract extended multi-period parameters from scenario metadata or Excel data
        extended_params = self._extract_extended_parameters(scenario, plants, routes)

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
            # Extended parameters
            period_specific_costs=extended_params.get("period_costs", {}),
            batch_multipliers=extended_params.get("batch_multipliers", {}),
            freight_costs=extended_params.get("freight_costs", {}),
            handling_costs=extended_params.get("handling_costs", {}),
            hub_codes=extended_params.get("hub_codes", {}),
            iugu_constraints=extended_params.get("iugu_constraints", {}),
            period_capacities=extended_params.get("period_capacities", {}),
            period_demands=extended_params.get("period_demands", {}),
            # New dummy dataset parameters
            hub_opening_stocks=extended_params.get("hub_opening_stocks", {}),
            hub_source_counts=extended_params.get("hub_source_counts", {}),
            min_closing_stocks=extended_params.get("min_closing_stocks", {}),
            max_closing_stocks=extended_params.get("max_closing_stocks", {}),
            flow_constraints=extended_params.get("flow_constraints", []),
            transport_modes=extended_params.get("transport_modes", []),
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
    def _extract_extended_parameters(self, scenario: PlanningScenario, plants: list[Plant], routes: list[TransportRoute]) -> dict[str, Any]:
        """Extract multi-period specific parameters, batch multipliers, hub codes, and constraints from scenario metadata or Excel."""
        periods = max(getattr(scenario, "periods", 0) or 0, 1)
        
        # Try to get extended data from scenario summary or metadata
        meta = getattr(scenario, "summary", {}) or {}
        if not isinstance(meta, dict):
            meta = {}
        
        result: dict[str, Any] = {
            "period_costs": {},
            "batch_multipliers": {},
            "freight_costs": {},
            "handling_costs": {},
            "hub_codes": {},
            "iugu_constraints": {},
            "period_capacities": {},
            "period_demands": {},
            # New dummy dataset parameters
            "hub_opening_stocks": {},
            "hub_source_counts": {},
            "min_closing_stocks": {},
            "max_closing_stocks": {},
            "flow_constraints": [],
            "transport_modes": [],
        }
        
        # Extract batch multipliers from routes metadata or summary
        batch_mult_data = meta.get("batch_multipliers", {})
        for route in routes:
            route_id = route.id
            # Try route-specific multiplier, fallback to metadata, finally default to 1.0 (no multiplier)
            multiplier = _to_float(getattr(route, "batch_multiplier", None))
            if not multiplier:
                multiplier = _to_float(batch_mult_data.get(str(route_id), batch_mult_data.get(route_id, 1.0)))
            if not multiplier:
                multiplier = 1.0
            result["batch_multipliers"][route_id] = multiplier
        
        # Extract period-specific freight and handling costs
        freight_data = meta.get("freight_costs", {})
        handling_data = meta.get("handling_costs", {})
        for route in routes:
            route_id = route.id
            # Freight costs per period
            freight_series = freight_data.get(str(route_id), freight_data.get(route_id, []))
            if isinstance(freight_series, (list, tuple)):
                freight_cleaned = [_to_float(v) for v in list(freight_series)[:periods]]
            else:
                # Single value: replicate across periods
                freight_cleaned = [_to_float(freight_series)] * periods
            if len(freight_cleaned) < periods:
                # Pad with last value or route's cost_per_trip
                last_val = freight_cleaned[-1] if freight_cleaned else _to_float(route.cost_per_trip)
                freight_cleaned.extend([last_val] * (periods - len(freight_cleaned)))
            result["freight_costs"][route_id] = freight_cleaned
            
            # Handling costs per period
            handling_series = handling_data.get(str(route_id), handling_data.get(route_id, []))
            if isinstance(handling_series, (list, tuple)):
                handling_cleaned = [_to_float(v) for v in list(handling_series)[:periods]]
            else:
                handling_cleaned = [_to_float(handling_series)] * periods
            if len(handling_cleaned) < periods:
                last_val = handling_cleaned[-1] if handling_cleaned else 0.0
                handling_cleaned.extend([last_val] * (periods - len(handling_cleaned)))
            result["handling_costs"][route_id] = handling_cleaned
        
        # Extract hub codes from plants metadata
        hub_data = meta.get("hub_codes", {})
        for plant in plants:
            plant_id = plant.id
            hub_code = getattr(plant, "hub_code", None) or hub_data.get(str(plant_id), hub_data.get(plant_id, ""))
            if hub_code:
                result["hub_codes"][plant_id] = str(hub_code)
        
        # Extract IUGU constraints (min, max flow bounds per IU-GU pair)
        iugu_data = meta.get("iugu_constraints", {})
        for constraint_key, bounds in iugu_data.items():
            # Expected format: "IU1_GU2" -> {"min": 100, "max": 500}
            if isinstance(bounds, dict):
                min_val = _to_float(bounds.get("min", 0.0))
                max_val = _to_float(bounds.get("max", float("inf")))
                result["iugu_constraints"][str(constraint_key)] = (min_val, max_val)
            elif isinstance(bounds, (list, tuple)) and len(bounds) >= 2:
                result["iugu_constraints"][str(constraint_key)] = (_to_float(bounds[0]), _to_float(bounds[1]))
        
        # Extract period-specific capacities and demands
        capacity_data = meta.get("period_capacities", {})
        demand_data = meta.get("period_demands", {})
        
        for plant in plants:
            plant_id = plant.id
            cap_series = capacity_data.get(str(plant_id), capacity_data.get(plant_id, []))
            if isinstance(cap_series, (list, tuple)):
                cap_cleaned = [_to_float(v) for v in list(cap_series)[:periods]]
            else:
                cap_cleaned = [_to_float(cap_series or plant.production_capacity)] * periods
            if len(cap_cleaned) < periods:
                last_val = cap_cleaned[-1] if cap_cleaned else _to_float(plant.production_capacity)
                cap_cleaned.extend([last_val] * (periods - len(cap_cleaned)))
            result["period_capacities"][plant_id] = cap_cleaned
            
            demand_series = demand_data.get(str(plant_id), demand_data.get(plant_id, []))
            if isinstance(demand_series, (list, tuple)):
                for t_idx, val in enumerate(list(demand_series)[:periods]):
                    result["period_demands"][(plant_id, t_idx + 1)] = _to_float(val)
            else:
                # Single value or no data: use existing demand extraction
                pass
        
        return result