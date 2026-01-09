"""Adapter layer for the elastic MILP solver."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Tuple

from .exceptions import SolverError
from .model_builder import ModelDefinition


@dataclass
class SolverResult:
    """Normalized solver output."""

    feasible: bool
    status: str
    objective_value: float
    shipment_plan: Dict[Tuple[int, int], float]
    production_plan: Dict[Tuple[int, int], float]
    inventory_plan: Dict[Tuple[int, int], float]
    trips_plan: Dict[Tuple[int, int], float] = field(default_factory=dict)
    fulfillment_plan: Dict[Tuple[int, int], float] = field(default_factory=dict)
    shortage_plan: Dict[Tuple[int, int], float] = field(default_factory=dict)
    slack_min_fulfillment: Dict[Tuple[int, int], float] = field(default_factory=dict)
    slack_min_stock: Dict[Tuple[int, int], float] = field(default_factory=dict)
    slack_max_stock: Dict[Tuple[int, int], float] = field(default_factory=dict)
    slack_service_level: float = 0.0
    cost_breakdown: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)
    runtime_seconds: float = 0.0


class _DeterministicMilpSolver:
    """Elastic deterministic MILP with slack penalties."""

    def __init__(self) -> None:
        self._pulp = None

    def _require_pulp(self):
        if self._pulp is None:
            try:
                import pulp  # type: ignore
            except ImportError as exc:  # pragma: no cover - import guard
                raise SolverError("PuLP is required for elastic MILP solving") from exc
            self._pulp = pulp
        return self._pulp

    def solve(self, model: ModelDefinition, time_limit: int | None = None) -> SolverResult:
        start = time.monotonic()
        dataset = model.dataset
        pulp = self._require_pulp()

        periods = range(1, dataset.periods + 1)
        plant_lookup = {p["id"]: p for p in dataset.plants}
        iu_ids = [pid for pid, plant in plant_lookup.items() if plant.get("type") == "IU"]
        demand_nodes = list(plant_lookup.keys())

        demand = {pid: dataset.demand.get(pid, [0.0] * dataset.periods) for pid in plant_lookup}
        min_ful = getattr(dataset, "min_fulfillment", {}) or {}
        inv0 = {pid: float(dataset.inventory.get(pid, 0.0)) for pid in plant_lookup}

        min_close = getattr(dataset, "min_closing_stocks", {}) or {}
        max_close = getattr(dataset, "max_closing_stocks", {}) or {}

        # Holding cost per plant (default 0 if not provided)
        hold_cost: Dict[int, float] = {pid: float(plant_lookup[pid].get("holding_cost", 0.0) or 0.0) for pid in plant_lookup}

        prod_cap: Dict[int, list[float]] = {}
        prod_cost: Dict[int, list[float]] = {}
        for pid in plant_lookup:
            if pid in dataset.period_capacities and len(dataset.period_capacities[pid]) == dataset.periods:
                prod_cap[pid] = [float(v) for v in dataset.period_capacities[pid]]
            else:
                static_cap = float(plant_lookup[pid].get("production_capacity", 0.0))
                prod_cap[pid] = [static_cap] * dataset.periods

            cost_series = getattr(dataset, "period_specific_costs", {}).get(pid)
            if cost_series and len(cost_series) == dataset.periods:
                prod_cost[pid] = [float(v) for v in cost_series]
            else:
                scalar = float(plant_lookup[pid].get("production_cost", 0.0) or 0.0)
                prod_cost[pid] = [scalar] * dataset.periods

        # Pre-compute normalized per-unit transport cost (freight + handling) / multiplier
        normalized_transport: Dict[Tuple[int, int], float] = {}
        trip_cost: Dict[Tuple[int, int], float] = {}
        route_modes: Dict[int, str] = {}
        for route in dataset.routes:
            rid = route["id"]
            route_modes[rid] = route.get("mode")
            multiplier = max(float(dataset.batch_multipliers.get(rid, 1.0) or 1.0), 1e-6)
            freight_series = dataset.freight_costs.get(rid, [0.0] * dataset.periods)
            handling_series = dataset.handling_costs.get(rid, [0.0] * dataset.periods)
            for idx, t in enumerate(periods):
                freight = float(freight_series[idx]) if idx < len(freight_series) else 0.0
                handling = float(handling_series[idx]) if idx < len(handling_series) else 0.0
                trip_cost[(rid, t)] = freight + handling
                normalized_transport[(rid, t)] = (freight + handling) / multiplier

        prob = pulp.LpProblem("elastic_supply_chain", pulp.LpMinimize)

        Prod = {(pid, t): pulp.LpVariable(f"prod_{pid}_{t}", lowBound=0) for pid in plant_lookup for t in periods}
        Ship = {(route["id"], t): pulp.LpVariable(f"ship_{route['id']}_{t}", lowBound=0) for route in dataset.routes for t in periods}
        Trips = {(route["id"], t): pulp.LpVariable(f"trips_{route['id']}_{t}", lowBound=0, cat=pulp.LpInteger) for route in dataset.routes for t in periods}
        Inv = {(pid, t): pulp.LpVariable(f"inv_{pid}_{t}", lowBound=0) for pid in plant_lookup for t in periods}
        Fulfill = {(pid, t): pulp.LpVariable(f"fulfill_{pid}_{t}", lowBound=0) for pid in plant_lookup for t in periods}
        Shortage = {(pid, t): pulp.LpVariable(f"short_{pid}_{t}", lowBound=0) for pid in plant_lookup for t in periods}

        SlackFul = {(pid, t): pulp.LpVariable(f"s_ful_{pid}_{t}", lowBound=0) for pid in plant_lookup for t in periods}
        SlackMinStk = {(pid, t): pulp.LpVariable(f"s_min_{pid}_{t}", lowBound=0) for pid in plant_lookup for t in periods}
        SlackMaxStk = {(pid, t): pulp.LpVariable(f"s_max_{pid}_{t}", lowBound=0) for pid in plant_lookup for t in periods}

        # Production bounds
        for pid in plant_lookup:
            for t in periods:
                cap = prod_cap.get(pid, [0.0] * dataset.periods)[t - 1]
                if pid in iu_ids:
                    prob += Prod[(pid, t)] <= cap, f"cap_prod_{pid}_{t}"
                else:
                    prob += Prod[(pid, t)] == 0, f"no_prod_{pid}_{t}"

        # Trip linkage and bounds per route/period
        for route in dataset.routes:
            rid = route["id"]
            multiplier = max(float(dataset.batch_multipliers.get(rid, 1.0) or 1.0), 1e-6)
            for t in periods:
                prob += Ship[(rid, t)] == Trips[(rid, t)] * multiplier, f"ship_trip_link_{rid}_{t}"
                max_trips = route.get("max_trips_per_period")
                if max_trips is not None:
                    prob += Trips[(rid, t)] <= float(max_trips), f"max_trips_{rid}_{t}"
                min_batch = float(route.get("min_batch_quantity", 0.0) or 0.0)
                if min_batch > 0:
                    prob += Ship[(rid, t)] >= Trips[(rid, t)] * min_batch, f"min_batch_{rid}_{t}"

        # Inventory balance with fulfillment draw
        for pid in plant_lookup:
            for t in periods:
                inbound = pulp.lpSum(Ship[(r["id"], t)] for r in dataset.routes if r.get("destination") == pid)
                outbound = pulp.lpSum(Ship[(r["id"], t)] for r in dataset.routes if r.get("source") == pid)
                prev_inv = inv0[pid] if t == 1 else Inv[(pid, t - 1)]

                prob += Inv[(pid, t)] == prev_inv + Prod[(pid, t)] + inbound - outbound - Fulfill[(pid, t)], f"inv_bal_{pid}_{t}"

        # Demand caps and min-fulfillment with slack
        for pid in plant_lookup:
            for t in periods:
                dem = float(demand.get(pid, [0.0] * dataset.periods)[t - 1])
                min_pct_series = min_ful.get(pid, [])
                min_pct = min_pct_series[t - 1] if len(min_pct_series) >= t else 0.0
                prob += Fulfill[(pid, t)] <= dem, f"fulfill_cap_{pid}_{t}"
                prob += Fulfill[(pid, t)] + Shortage[(pid, t)] == dem, f"demand_balance_{pid}_{t}"
                prob += Fulfill[(pid, t)] + SlackFul[(pid, t)] >= dem * min_pct, f"fulfill_min_{pid}_{t}"

        # Inventory bounds with slack
        for pid in plant_lookup:
            for t in periods:
                min_floor = float(min_close.get((pid, t), 0.0))
                max_cap = float(max_close.get((pid, t), float("inf")))
                prob += Inv[(pid, t)] + SlackMinStk[(pid, t)] >= min_floor, f"min_close_{pid}_{t}"
                if max_cap < float("inf"):
                    prob += Inv[(pid, t)] - SlackMaxStk[(pid, t)] <= max_cap, f"max_close_{pid}_{t}"

        # Optional flow constraints (IUGUConstraint)
        if getattr(dataset, "flow_constraints", None):
            for fc in dataset.flow_constraints:
                period = int(fc.get("period", 1))
                if period not in periods:
                    continue
                src = fc.get("source")
                dst = fc.get("destination")
                mode = fc.get("mode")
                bound_type = fc.get("type")
                value = float(fc.get("value", 0.0))

                relevant_routes = [
                    r for r in dataset.routes
                    if r.get("source") == src
                    and (dst is None or r.get("destination") == dst)
                    and (mode is None or r.get("mode") == mode)
                ]
                if not relevant_routes:
                    continue

                flow_expr = pulp.lpSum(Ship[(r["id"], period)] for r in relevant_routes)
                if bound_type == "E":
                    prob += flow_expr == value, f"flow_exact_{src}_{dst or 'all'}_{mode or 'all'}_{period}"
                elif bound_type == "L":
                    prob += flow_expr >= value, f"flow_min_{src}_{dst or 'all'}_{mode or 'all'}_{period}"
                elif bound_type == "G":
                    prob += flow_expr <= value, f"flow_max_{src}_{dst or 'all'}_{mode or 'all'}_{period}"

        # Global service level constraint if provided
        total_demand = sum(sum(demand.get(pid, [0.0] * dataset.periods)) for pid in demand_nodes)
        SlackService = None
        if model.service_level_target is not None and total_demand > 0:
            prob += (
                pulp.lpSum(Fulfill.values()) + (SlackService := pulp.LpVariable("s_service", lowBound=0))
                >= float(model.service_level_target) * total_demand
            ), "service_level_floor"

        shortage_penalty_eff = model.shortage_penalty if model.allow_shortage else max(model.shortage_penalty, 1_000_000.0)
        if model.strict_service:
            shortage_penalty_eff = max(shortage_penalty_eff, 1_000_000.0)

        # Objective: production + normalized transport + slack penalties
        objective = (
            pulp.lpSum(prod_cost[i][t - 1] * Prod[(i, t)] for i in iu_ids for t in periods)
            + pulp.lpSum(trip_cost[(rid, t)] * Trips[(rid, t)] for (rid, t) in trip_cost)
            + pulp.lpSum(hold_cost[i] * Inv[(i, t)] for i in plant_lookup for t in periods)
            + shortage_penalty_eff * pulp.lpSum(Shortage.values())
            + model.penalty_min_fulfillment * pulp.lpSum(SlackFul.values())
            + model.penalty_min_stock * pulp.lpSum(SlackMinStk.values())
            + model.penalty_max_stock * pulp.lpSum(SlackMaxStk.values())
            + (model.penalty_service_level * SlackService if SlackService is not None else 0)
        )
        prob += objective

        solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit) if time_limit else pulp.PULP_CBC_CMD(msg=False)
        prob.solve(solver)

        status_label = pulp.LpStatus[prob.status]
        feasible = prob.status not in {pulp.LpStatusInfeasible, pulp.LpStatusUnbounded}

        production_plan: Dict[Tuple[int, int], float] = {}
        shipment_plan: Dict[Tuple[int, int], float] = {}
        trips_plan: Dict[Tuple[int, int], float] = {}
        inventory_plan: Dict[Tuple[int, int], float] = {}
        fulfillment_plan: Dict[Tuple[int, int], float] = {}
        shortage_plan: Dict[Tuple[int, int], float] = {}
        slack_ful: Dict[Tuple[int, int], float] = {}
        slack_min: Dict[Tuple[int, int], float] = {}
        slack_max: Dict[Tuple[int, int], float] = {}
        slack_service = 0.0

        production_cost_total = transport_cost_total = holding_cost_total = shortage_cost_total = penalty_ful = penalty_min = penalty_max = 0.0

        if feasible:
            for key, var in Prod.items():
                val = max(pulp.value(var), 0.0)
                if val > 0:
                    production_plan[key] = val
            for key, var in Ship.items():
                val = max(pulp.value(var), 0.0)
                if val > 0:
                    shipment_plan[key] = val
            for key, var in Trips.items():
                val = max(pulp.value(var), 0.0)
                if val > 0:
                    trips_plan[key] = val
            for key, var in Inv.items():
                val = max(pulp.value(var), 0.0)
                if val > 0:
                    inventory_plan[key] = val
            for key, var in Fulfill.items():
                val = max(pulp.value(var), 0.0)
                if val > 0:
                    fulfillment_plan[key] = val
            for key, var in Shortage.items():
                val = max(pulp.value(var), 0.0)
                if val > 0:
                    shortage_plan[key] = val
            for key, var in SlackFul.items():
                val = max(pulp.value(var), 0.0)
                if val > 0:
                    slack_ful[key] = val
            for key, var in SlackMinStk.items():
                val = max(pulp.value(var), 0.0)
                if val > 0:
                    slack_min[key] = val
            for key, var in SlackMaxStk.items():
                val = max(pulp.value(var), 0.0)
                if val > 0:
                    slack_max[key] = val
            if SlackService is not None:
                slack_service = max(pulp.value(SlackService), 0.0)

            production_cost_total = sum(prod_cost[i][t - 1] * production_plan.get((i, t), 0.0) for i in iu_ids for t in periods)
            transport_cost_total = sum(trip_cost.get((rid, t), 0.0) * trips_plan.get((rid, t), 0.0) for (rid, t) in trip_cost)
            holding_cost_total = sum(hold_cost[i] * inventory_plan.get((i, t), 0.0) for i in plant_lookup for t in periods)
            shortage_cost_total = shortage_penalty_eff * sum(shortage_plan.values())
            penalty_ful = model.penalty_min_fulfillment * sum(slack_ful.values())
            penalty_min = model.penalty_min_stock * sum(slack_min.values())
            penalty_max = model.penalty_max_stock * sum(slack_max.values())
            penalty_service = (model.penalty_service_level * slack_service) if SlackService is not None else 0.0
        else:
            penalty_service = 0.0
        objective_value = production_cost_total + transport_cost_total + holding_cost_total + shortage_cost_total + penalty_ful + penalty_min + penalty_max
        objective_value += penalty_service
        runtime = time.monotonic() - start

        diagnostics: dict[str, object] = {
            "status": status_label,
            "mip_gap": getattr(prob, "mipGap", None),
            "slack_fulfillment_total": sum(slack_ful.values()),
            "slack_min_stock_total": sum(slack_min.values()),
            "slack_max_stock_total": sum(slack_max.values()),
            "slack_service_level": slack_service,
        }

        return SolverResult(
            feasible=feasible,
            status=status_label.lower(),
            objective_value=round(objective_value, 2),
            shipment_plan=shipment_plan,
            production_plan=production_plan,
            inventory_plan=inventory_plan,
            trips_plan=trips_plan,
            fulfillment_plan=fulfillment_plan,
            shortage_plan=shortage_plan,
            slack_min_fulfillment=slack_ful,
            slack_min_stock=slack_min,
            slack_max_stock=slack_max,
            slack_service_level=slack_service,
            cost_breakdown={
                "production_cost": round(production_cost_total, 2),
                "transport_cost": round(transport_cost_total, 2),
                "holding_cost": round(holding_cost_total, 2),
                "shortage_cost": round(shortage_cost_total, 2),
                "penalty_min_fulfillment": round(penalty_ful, 2),
                "penalty_min_stock": round(penalty_min, 2),
                "penalty_max_stock": round(penalty_max, 2),
                "penalty_service_level": round(penalty_service, 2),
                "total_cost": round(objective_value, 2),
            },
            diagnostics=diagnostics,
            runtime_seconds=runtime,
        )


class _GreedyCostSolver:
    """Deterministic heuristic solver when premium solvers are unavailable."""

    def solve(self, model: ModelDefinition, time_limit: int | None = None) -> SolverResult:
        start = time.monotonic()
        dataset = model.dataset
        periods = dataset.periods

        plant_lookup = {p["id"]: p for p in dataset.plants}
        iu_ids = [pid for pid, p in plant_lookup.items() if p.get("type") == "IU"]
        consumers = list(plant_lookup.keys())

        route_candidates = {}
        for route in dataset.routes:
            if route["destination"] in consumers and route["source"] in iu_ids:
                multiplier = float(dataset.batch_multipliers.get(route["id"], 1.0) or 1.0)
                freight_series = dataset.freight_costs.get(route["id"], [0.0] * periods)
                handling_series = dataset.handling_costs.get(route["id"], [0.0] * periods)
                unit_costs = [
                    (float(freight_series[idx]) + float(handling_series[idx])) / max(multiplier, 1e-6)
                    for idx in range(periods)
                ]
                route_candidates.setdefault(route["destination"], []).append({**route, "unit_costs": unit_costs, "multiplier": multiplier})

        shipment_plan: Dict[tuple[int, int], float] = {}
        production_plan: Dict[tuple[int, int], float] = {}
        inventory_plan: Dict[tuple[int, int], float] = {}
        fulfillment_plan: Dict[tuple[int, int], float] = {}

        transport_cost_total = 0.0
        feasible = True

        inventory_levels = {pid: float(dataset.inventory.get(pid, 0.0)) for pid in plant_lookup}
        total_demand_qty = sum(sum(dataset.demand.get(pid, [0.0] * periods)) for pid in plant_lookup)

        for period in range(1, periods + 1):
            # Produce at capacity for all IUs
            for iu in iu_ids:
                produced = float(plant_lookup[iu].get("production_capacity", 0.0))
                if produced > 0:
                    production_plan[(iu, period)] = produced
                inventory_levels[iu] = inventory_levels.get(iu, 0.0) + produced

            # Serve demand greedily
            for dest in consumers:
                demand_qty = float(dataset.demand.get(dest, [0.0] * periods)[period - 1])
                if demand_qty <= 0:
                    inventory_plan[(dest, period)] = inventory_levels.get(dest, 0.0)
                    continue
                current_inv = inventory_levels.get(dest, 0.0)
                remaining = max(demand_qty - current_inv, 0.0)
                delivered = min(current_inv, demand_qty)

                candidates = sorted(route_candidates.get(dest, []), key=lambda r: r["unit_costs"][period - 1])
                for route in candidates:
                    if remaining <= 0:
                        break
                    source = route["source"]
                    supply_avail = inventory_levels.get(source, 0.0)
                    if supply_avail <= 0:
                        continue
                    send = min(supply_avail, remaining)
                    inventory_levels[source] -= send
                    delivered += send
                    remaining -= send
                    shipment_plan[(route["id"], period)] = shipment_plan.get((route["id"], period), 0.0) + send
                    transport_cost_total += send * route["unit_costs"][period - 1]

                fulfilled = min(delivered, demand_qty)
                fulfillment_plan[(dest, period)] = fulfilled
                inventory_levels[dest] = max(current_inv + delivered - demand_qty, 0.0)
                inventory_plan[(dest, period)] = inventory_levels[dest]

        prod_cost_total = sum(float(plant_lookup[i].get("production_cost", 0.0)) * qty for (i, _), qty in production_plan.items())
        total_cost = prod_cost_total + transport_cost_total
        runtime = time.monotonic() - start

        diagnostics = {"solver_used": "greedy_fallback", "shortage_total": max(total_demand_qty - sum(fulfillment_plan.values()), 0.0)}

        return SolverResult(
            feasible=feasible,
            status="optimal" if feasible else "infeasible",
            objective_value=round(total_cost, 2),
            shipment_plan=shipment_plan,
            production_plan=production_plan,
            inventory_plan=inventory_plan,
            trips_plan={},
            fulfillment_plan=fulfillment_plan,
            shortage_plan={},
            slack_min_fulfillment={},
            slack_min_stock={},
            slack_max_stock={},
            cost_breakdown={
                "production_cost": round(prod_cost_total, 2),
                "transport_cost": round(transport_cost_total, 2),
                "total_cost": round(total_cost, 2),
            },
            diagnostics=diagnostics,
            runtime_seconds=runtime,
        )


class SolverAdapter:
    """Facade choosing the appropriate solver backend."""

    def __init__(self, preferred: str | None = None):
        self.preferred = preferred or "elastic"
        self._milp = _DeterministicMilpSolver()
        self._fallback = _GreedyCostSolver()

    def solve(
        self,
        model: ModelDefinition,
        scenarios: list | None = None,
        time_limit: int | None = None,
        mode_override: str | None = None,
    ) -> SolverResult:
        try:
            solved = self._milp.solve(model, time_limit=time_limit)
            solved.diagnostics["solver_used"] = "cbc"
            return solved
        except SolverError as exc:
            fallback = self._fallback.solve(model, time_limit=time_limit)
            fallback.diagnostics["fallback_reason"] = str(exc)
            fallback.diagnostics["solver_used"] = "greedy_fallback"
            return fallback
        except Exception as exc:  # pragma: no cover - defensive
            raise SolverError(str(exc)) from exc
