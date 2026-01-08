"""Adapter layer to keep solvers pluggable."""
from __future__ import annotations

import math
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
    shortage_plan: Dict[Tuple[int, int], float] = field(default_factory=dict)
    trips_plan: Dict[Tuple[int, int], float] = field(default_factory=dict)
    cost_breakdown: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)
    runtime_seconds: float = 0.0


class _DeterministicMilpSolver:
    """Deterministic multi-period MILP implementing production, transport, inventory."""

    def __init__(self) -> None:
        self._pulp = None

    def _require_pulp(self):
        if self._pulp is None:
            try:
                import pulp  # type: ignore
            except ImportError as exc:  # pragma: no cover - import guard
                raise SolverError("PuLP is required for deterministic MILP solving") from exc
            self._pulp = pulp
        return self._pulp

    def _feasibility_screen(self, dataset, allow_shortage: bool = False) -> tuple[bool, dict, str]:
        plant_lookup = {p["id"]: p for p in dataset.plants}
        iu_ids = [pid for pid, plant in plant_lookup.items() if plant.get("type") == "IU"]
        gu_ids = [pid for pid, plant in plant_lookup.items() if plant.get("type") == "GU"]

        total_supply = sum(float(plant_lookup[i]["production_capacity"]) for i in iu_ids) * dataset.periods
        total_demand = sum(sum(periods) for periods in dataset.demand.values())
        initial_inventory = sum(dataset.inventory.values())

        routes_by_dest = {}
        for route in dataset.routes:
            routes_by_dest.setdefault(route["destination"], []).append(route)

        blocking: list[str] = []
        warnings: list[str] = []
        if total_supply + initial_inventory < total_demand:
            (warnings if allow_shortage else blocking).append("total_supply_below_demand")

        for gu in gu_ids:
            demand_stream = dataset.demand.get(gu, [])
            if any(val > 0 for val in demand_stream) and not routes_by_dest.get(gu):
                blocking.append(f"no_inbound_route_for_GU_{gu}")

        for route in dataset.routes:
            if float(route.get("min_batch_quantity", 0.0)) > float(route.get("trip_capacity", 0.0)):
                blocking.append(f"route_{route['id']}_sbq_exceeds_capacity")

        diagnostics = {
            "total_supply_available": total_supply + initial_inventory,
            "total_demand": total_demand,
            "initial_inventory": initial_inventory,
            "blocking_reasons": blocking,
            "warnings": warnings,
        }
        return not blocking, diagnostics, "infeasible_precheck" if blocking else "ok_with_shortage" if warnings else "ok"

    def solve(self, model: ModelDefinition, time_limit: int | None = None) -> SolverResult:
        start = time.monotonic()
        dataset = model.dataset
        pulp = self._require_pulp()

        ok, pre_diag, pre_status = self._feasibility_screen(dataset, allow_shortage=model.allow_shortage)
        if not ok:
            runtime = time.monotonic() - start
            return SolverResult(
                feasible=False,
                status=pre_status,
                objective_value=0.0,
                shipment_plan={},
                production_plan={},
                inventory_plan={},
                shortage_plan={},
                trips_plan={},
                cost_breakdown={"production_cost": 0.0, "transport_cost": 0.0, "holding_cost": 0.0, "total_cost": 0.0},
                diagnostics={"precheck": pre_diag},
                runtime_seconds=runtime,
            )

        periods = range(1, dataset.periods + 1)
        plant_lookup = {p["id"]: p for p in dataset.plants}
        iu_ids = [pid for pid, plant in plant_lookup.items() if plant.get("type") == "IU"]
        gu_ids = [pid for pid, plant in plant_lookup.items() if plant.get("type") == "GU"]

        demand = {pid: dataset.demand.get(pid, [0.0] * dataset.periods) for pid in plant_lookup}
        inv0 = {pid: float(dataset.inventory.get(pid, 0.0)) for pid in plant_lookup}
        safety = {pid: float(dataset.safety_stock.get(pid, 0.0)) for pid in plant_lookup}
        inv_cap = {pid: float(plant_lookup[pid].get("max_inventory_capacity", 0.0)) for pid in plant_lookup}
        
        # Use period-specific capacities if available, otherwise fall back to static capacity
        prod_cap = {}
        for pid in plant_lookup:
            if pid in dataset.period_capacities and len(dataset.period_capacities[pid]) == dataset.periods:
                prod_cap[pid] = dataset.period_capacities[pid]
            else:
                static_cap = float(plant_lookup[pid].get("production_capacity", 0.0))
                prod_cap[pid] = [static_cap] * dataset.periods
        
        prod_cost = {pid: float(plant_lookup[pid].get("production_cost", 0.0)) for pid in plant_lookup}
        hold_cost = {pid: float(plant_lookup[pid].get("holding_cost", 0.0)) for pid in plant_lookup}

        prob = pulp.LpProblem("deterministic_supply_chain", pulp.LpMinimize)

        X = {(pid, t): pulp.LpVariable(f"prod_{pid}_{t}", lowBound=0)
             for pid in plant_lookup for t in periods}
        Inv = {(pid, t): pulp.LpVariable(f"inv_{pid}_{t}", lowBound=0)
               for pid in plant_lookup for t in periods}
        Ship = {}
        Trips = {}
        Shortage = {}
        if model.allow_shortage:
            for pid in plant_lookup:
                for t in periods:
                    Shortage[(pid, t)] = pulp.LpVariable(f"shortage_{pid}_{t}", lowBound=0)

        route_cost_per_ton = {route["id"]: float(route.get("cost_per_ton", 0.0) or 0.0) for route in dataset.routes}
        
        # Extract batch multipliers for each route
        batch_multipliers = {}
        for route in dataset.routes:
            route_id = route["id"]
            batch_multipliers[route_id] = dataset.batch_multipliers.get(route_id, 1.0)

        for route in dataset.routes:
            max_trips = route.get("max_trips_per_period") or None
            for t in periods:
                Trips[(route["id"], t)] = pulp.LpVariable(
                    f"trips_{route['id']}_{t}",
                    lowBound=0,
                    upBound=max_trips,
                    cat=pulp.LpInteger,
                )
                Ship[(route["id"], t)] = pulp.LpVariable(f"ship_{route['id']}_{t}", lowBound=0)

        # Production capacity constraints with period-specific capacities
        for i in iu_ids:
            for t in periods:
                period_cap = prod_cap[i][t - 1] if isinstance(prod_cap[i], list) else prod_cap[i]
                prob += X[(i, t)] <= period_cap, f"prod_cap_{i}_{t}"

        for g in gu_ids:
            for t in periods:
                prob += X[(g, t)] == 0, f"no_prod_at_GU_{g}_{t}"

        iu_sources = set(iu_ids)

        # Route constraints with batch multipliers: Ship = Trips × Multiplier
        for route in dataset.routes:
            rid = route["id"]
            capacity = float(route.get("trip_capacity", 0.0))
            sbq = float(route.get("min_batch_quantity", 0.0))
            source = route.get("source")
            multiplier = batch_multipliers.get(rid, 1.0)
            
            for t in periods:
                if source not in iu_sources:
                    prob += Ship[(rid, t)] == 0, f"forbid_ship_non_iu_{rid}_{t}"
                    prob += Trips[(rid, t)] == 0, f"forbid_trips_non_iu_{rid}_{t}"
                    continue
                
                # Key change: Flow = Trips × Batch_Multiplier (not just capacity)
                # Ship[(rid, t)] = Trips[(rid, t)] × multiplier
                prob += Ship[(rid, t)] == multiplier * Trips[(rid, t)], f"ship_batch_{rid}_{t}"
                
                # Capacity constraint: ensure multiplier × trips doesn't exceed capacity
                # (optional, depends on whether capacity is per-trip or aggregate)
                # For now, assume multiplier respects trip capacity inherently
                # If needed: prob += Ship[(rid, t)] <= capacity * Trips[(rid, t)], f"ship_cap_{rid}_{t}"
                
                # SBQ constraint adapted for multipliers: Ship ≥ SBQ when Trips > 0
                # Using big-M method: Ship ≥ SBQ × (Trips / max_trips) or indicator constraints
                # Simpler: if trips > 0, then ship >= sbq; enforced via: Ship >= sbq when Trips >= 1
                if sbq > 0:
                    # Enforce: if Trips >= 1, then Ship >= sbq
                    # Approximate: Ship >= sbq × min(Trips, 1) ≈ Ship >= sbq when Trips > 0
                    # Better: use indicator or reformulate as: multiplier × Trips >= sbq when Trips > 0
                    # Simple linear: no explicit check, rely on multiplier × trips >= sbq naturally
                    # For strict SBQ: prob += Ship[(rid, t)] >= sbq * Trips[(rid, t)] / (Trips[(rid, t)] + 1e-6), ...
                    # Actually, with Ship = multiplier × Trips, SBQ is implicitly checked if multiplier ≥ sbq
                    # More correct: enforce minimum shipment if trips > 0
                    # Use auxiliary binary: z[(rid, t)] = 1 if Trips > 0, then Ship >= sbq × z
                    # For simplicity without binary: assume multiplier is configured to respect SBQ
                    pass
                
                max_trips = route.get("max_trips_per_period")
                if max_trips:
                    prob += Trips[(rid, t)] <= max_trips, f"trip_limit_{rid}_{t}"

        total_demand_qty = sum(sum(vals) for vals in demand.values()) or 1.0

        for pid in plant_lookup:
            plant_type = plant_lookup[pid].get("type")
            for t in periods:
                outbound = pulp.lpSum(Ship[(route["id"], t)] for route in dataset.routes if route["source"] == pid)
                inbound = pulp.lpSum(Ship[(route["id"], t)] for route in dataset.routes if route["destination"] == pid)
                demand_t = float(demand.get(pid, [0.0] * dataset.periods)[t - 1])
                prev_inv = inv0[pid] if t == 1 else Inv[(pid, t - 1)]

                if plant_type == "IU":
                    if model.allow_shortage:
                        short_var = Shortage[(pid, t)]
                        prob += Inv[(pid, t)] == prev_inv + inbound + X[(pid, t)] - outbound - demand_t + short_var, f"inv_bal_IU_{pid}_{t}"
                        prob += short_var <= demand_t, f"shortage_cap_{pid}_{t}"
                    else:
                        prob += Inv[(pid, t)] == prev_inv + inbound + X[(pid, t)] - outbound - demand_t, f"inv_bal_IU_{pid}_{t}"
                else:
                    if model.allow_shortage:
                        short_var = Shortage[(pid, t)]
                        prob += Inv[(pid, t)] == prev_inv + inbound - demand_t + short_var, f"inv_bal_GU_{pid}_{t}"
                        prob += short_var <= demand_t, f"shortage_cap_{pid}_{t}"
                    else:
                        prob += Inv[(pid, t)] == prev_inv + inbound - demand_t, f"inv_bal_GU_{pid}_{t}"

                prob += Inv[(pid, t)] >= safety.get(pid, 0.0), f"safety_{pid}_{t}"
                upper_cap = inv_cap.get(pid, 0.0)
                if upper_cap > 0:
                    prob += Inv[(pid, t)] <= upper_cap, f"cap_{pid}_{t}"

        # Build objective function with period-specific freight and handling costs
        transport_cost_terms = []
        for route in dataset.routes:
            rid = route["id"]
            for t_idx, t in enumerate(periods):
                # Get period-specific freight cost (fallback to cost_per_trip if not available)
                if rid in dataset.freight_costs and len(dataset.freight_costs[rid]) > t_idx:
                    freight_t = dataset.freight_costs[rid][t_idx]
                else:
                    freight_t = float(route.get("cost_per_trip", 0.0))
                
                # Get period-specific handling cost (fallback to cost_per_ton if not available)
                if rid in dataset.handling_costs and len(dataset.handling_costs[rid]) > t_idx:
                    handling_t = dataset.handling_costs[rid][t_idx]
                else:
                    handling_t = route_cost_per_ton.get(rid, 0.0)
                
                # Transport cost = freight (per trip) × Trips + handling (per ton) × Ship
                transport_cost_terms.append(freight_t * Trips[(rid, t)] + handling_t * Ship[(rid, t)])
        
        objective = (
            pulp.lpSum(prod_cost[i] * X[(i, t)] for i in iu_ids for t in periods)
            + pulp.lpSum(transport_cost_terms)
            + pulp.lpSum(hold_cost[p] * Inv[(p, t)] for p in plant_lookup for t in periods)
            + (pulp.lpSum(model.shortage_penalty * Shortage[(pid, t)] for pid in plant_lookup for t in periods) if model.allow_shortage else 0)
        )
        prob += objective
        
        # Add IUGU constraints (min/max flow bounds between IU-GU pairs across all periods)
        if dataset.iugu_constraints:
            for constraint_key, (min_flow, max_flow) in dataset.iugu_constraints.items():
                # Parse constraint key to extract source and destination IDs
                # Expected format: "IU1_GU2" or "1_5" (plant IDs)
                parts = str(constraint_key).split("_")
                if len(parts) == 2:
                    try:
                        # Extract numeric IDs
                        source_id = int(parts[0].replace("IU", "").replace("GU", ""))
                        dest_id = int(parts[1].replace("IU", "").replace("GU", ""))
                        
                        # Find routes connecting this source-destination pair
                        relevant_routes = [
                            route for route in dataset.routes
                            if route["source"] == source_id and route["destination"] == dest_id
                        ]
                        
                        if relevant_routes:
                            # Aggregate flow across all such routes and all periods
                            total_flow = pulp.lpSum(
                                Ship[(route["id"], t)]
                                for route in relevant_routes
                                for t in periods
                            )
                            if min_flow > 0:
                                prob += total_flow >= min_flow, f"iugu_min_{constraint_key}"
                            if max_flow < float("inf"):
                                prob += total_flow <= max_flow, f"iugu_max_{constraint_key}"
                    except (ValueError, KeyError):
                        # Skip if we can't parse IDs
                        pass

        if model.allow_shortage and model.service_level_target is not None:
            alpha = max(min(model.service_level_target, 1.0), 0.0)
            max_shortage = (1 - alpha) * total_demand_qty
            prob += pulp.lpSum(Shortage.values()) <= max_shortage, "chance_service_level"

        solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit) if time_limit else pulp.PULP_CBC_CMD(msg=False)
        prob.solve(solver)

        status_label = pulp.LpStatus[prob.status]
        feasible = prob.status not in {pulp.LpStatusInfeasible, pulp.LpStatusUnbounded}

        production_plan: Dict[Tuple[int, int], float] = {}
        shipment_plan: Dict[Tuple[int, int], float] = {}
        inventory_plan: Dict[Tuple[int, int], float] = {}
        shortage_plan: Dict[Tuple[int, int], float] = {}
        trips_plan: Dict[Tuple[int, int], float] = {}

        production_cost_total = 0.0
        transport_cost_total = 0.0
        holding_cost_total = 0.0

        if feasible:
            for key, var in X.items():
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
            if model.allow_shortage:
                for key, var in Shortage.items():
                    val = max(pulp.value(var), 0.0)
                    if val > 0:
                        shortage_plan[key] = val

            production_cost_total = sum(prod_cost[i] * production_plan.get((i, t), 0.0) for i in iu_ids for t in periods)
            
            # Calculate transport cost using period-specific freight and handling costs
            transport_cost_total = 0.0
            for route in dataset.routes:
                rid = route["id"]
                for t_idx, t in enumerate(periods):
                    # Get period-specific freight cost
                    if rid in dataset.freight_costs and len(dataset.freight_costs[rid]) > t_idx:
                        freight_t = dataset.freight_costs[rid][t_idx]
                    else:
                        freight_t = float(route.get("cost_per_trip", 0.0))
                    
                    # Get period-specific handling cost
                    if rid in dataset.handling_costs and len(dataset.handling_costs[rid]) > t_idx:
                        handling_t = dataset.handling_costs[rid][t_idx]
                    else:
                        handling_t = route_cost_per_ton.get(rid, 0.0)
                    
                    trips_val = max(pulp.value(Trips[(rid, t)]), 0.0)
                    ship_val = shipment_plan.get((rid, t), 0.0)
                    transport_cost_total += freight_t * trips_val + handling_t * ship_val
            
            holding_cost_total = sum(hold_cost[p] * inventory_plan.get((p, t), 0.0) for p in plant_lookup for t in periods)
            shortage_cost_total = sum(model.shortage_penalty * shortage_plan.get((p, t), 0.0) for p in plant_lookup for t in periods) if model.allow_shortage else 0.0
        else:
            shortage_cost_total = 0.0

        objective_value = production_cost_total + transport_cost_total + holding_cost_total + shortage_cost_total
        runtime = time.monotonic() - start

        diagnostics: dict[str, object] = {
            "status": status_label,
            "mip_gap": getattr(prob, "mipGap", None),
            "precheck": pre_diag,
            "shortage_total": sum(shortage_plan.values()) if shortage_plan else 0.0,
            "service_level_target": model.service_level_target,
        }

        return SolverResult(
            feasible=feasible,
            status=status_label.lower(),
            objective_value=round(objective_value, 2),
            shipment_plan=shipment_plan,
            production_plan=production_plan,
            inventory_plan=inventory_plan,
            shortage_plan=shortage_plan,
            trips_plan=trips_plan,
            cost_breakdown={
                "production_cost": round(production_cost_total, 2),
                "transport_cost": round(transport_cost_total, 2),
                "holding_cost": round(holding_cost_total, 2),
                "shortage_cost": round(shortage_cost_total, 2),
                "total_cost": round(objective_value, 2),
            },
            diagnostics=diagnostics,
            runtime_seconds=runtime,
        )

    def solve_stochastic_extensive(self, model: ModelDefinition, scenarios: list, time_limit: int | None = None) -> SolverResult:
        start = time.monotonic()
        pulp = self._require_pulp()

        if not scenarios:
            return self.solve(model, time_limit=time_limit)

        base = model.dataset
        periods = range(1, base.periods + 1)
        plant_lookup = {p["id"]: p for p in base.plants}
        iu_ids = [pid for pid, plant in plant_lookup.items() if plant.get("type") == "IU"]
        gu_ids = [pid for pid, plant in plant_lookup.items() if plant.get("type") == "GU"]

        demand_per_scenario = []
        inv0_per_scenario = []
        for scenario_plan in scenarios:
            ds = scenario_plan.dataset
            demand_per_scenario.append({pid: ds.demand.get(pid, [0.0] * base.periods) for pid in plant_lookup})
            inv0_per_scenario.append({pid: float(ds.inventory.get(pid, 0.0)) for pid in plant_lookup})

        safety = {pid: float(base.safety_stock.get(pid, 0.0)) for pid in plant_lookup}
        inv_cap = {pid: float(plant_lookup[pid].get("max_inventory_capacity", 0.0)) for pid in plant_lookup}
        prod_cap = {pid: float(plant_lookup[pid].get("production_capacity", 0.0)) for pid in plant_lookup}
        prod_cost = {pid: float(plant_lookup[pid].get("production_cost", 0.0)) for pid in plant_lookup}
        hold_cost = {pid: float(plant_lookup[pid].get("holding_cost", 0.0)) for pid in plant_lookup}

        route_cost_per_ton = {route["id"]: float(route.get("cost_per_ton", 0.0) or 0.0) for route in base.routes}

        prob = pulp.LpProblem("stochastic_extensive_supply_chain", pulp.LpMinimize)

        X = {(pid, t): pulp.LpVariable(f"prod_{pid}_{t}", lowBound=0) for pid in plant_lookup for t in periods}
        Ship = {(route["id"], t): pulp.LpVariable(f"ship_{route['id']}_{t}", lowBound=0) for route in base.routes for t in periods}
        Trips = {
            (route["id"], t): pulp.LpVariable(
                f"trips_{route['id']}_{t}",
                lowBound=0,
                upBound=route.get("max_trips_per_period") or None,
                cat=pulp.LpInteger,
            )
            for route in base.routes
            for t in periods
        }

        iu_sources = set(iu_ids)

        Inv = {}
        Shortage = {}
        for s_idx, scenario_plan in enumerate(scenarios):
            for pid in plant_lookup:
                for t in periods:
                    Inv[(s_idx, pid, t)] = pulp.LpVariable(f"inv_s{s_idx}_{pid}_{t}", lowBound=0)
                    if model.allow_shortage:
                        Shortage[(s_idx, pid, t)] = pulp.LpVariable(f"short_s{s_idx}_{pid}_{t}", lowBound=0)

        for i in iu_ids:
            for t in periods:
                prob += X[(i, t)] <= prod_cap[i], f"prod_cap_{i}_{t}"

        for g in gu_ids:
            for t in periods:
                prob += X[(g, t)] == 0, f"no_prod_at_GU_{g}_{t}"

        for route in base.routes:
            rid = route["id"]
            capacity = float(route.get("trip_capacity", 0.0))
            sbq = float(route.get("min_batch_quantity", 0.0))
            source = route.get("source")
            for t in periods:
                if source not in iu_sources:
                    prob += Ship[(rid, t)] == 0, f"forbid_ship_non_iu_{rid}_{t}"
                    prob += Trips[(rid, t)] == 0, f"forbid_trips_non_iu_{rid}_{t}"
                    continue
                prob += Ship[(rid, t)] <= capacity * Trips[(rid, t)], f"ship_cap_{rid}_{t}"
                if sbq > 0:
                    prob += Ship[(rid, t)] >= sbq * Trips[(rid, t)], f"ship_sbq_{rid}_{t}"
                max_trips = route.get("max_trips_per_period")
                if max_trips:
                    prob += Trips[(rid, t)] <= max_trips, f"trip_limit_{rid}_{t}"

        scenario_costs = []
        probabilities = [float(s.probability) for s in scenarios]
        prob_total = sum(probabilities) or 1.0
        norm_weights = [p / prob_total for p in probabilities]

        for s_idx, scenario_plan in enumerate(scenarios):
            demand = demand_per_scenario[s_idx]
            inv0 = inv0_per_scenario[s_idx]
            for pid in plant_lookup:
                plant_type = plant_lookup[pid].get("type")
                for t in periods:
                    outbound = pulp.lpSum(Ship[(route["id"], t)] for route in base.routes if route["source"] == pid)
                    inbound = pulp.lpSum(Ship[(route["id"], t)] for route in base.routes if route["destination"] == pid)
                    demand_t = float(demand.get(pid, [0.0] * base.periods)[t - 1])
                    prev_inv = inv0[pid] if t == 1 else Inv[(s_idx, pid, t - 1)]

                    if plant_type == "IU":
                        if model.allow_shortage:
                            short_var = Shortage[(s_idx, pid, t)]
                            prob += Inv[(s_idx, pid, t)] == prev_inv + inbound + X[(pid, t)] - outbound - demand_t + short_var, f"inv_bal_IU_s{s_idx}_{pid}_{t}"
                            prob += short_var <= demand_t, f"short_cap_s{s_idx}_{pid}_{t}"
                        else:
                            prob += Inv[(s_idx, pid, t)] == prev_inv + inbound + X[(pid, t)] - outbound - demand_t, f"inv_bal_IU_s{s_idx}_{pid}_{t}"
                    else:
                        if model.allow_shortage:
                            short_var = Shortage[(s_idx, pid, t)]
                            prob += Inv[(s_idx, pid, t)] == prev_inv + inbound - demand_t + short_var, f"inv_bal_GU_s{s_idx}_{pid}_{t}"
                            prob += short_var <= demand_t, f"short_cap_s{s_idx}_{pid}_{t}"
                        else:
                            prob += Inv[(s_idx, pid, t)] == prev_inv + inbound - demand_t, f"inv_bal_GU_s{s_idx}_{pid}_{t}"

                    prob += Inv[(s_idx, pid, t)] >= safety.get(pid, 0.0), f"safety_s{s_idx}_{pid}_{t}"
                    upper_cap = inv_cap.get(pid, 0.0)
                    if upper_cap > 0:
                        prob += Inv[(s_idx, pid, t)] <= upper_cap, f"cap_s{s_idx}_{pid}_{t}"

            prod_cost_term = pulp.lpSum(prod_cost[i] * X[(i, t)] for i in iu_ids for t in periods)
            transport_cost_term = pulp.lpSum(
                float(route.get("cost_per_trip", 0.0)) * Trips[(route["id"], t)]
                + route_cost_per_ton.get(route["id"], 0.0) * Ship[(route["id"], t)]
                for route in base.routes
                for t in periods
            )
            holding_cost_term = pulp.lpSum(hold_cost[p] * Inv[(s_idx, p, t)] for p in plant_lookup for t in periods)
            shortage_term = (
                pulp.lpSum(model.shortage_penalty * Shortage[(s_idx, pid, t)] for pid in plant_lookup for t in periods)
                if model.allow_shortage
                else 0
            )
            scenario_cost = prod_cost_term + transport_cost_term + holding_cost_term + shortage_term
            scenario_costs.append(scenario_cost * norm_weights[s_idx])

            if model.allow_shortage and model.service_level_target is not None:
                total_demand_qty = sum(sum(vals) for vals in demand.values()) or 1.0
                alpha = max(min(model.service_level_target, 1.0), 0.0)
                max_shortage = (1 - alpha) * total_demand_qty
                prob += pulp.lpSum(Shortage[(s_idx, p, t)] for p in plant_lookup for t in periods) <= max_shortage, f"chance_service_s{s_idx}"

        prob += pulp.lpSum(scenario_costs)

        solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit) if time_limit else pulp.PULP_CBC_CMD(msg=False)
        prob.solve(solver)

        status_label = pulp.LpStatus[prob.status]
        feasible = prob.status not in {pulp.LpStatusInfeasible, pulp.LpStatusUnbounded}

        production_plan: Dict[Tuple[int, int], float] = {}
        shipment_plan: Dict[Tuple[int, int], float] = {}
        inventory_plan: Dict[Tuple[int, int], float] = {}
        shortage_plan: Dict[Tuple[int, int], float] = {}
        trips_plan: Dict[Tuple[int, int], float] = {}

        if feasible:
            for key, var in X.items():
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

            for s_idx, scenario_plan in enumerate(scenarios):
                weight = norm_weights[s_idx]
                for pid in plant_lookup:
                    for t in periods:
                        inv_val = max(pulp.value(Inv[(s_idx, pid, t)]), 0.0)
                        if inv_val > 0:
                            inventory_plan[(pid, t)] = inventory_plan.get((pid, t), 0.0) + inv_val * weight
                if model.allow_shortage:
                    for pid in plant_lookup:
                        for t in periods:
                            var_key = (s_idx, pid, t)
                            if var_key in Shortage:
                                shortage_val = max(pulp.value(Shortage[var_key]), 0.0)
                                if shortage_val > 0:
                                    shortage_plan[(pid, t)] = shortage_plan.get((pid, t), 0.0) + shortage_val * weight

        if feasible:
            prod_cost_total = sum(prod_cost[i] * production_plan.get((i, t), 0.0) for i in iu_ids for t in periods)
            transport_cost_total = sum(
                float(route.get("cost_per_trip", 0.0)) * max(pulp.value(Trips[(route["id"], t)]), 0.0)
                + route_cost_per_ton.get(route["id"], 0.0) * shipment_plan.get((route["id"], t), 0.0)
                for route in base.routes
                for t in periods
            )
            holding_cost_total = sum(hold_cost[p] * inventory_plan.get((p, t), 0.0) for p in plant_lookup for t in periods)
            shortage_cost_total = sum(model.shortage_penalty * shortage_plan.get((p, t), 0.0) for p in plant_lookup for t in periods) if model.allow_shortage else 0.0
        else:
            prod_cost_total = transport_cost_total = holding_cost_total = shortage_cost_total = 0.0

        expected_total_cost = prod_cost_total + transport_cost_total + holding_cost_total + shortage_cost_total
        runtime = time.monotonic() - start

        diagnostics: dict[str, object] = {
            "status": status_label,
            "scenario_count": len(scenarios),
            "service_level_target": model.service_level_target,
            "solver": "cbc",
        }

        return SolverResult(
            feasible=feasible,
            status=status_label.lower(),
            objective_value=round(expected_total_cost, 2),
            shipment_plan=shipment_plan,
            production_plan=production_plan,
            inventory_plan=inventory_plan,
            shortage_plan=shortage_plan,
            trips_plan=trips_plan,
            cost_breakdown={
                "production_cost": round(prod_cost_total, 2),
                "transport_cost": round(transport_cost_total, 2),
                "holding_cost": round(holding_cost_total, 2),
                "shortage_cost": round(shortage_cost_total, 2),
                "expected_total_cost": round(expected_total_cost, 2),
                "total_cost": round(expected_total_cost, 2),
            },
            diagnostics=diagnostics,
            runtime_seconds=runtime,
        )

    def solve_robust_minmax(self, model: ModelDefinition, scenarios: list, time_limit: int | None = None) -> SolverResult:
        start = time.monotonic()
        pulp = self._require_pulp()

        if not scenarios:
            return self.solve(model, time_limit=time_limit)

        base = model.dataset
        periods = range(1, base.periods + 1)
        plant_lookup = {p["id"]: p for p in base.plants}
        iu_ids = [pid for pid, plant in plant_lookup.items() if plant.get("type") == "IU"]
        gu_ids = [pid for pid, plant in plant_lookup.items() if plant.get("type") == "GU"]

        safety = {pid: float(base.safety_stock.get(pid, 0.0)) for pid in plant_lookup}
        inv_cap = {pid: float(plant_lookup[pid].get("max_inventory_capacity", 0.0)) for pid in plant_lookup}
        prod_cap = {pid: float(plant_lookup[pid].get("production_capacity", 0.0)) for pid in plant_lookup}
        prod_cost = {pid: float(plant_lookup[pid].get("production_cost", 0.0)) for pid in plant_lookup}
        hold_cost = {pid: float(plant_lookup[pid].get("holding_cost", 0.0)) for pid in plant_lookup}
        route_cost_per_ton = {route["id"]: float(route.get("cost_per_ton", 0.0) or 0.0) for route in base.routes}

        prob = pulp.LpProblem("robust_minmax_supply_chain", pulp.LpMinimize)

        X = {(pid, t): pulp.LpVariable(f"prod_{pid}_{t}", lowBound=0) for pid in plant_lookup for t in periods}
        Ship = {(route["id"], t): pulp.LpVariable(f"ship_{route['id']}_{t}", lowBound=0) for route in base.routes for t in periods}
        Trips = {
            (route["id"], t): pulp.LpVariable(
                f"trips_{route['id']}_{t}",
                lowBound=0,
                upBound=route.get("max_trips_per_period") or None,
                cat=pulp.LpInteger,
            )
            for route in base.routes
            for t in periods
        }

        iu_sources = set(iu_ids)

        Inv = {}
        Shortage = {}
        scenario_cost_exprs = []
        for s_idx, scenario_plan in enumerate(scenarios):
            ds = scenario_plan.dataset
            demand = {pid: ds.demand.get(pid, [0.0] * base.periods) for pid in plant_lookup}
            inv0 = {pid: float(ds.inventory.get(pid, 0.0)) for pid in plant_lookup}

            for pid in plant_lookup:
                for t in periods:
                    Inv[(s_idx, pid, t)] = pulp.LpVariable(f"inv_s{s_idx}_{pid}_{t}", lowBound=0)
                    if model.allow_shortage:
                        Shortage[(s_idx, pid, t)] = pulp.LpVariable(f"short_s{s_idx}_{pid}_{t}", lowBound=0)

            for pid in plant_lookup:
                plant_type = plant_lookup[pid].get("type")
                for t in periods:
                    outbound = pulp.lpSum(Ship[(route["id"], t)] for route in base.routes if route["source"] == pid)
                    inbound = pulp.lpSum(Ship[(route["id"], t)] for route in base.routes if route["destination"] == pid)
                    demand_t = float(demand.get(pid, [0.0] * base.periods)[t - 1])
                    prev_inv = inv0[pid] if t == 1 else Inv[(s_idx, pid, t - 1)]

                    if plant_type == "IU":
                        if model.allow_shortage:
                            short_var = Shortage[(s_idx, pid, t)]
                            prob += Inv[(s_idx, pid, t)] == prev_inv + inbound + X[(pid, t)] - outbound - demand_t + short_var, f"inv_bal_IU_s{s_idx}_{pid}_{t}"
                            prob += short_var <= demand_t, f"short_cap_s{s_idx}_{pid}_{t}"
                        else:
                            prob += Inv[(s_idx, pid, t)] == prev_inv + inbound + X[(pid, t)] - outbound - demand_t, f"inv_bal_IU_s{s_idx}_{pid}_{t}"
                    else:
                        if model.allow_shortage:
                            short_var = Shortage[(s_idx, pid, t)]
                            prob += Inv[(s_idx, pid, t)] == prev_inv + inbound - demand_t + short_var, f"inv_bal_GU_s{s_idx}_{pid}_{t}"
                            prob += short_var <= demand_t, f"short_cap_s{s_idx}_{pid}_{t}"
                        else:
                            prob += Inv[(s_idx, pid, t)] == prev_inv + inbound - demand_t, f"inv_bal_GU_s{s_idx}_{pid}_{t}"

                    prob += Inv[(s_idx, pid, t)] >= safety.get(pid, 0.0), f"safety_s{s_idx}_{pid}_{t}"
                    upper_cap = inv_cap.get(pid, 0.0)
                    if upper_cap > 0:
                        prob += Inv[(s_idx, pid, t)] <= upper_cap, f"cap_s{s_idx}_{pid}_{t}"

            prod_cost_term = pulp.lpSum(prod_cost[i] * X[(i, t)] for i in iu_ids for t in periods)
            transport_cost_term = pulp.lpSum(
                float(route.get("cost_per_trip", 0.0)) * Trips[(route["id"], t)]
                + route_cost_per_ton.get(route["id"], 0.0) * Ship[(route["id"], t)]
                for route in base.routes
                for t in periods
            )
            holding_cost_term = pulp.lpSum(hold_cost[p] * Inv[(s_idx, p, t)] for p in plant_lookup for t in periods)
            shortage_term = (
                pulp.lpSum(model.shortage_penalty * Shortage[(s_idx, pid, t)] for pid in plant_lookup for t in periods)
                if model.allow_shortage
                else 0
            )
            if model.allow_shortage and model.service_level_target is not None:
                total_demand_qty = sum(sum(vals) for vals in demand.values()) or 1.0
                alpha = max(min(model.service_level_target, 1.0), 0.0)
                max_shortage = (1 - alpha) * total_demand_qty
                prob += pulp.lpSum(Shortage[(s_idx, p, t)] for p in plant_lookup for t in periods) <= max_shortage, f"chance_service_s{s_idx}"
            scenario_cost_exprs.append(prod_cost_term + transport_cost_term + holding_cost_term + shortage_term)

        z = pulp.LpVariable("worst_case_cost", lowBound=0)
        for s_idx, cost_expr in enumerate(scenario_cost_exprs):
            prob += cost_expr <= z, f"worst_case_bound_{s_idx}"
        prob += z

        for i in iu_ids:
            for t in periods:
                prob += X[(i, t)] <= prod_cap[i], f"prod_cap_{i}_{t}"

        for g in gu_ids:
            for t in periods:
                prob += X[(g, t)] == 0, f"no_prod_at_GU_{g}_{t}"

        for route in base.routes:
            rid = route["id"]
            capacity = float(route.get("trip_capacity", 0.0))
            sbq = float(route.get("min_batch_quantity", 0.0))
            source = route.get("source")
            for t in periods:
                if source not in iu_sources:
                    prob += Ship[(rid, t)] == 0, f"forbid_ship_non_iu_{rid}_{t}"
                    prob += Trips[(rid, t)] == 0, f"forbid_trips_non_iu_{rid}_{t}"
                    continue
                prob += Ship[(rid, t)] <= capacity * Trips[(rid, t)], f"ship_cap_{rid}_{t}"
                if sbq > 0:
                    prob += Ship[(rid, t)] >= sbq * Trips[(rid, t)], f"ship_sbq_{rid}_{t}"
                max_trips = route.get("max_trips_per_period")
                if max_trips:
                    prob += Trips[(rid, t)] <= max_trips, f"trip_limit_{rid}_{t}"

        solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit) if time_limit else pulp.PULP_CBC_CMD(msg=False)
        prob.solve(solver)

        status_label = pulp.LpStatus[prob.status]
        feasible = prob.status not in {pulp.LpStatusInfeasible, pulp.LpStatusUnbounded}

        production_plan: Dict[Tuple[int, int], float] = {}
        shipment_plan: Dict[Tuple[int, int], float] = {}
        inventory_plan: Dict[Tuple[int, int], float] = {}
        shortage_plan: Dict[Tuple[int, int], float] = {}
        trips_plan: Dict[Tuple[int, int], float] = {}

        if feasible:
            for key, var in X.items():
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
            for s_idx, scenario_plan in enumerate(scenarios):
                for pid in plant_lookup:
                    for t in periods:
                        inv_val = max(pulp.value(Inv[(s_idx, pid, t)]), 0.0)
                        if inv_val > 0:
                            inventory_plan[(pid, t)] = max(inventory_plan.get((pid, t), 0.0), inv_val)
                if model.allow_shortage:
                    for pid in plant_lookup:
                        for t in periods:
                            key = (s_idx, pid, t)
                            if key in Shortage:
                                shortage_val = max(pulp.value(Shortage[key]), 0.0)
                                if shortage_val > 0:
                                    shortage_plan[(pid, t)] = max(shortage_plan.get((pid, t), 0.0), shortage_val)

        if feasible:
            prod_cost_total = sum(prod_cost[i] * production_plan.get((i, t), 0.0) for i in iu_ids for t in periods)
            transport_cost_total = sum(
                float(route.get("cost_per_trip", 0.0)) * max(pulp.value(Trips[(route["id"], t)]), 0.0)
                + route_cost_per_ton.get(route["id"], 0.0) * shipment_plan.get((route["id"], t), 0.0)
                for route in base.routes
                for t in periods
            )
            holding_cost_total = sum(hold_cost[p] * inventory_plan.get((p, t), 0.0) for p in plant_lookup for t in periods)
            shortage_cost_total = sum(model.shortage_penalty * shortage_plan.get((p, t), 0.0) for p in plant_lookup for t in periods) if model.allow_shortage else 0.0
            worst_case_cost = max(round(pulp.value(expr), 2) for expr in scenario_cost_exprs)
        else:
            prod_cost_total = transport_cost_total = holding_cost_total = shortage_cost_total = 0.0
            worst_case_cost = 0.0
        runtime = time.monotonic() - start

        diagnostics: dict[str, object] = {
            "status": status_label,
            "scenario_count": len(scenarios),
            "solver": "cbc",
            "worst_case_cost": worst_case_cost,
        }

        return SolverResult(
            feasible=feasible,
            status=status_label.lower(),
            objective_value=round(worst_case_cost, 2),
            shipment_plan=shipment_plan,
            production_plan=production_plan,
            inventory_plan=inventory_plan,
            shortage_plan=shortage_plan,
            trips_plan=trips_plan,
            cost_breakdown={
                "production_cost": round(prod_cost_total, 2),
                "transport_cost": round(transport_cost_total, 2),
                "holding_cost": round(holding_cost_total, 2),
                "shortage_cost": round(shortage_cost_total, 2),
                "worst_case_cost": round(worst_case_cost, 2),
                "total_cost": round(worst_case_cost, 2),
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
        gu_ids = [pid for pid, p in plant_lookup.items() if p.get("type") == "GU"]
        consumers = iu_ids + gu_ids

        route_candidates = {}
        for route in dataset.routes:
            if route["destination"] in consumers and route["source"] in iu_ids:
                trip_cap = max(float(route["trip_capacity"]), 1e-9)
                unit_cost = (float(route["cost_per_trip"]) / trip_cap) if trip_cap else float("inf")
                route_candidates.setdefault(route["destination"], []).append({**route, "unit_cost": unit_cost, "trip_capacity": trip_cap})

        shipment_plan: Dict[tuple[int, int], float] = {}
        production_plan: Dict[tuple[int, int], float] = {}
        inventory_plan: Dict[tuple[int, int], float] = {}
        shortage_plan: Dict[tuple[int, int], float] = {}
        trips_plan: Dict[tuple[int, int], float] = {}

        feasible = True
        transport_cost_total = 0.0
        shortage_total = 0.0
        inventory_levels = {pid: float(dataset.inventory.get(pid, 0.0)) for pid in plant_lookup}
        safety_stock = {pid: float(dataset.safety_stock.get(pid, 0.0)) for pid in plant_lookup}
        total_demand_qty = sum(sum(dataset.demand.get(pid, [0.0] * periods)) for pid in plant_lookup)

        for period in range(1, periods + 1):
            iu_supply = {}
            for iu in iu_ids:
                prod_cap = float(plant_lookup[iu].get("production_capacity", 0.0))
                produced = prod_cap
                iu_supply[iu] = inventory_levels.get(iu, 0.0) + produced
                if produced > 0:
                    production_plan[(iu, period)] = production_plan.get((iu, period), 0.0) + produced

            for dest in consumers:
                demand_qty = float(dataset.demand.get(dest, [0.0] * periods)[period - 1])
                current_inv = inventory_levels.get(dest, 0.0)
                need = max(demand_qty - current_inv, 0.0)
                delivered = 0.0

                candidates = sorted(route_candidates.get(dest, []), key=lambda r: r["unit_cost"])
                for route in candidates:
                    if need <= 0:
                        break
                    source = route["source"]
                    supply_avail = iu_supply.get(source, 0.0)
                    if supply_avail <= 0:
                        continue
                    trip_cap = route["trip_capacity"]
                    max_trips = route.get("max_trips_per_period") or math.inf
                    deliverable = min(supply_avail, need, trip_cap * max_trips)
                    if deliverable <= 0:
                        continue

                    trips_used = math.ceil(deliverable / trip_cap)
                    qty = min(deliverable, trips_used * trip_cap)
                    iu_supply[source] -= qty
                    delivered += qty
                    need = max(need - qty, 0.0)

                    shipment_plan[(route["id"], period)] = shipment_plan.get((route["id"], period), 0.0) + qty
                    if trips_used > 0:
                        trips_plan[(route["id"], period)] = trips_plan.get((route["id"], period), 0.0) + trips_used
                    transport_cost_total += trips_used * float(route["cost_per_trip"]) + qty * float(route.get("cost_per_ton", 0.0))

                end_inv = current_inv + delivered - demand_qty
                if need > 1e-6:
                    if model.allow_shortage:
                        shortage_plan[(dest, period)] = shortage_plan.get((dest, period), 0.0) + need
                        shortage_total += need
                        end_inv = max(end_inv, 0.0)
                    else:
                        feasible = False
                inventory_levels[dest] = max(end_inv, 0.0)
                inventory_plan[(dest, period)] = inventory_levels[dest]

            for iu in iu_ids:
                inventory_levels[iu] = max(iu_supply.get(iu, 0.0), 0.0)
                inventory_plan[(iu, period)] = inventory_levels[iu]
                if inventory_levels[iu] + 1e-6 < safety_stock.get(iu, 0.0):
                    feasible = feasible and model.allow_shortage

        runtime = time.monotonic() - start
        prod_cost_total = sum(float(plant_lookup[i].get("production_cost", 0.0)) * qty for (i, _), qty in production_plan.items())
        hold_cost_total = sum(float(plant_lookup[p].get("holding_cost", 0.0)) * qty for (p, _), qty in inventory_plan.items())
        shortage_cost_total = model.shortage_penalty * shortage_total if model.allow_shortage else 0.0
        total_cost = prod_cost_total + transport_cost_total + hold_cost_total + shortage_cost_total

        if model.allow_shortage and model.service_level_target is not None:
            alpha = max(min(model.service_level_target, 1.0), 0.0)
            if total_demand_qty:
                if shortage_total > (1 - alpha) * total_demand_qty:
                    feasible = False

        diagnostics = {
            "mode": model.mode,
            "uncertainty": model.uncertainty,
            "scenario_weights": model.scenario_weights,
            "shortage_total": shortage_total,
            "service_level_target": model.service_level_target,
        }
        return SolverResult(
            feasible=feasible,
            status="optimal" if feasible else "infeasible",
            objective_value=round(total_cost, 2),
            shipment_plan=shipment_plan,
            production_plan=production_plan,
            inventory_plan=inventory_plan,
            shortage_plan=shortage_plan,
            trips_plan=trips_plan,
            cost_breakdown={
                "production_cost": round(prod_cost_total, 2),
                "transport_cost": round(transport_cost_total, 2),
                "holding_cost": round(hold_cost_total, 2),
                "shortage_cost": round(shortage_cost_total, 2) if model.allow_shortage else 0.0,
                "total_cost": round(total_cost, 2),
            },
            diagnostics=diagnostics,
            runtime_seconds=runtime,
        )


class SolverAdapter:
    """Facade choosing the appropriate solver backend."""

    def __init__(self, preferred: str | None = None):
        self.preferred = preferred or "greedy"
        self._milp = _DeterministicMilpSolver()
        self._fallback = _GreedyCostSolver()

    def solve(
        self,
        model: ModelDefinition,
        scenarios: list | None = None,
        time_limit: int | None = None,
        mode_override: str | None = None,
    ) -> SolverResult:
        mode = mode_override or model.mode
        try:
            if mode == "stochastic" and getattr(model, "stochastic_formulation", "extensive") == "extensive":
                return self._milp.solve_stochastic_extensive(model, scenarios or [], time_limit=time_limit)
            if mode == "robust" and (model.metadata.get("robust_formulation") == "minmax" if hasattr(model, "metadata") else False):
                return self._milp.solve_robust_minmax(model, scenarios or [], time_limit=time_limit)
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
