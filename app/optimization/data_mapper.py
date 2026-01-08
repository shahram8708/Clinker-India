"""Translate nine-sheet clinker tables into canonical optimization datasets."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from ..models import (
    ClinkerCapacity,
    ClinkerDemand,
    HubOpeningStock,
    IUGUClosingStock,
    IUGUConstraint,
    IUGUOpeningStock,
    IUGUType,
    LogisticsIUGU,
    PlanningScenario,
    ProductionCost,
)


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
    min_fulfillment: dict[int, list[float]] = field(default_factory=dict)
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


def _zeros(periods: int) -> List[float]:
    return [0.0 for _ in range(periods)]


class DataMapper:
    """Loads tenant-safe data from the nine-sheet schema and normalizes it for optimization."""

    def __init__(self, session):
        self.session = session

    def load_dataset(self, organization_id: int, scenario: PlanningScenario) -> CanonicalDataset:
        periods = max(int(getattr(scenario, "periods", 1) or 1), 1)

        def _get_rows(model, order_by=None):
            """Fetch scenario-scoped rows; fall back to baseline rows when missing."""
            def _ordered(query):
                return query.order_by(*order_by) if order_by else query

            scoped = model.for_org(organization_id)
            rows = _ordered(scoped.filter_by(planning_scenario_id=scenario.id)).all()
            if rows:
                return rows
            return _ordered(scoped.filter_by(planning_scenario_id=None)).all()

        plants = _get_rows(IUGUType, order_by=[IUGUType.code])

        # Raw table reads (all used to build the canonical graph)
        capacities_raw = _get_rows(ClinkerCapacity)
        prod_cost_raw = _get_rows(ProductionCost)
        demand_rows = _get_rows(ClinkerDemand)
        inventory_rows = _get_rows(IUGUOpeningStock)
        hub_rows = _get_rows(HubOpeningStock)
        close_rows = _get_rows(IUGUClosingStock)
        logistics_rows = _get_rows(LogisticsIUGU)
        constraint_rows = _get_rows(IUGUConstraint)

        # Build a complete set of plant codes (include any codes referenced only in logistics/demand/constraints)
        plant_records: Dict[str, str] = {p.code: p.plant_type for p in plants}

        def _infer_type(code: str) -> str:
            if code.startswith("GU"):
                return "GU"
            if code.startswith("IU") or code.startswith("EXT"):
                return "IU"
            return "IU"

        def _add_code(code: str | None) -> None:
            if code and code not in plant_records:
                plant_records[code] = _infer_type(code)

        for row in capacities_raw:
            _add_code(row.plant_code)
        for row in prod_cost_raw:
            _add_code(row.plant_code)
        for row in demand_rows:
            _add_code(row.plant_code)
        for row in inventory_rows:
            _add_code(row.plant_code)
        for row in hub_rows:
            _add_code(row.from_code)
            _add_code(row.to_code)
        for row in close_rows:
            _add_code(row.plant_code)
        for row in logistics_rows:
            _add_code(row.from_code)
            _add_code(row.to_code)
        for row in constraint_rows:
            _add_code(row.from_code)
            _add_code(row.to_code)

        code_to_id: Dict[str, int] = {code: idx + 1 for idx, code in enumerate(sorted(plant_records.keys()))}
        id_to_code: Dict[int, str] = {v: k for k, v in code_to_id.items()}

        capacities: Dict[int, List[float]] = {pid: _zeros(periods) for pid in code_to_id.values()}
        for row in capacities_raw:
            pid = code_to_id.get(row.plant_code)
            if pid is None:
                continue
            idx = max(min(int(row.time_period or 1), periods), 1) - 1
            capacities[pid][idx] = _to_float(row.capacity_tons)

        prod_costs: Dict[int, List[float]] = {pid: _zeros(periods) for pid in code_to_id.values()}
        for row in prod_cost_raw:
            pid = code_to_id.get(row.plant_code)
            if pid is None:
                continue
            idx = max(min(int(row.time_period or 1), periods), 1) - 1
            prod_costs[pid][idx] = _to_float(row.cost_per_ton)

        # Demand and min fulfillment per plant per period
        demand: Dict[int, List[float]] = {pid: _zeros(periods) for pid in code_to_id.values()}
        min_fulfillment: Dict[int, List[float]] = {pid: [1.0] * periods for pid in code_to_id.values()}
        for row in demand_rows:
            pid = code_to_id.get(row.plant_code)
            if pid is None:
                continue
            idx = max(min(int(row.time_period or 1), periods), 1) - 1
            demand[pid][idx] = _to_float(row.demand_tons)
            min_fulfillment[pid][idx] = max(min(_to_float(row.min_fulfillment_pct) / 100.0, 1.0), 0.0)

        # Opening stock
        inventory = {pid: 0.0 for pid in code_to_id.values()}
        for row in inventory_rows:
            pid = code_to_id.get(row.plant_code)
            if pid is not None:
                inventory[pid] = _to_float(row.opening_stock)

        # Hub opening stock (optional, tracked in metadata only for now)
        hub_opening_stocks: Dict[Tuple[int, int], float] = {}
        for row in hub_rows:
            iu_id = code_to_id.get(row.from_code)
            hub_id = code_to_id.get(row.to_code)
            if iu_id is not None and hub_id is not None:
                hub_opening_stocks[(hub_id, iu_id)] = _to_float(row.opening_stock)

        # Closing stock bounds per plant-period
        min_close: Dict[Tuple[int, int], float] = {}
        max_close: Dict[Tuple[int, int], float] = {}
        for row in close_rows:
            pid = code_to_id.get(row.plant_code)
            if pid is None:
                continue
            idx = max(min(int(row.time_period or 1), periods), 1)
            min_close[(pid, idx)] = _to_float(row.min_close_stock)
            max_close[(pid, idx)] = _to_float(row.max_close_stock) if row.max_close_stock is not None else float("inf")

        # Safety stock baseline: lowest min_close across periods (used as per-plant floor)
        safety_stock = {pid: 0.0 for pid in code_to_id.values()}
        for (pid, _), val in min_close.items():
            safety_stock[pid] = min(safety_stock.get(pid, val) or val, val)

        # Logistics routes grouped by (from, to, mode)
        route_groups: Dict[Tuple[str, str, str], List[LogisticsIUGU]] = {}
        for row in logistics_rows:
            key = (row.from_code, row.to_code, row.transport_code)
            route_groups.setdefault(key, []).append(row)

        routes = []
        freight_costs: Dict[int, List[float]] = {}
        handling_costs: Dict[int, List[float]] = {}
        batch_multipliers: Dict[int, float] = {}
        transport_modes: List[str] = []

        route_id = 1
        for (from_code, to_code, mode), entries in route_groups.items():
            src_id = code_to_id.get(from_code)
            dst_id = code_to_id.get(to_code)
            if src_id is None or dst_id is None:
                continue

            # Assume multiplier constant across periods; take the first non-zero value
            multiplier_val = None
            for entry in entries:
                if entry.quantity_multiplier:
                    multiplier_val = _to_float(entry.quantity_multiplier)
                    break

            transport_modes.append(mode)
            routes.append(
                {
                    "id": route_id,
                    "source": src_id,
                    "destination": dst_id,
                    "mode": mode,
                    # quantity_multiplier acts as trip capacity; batch linkage enforced in solver
                    "trip_capacity": multiplier_val if multiplier_val is not None else 0.0,
                    "min_batch_quantity": 0.0,
                    "max_trips_per_period": None,
                }
            )

            freight_costs[route_id] = _zeros(periods)
            handling_costs[route_id] = _zeros(periods)
            batch_multipliers[route_id] = multiplier_val or 1.0

            for entry in entries:
                idx = max(min(int(entry.time_period or 1), periods), 1) - 1
                freight_costs[route_id][idx] = _to_float(entry.freight_cost)
                handling_costs[route_id][idx] = _to_float(entry.handling_cost)

            route_id += 1

        # IUGU constraints per period (flow bounds)
        flow_constraints: List[dict] = []
        for row in constraint_rows:
            src_id = code_to_id.get(row.from_code)
            dst_id = code_to_id.get(row.to_code) if row.to_code else None
            if src_id is None:
                continue
            flow_constraints.append(
                {
                    "source": src_id,
                    "destination": dst_id,
                    "mode": row.transport_code or None,
                    "period": int(row.time_period or 1),
                    "type": row.constraint_type,
                    "value": _to_float(row.value),
                }
            )

        # Legacy iugu_constraints (min/max across all periods) left empty for now
        iugu_constraints: Dict[str, Tuple[float, float]] = {}

        # Plant payload for solver
        plant_payload = []
        default_prod_penalty = 1_000_000.0
        holding_cost_default = 5.0  # ₹5 per ton-period

        for code, pid in code_to_id.items():
            plant_type = plant_records.get(code, "GU")
            cap_series = capacities.get(pid, _zeros(periods))
            cost_series = prod_costs.get(pid, _zeros(periods))
            # Use first period cost as a representative scalar for downstream logging; full series kept separately
            representative_cost = cost_series[0] if any(cost_series) else default_prod_penalty
            plant_payload.append(
                {
                    "id": pid,
                    "code": code,
                    "type": plant_type,
                    "production_capacity": max(cap_series) if plant_type == "IU" else 0.0,
                    "production_cost": representative_cost,
                    "consumption_capacity": 0.0,
                    "holding_cost": holding_cost_default,
                    "max_inventory_capacity": float("inf"),
                }
            )

        metadata = {
            "scenario_name": scenario.scenario_name,
            "id_to_code": id_to_code,
            "code_to_id": code_to_id,
        }

        return CanonicalDataset(
            organization_id=organization_id,
            scenario_id=scenario.id,
            periods=periods,
            plants=plant_payload,
            routes=routes,
            inventory=inventory,
            demand=demand,
            safety_stock=safety_stock,
            min_fulfillment=min_fulfillment,
            metadata=metadata,
            period_specific_costs=prod_costs,
            batch_multipliers=batch_multipliers,
            freight_costs=freight_costs,
            handling_costs=handling_costs,
            hub_codes={},
            iugu_constraints=iugu_constraints,
            period_capacities=capacities,
            period_demands={},
            hub_opening_stocks=hub_opening_stocks,
            hub_source_counts={},
            min_closing_stocks=min_close,
            max_closing_stocks=max_close,
            flow_constraints=flow_constraints,
            transport_modes=transport_modes,
        )