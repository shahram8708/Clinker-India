"""Translate solver output into business-facing results."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from .data_mapper import CanonicalDataset
from .solver_adapter import SolverResult


@dataclass
class ParsedSolution:
    total_cost: float
    shipment_plan: dict
    trips_plan: dict
    production_plan: dict
    inventory_plan: dict
    fulfillment_plan: dict
    shortage_plan: dict
    cost_breakdown: dict
    kpis: dict
    diagnostics: dict
    runtime_seconds: float
    ui_views: dict
    solver_status: str
    feasible: bool


class ResultsParser:
    """Converts raw solver output to KPI-rich payloads."""

    def parse(self, solver_result: SolverResult, dataset: CanonicalDataset) -> ParsedSolution:
        route_lookup = {route["id"]: route for route in dataset.routes}
        route_dest = {rid: route.get("destination") for rid, route in route_lookup.items()}
        route_source = {rid: route.get("source") for rid, route in route_lookup.items()}

        def _route_cost(route_id: int, period: int, ship_qty: float | None = None, trips: float | None = None) -> float:
            freight = dataset.freight_costs.get(route_id, [0.0] * dataset.periods)[period - 1]
            handling = dataset.handling_costs.get(route_id, [0.0] * dataset.periods)[period - 1]
            multiplier = max(dataset.batch_multipliers.get(route_id, 1.0) or 1.0, 1e-6)
            eff_trips = trips if trips is not None else (ship_qty or 0.0) / multiplier
            return (freight + handling) * eff_trips

        demand_totals: Dict[int, float] = {}
        candidate_ids = set(route_source.values()) | set(route_dest.values()) | set(dataset.demand.keys())
        for pid in candidate_ids:
            if pid is None:
                continue
            demand_totals[pid] = sum(dataset.demand.get(pid, [0.0] * dataset.periods))
        total_demand = sum(demand_totals.values())

        fulfillment_totals: Dict[int, float] = {}
        for (pid, _), qty in solver_result.fulfillment_plan.items():
            fulfillment_totals[pid] = fulfillment_totals.get(pid, 0.0) + qty
        total_fulfilled = sum(fulfillment_totals.values())

        shortage_totals: Dict[int, float] = {}
        for (pid, _), qty in solver_result.shortage_plan.items():
            shortage_totals[pid] = shortage_totals.get(pid, 0.0) + qty
        unmet_demand_total = round(sum(shortage_totals.values()), 2)

        service_level = round((total_fulfilled / total_demand) * 100, 2) if total_demand > 0 else 100.0

        service_level_by_node: Dict[int, dict] = {}
        for plant in dataset.plants:
            pid = plant.get("id")
            demand_val = demand_totals.get(pid, 0.0)
            if demand_val <= 0:
                continue
            fulfilled = fulfillment_totals.get(pid, 0.0)
            shortage_val = shortage_totals.get(pid, 0.0)
            pct = round((fulfilled / demand_val) * 100, 2) if demand_val else 0.0
            service_level_by_node[pid] = {
                "code": plant.get("code", dataset.metadata.get("id_to_code", {}).get(pid, str(pid))),
                "demand": round(demand_val, 2),
                "fulfilled": round(fulfilled, 2),
                "shortage": round(shortage_val, 2),
                "service_level_pct": pct,
            }

        transport_cost_by_mode: Dict[str, float] = {}
        for (route_id, period), qty in solver_result.shipment_plan.items():
            trips = solver_result.trips_plan.get((route_id, period))
            cost = _route_cost(route_id, period, ship_qty=qty, trips=trips)
            mode = route_lookup.get(route_id, {}).get("mode", "unknown")
            transport_cost_by_mode[mode] = transport_cost_by_mode.get(mode, 0.0) + cost

        production_cost = solver_result.cost_breakdown.get("production_cost", 0.0)
        transport_cost = solver_result.cost_breakdown.get("transport_cost", sum(transport_cost_by_mode.values()))
        holding_cost = solver_result.cost_breakdown.get("holding_cost", 0.0)
        shortage_cost = solver_result.cost_breakdown.get("shortage_cost", 0.0)
        penalty_ful = solver_result.cost_breakdown.get("penalty_min_fulfillment", 0.0)
        penalty_min = solver_result.cost_breakdown.get("penalty_min_stock", 0.0)
        penalty_max = solver_result.cost_breakdown.get("penalty_max_stock", 0.0)
        total_cost = solver_result.cost_breakdown.get("total_cost", solver_result.objective_value)

        if not transport_cost_by_mode and transport_cost:
            transport_cost_by_mode["unspecified"] = transport_cost

        reliability_score = round(service_level, 2)
        risk_exposure = round(max(0.0, 100.0 - reliability_score), 2)
        inventory_slack = round(sum(solver_result.inventory_plan.values()) / total_demand, 3) if total_demand else 0.0

        total_shipped = sum(solver_result.shipment_plan.values())
        total_produced = sum(solver_result.production_plan.values())

        total_capacity = 0.0
        production_utilization_values = []
        production_utilization_by_iu: Dict[int, float] = {}
        for p in dataset.plants:
            if p.get("type") != "IU":
                continue
            pid = p["id"]
            caps = dataset.period_capacities.get(pid, [p.get("production_capacity", 0.0)] * dataset.periods)
            cap_sum = sum(caps)
            total_capacity += cap_sum
            produced_sum = sum(solver_result.production_plan.get((pid, t), 0.0) for t in range(1, dataset.periods + 1))
            util_pct = round((produced_sum / cap_sum) * 100, 2) if cap_sum else 0.0
            production_utilization_by_iu[pid] = util_pct
            if cap_sum > 0:
                production_utilization_values.append(util_pct)

        production_utilization = round((total_produced / total_capacity) * 100, 2) if total_capacity else 0.0
        production_utilization_avg = round(sum(production_utilization_values) / len(production_utilization_values), 2) if production_utilization_values else 0.0

        transport_capacity = 0.0
        route_usage: Dict[int, dict] = {}
        route_usage_values = []
        for route in dataset.routes:
            rid = route["id"]
            multiplier = dataset.batch_multipliers.get(rid, 1.0)
            max_trips = route.get("max_trips_per_period") or 0
            capacity = max_trips * multiplier * dataset.periods if max_trips else 0.0
            shipped_total = sum(solver_result.shipment_plan.get((rid, t), 0.0) for t in range(1, dataset.periods + 1))
            trips_used = sum(solver_result.trips_plan.get((rid, t), 0.0) for t in range(1, dataset.periods + 1))
            usage_pct = round((shipped_total / capacity) * 100, 2) if capacity else 0.0
            if capacity:
                transport_capacity += capacity
                route_usage_values.append(usage_pct)
            route_usage[rid] = {
                "mode": route.get("mode"),
                "shipped": round(shipped_total, 2),
                "trips": round(trips_used, 2),
                "capacity": round(capacity, 2),
                "usage_pct": usage_pct,
            }

        transport_utilization = round((total_shipped / transport_capacity) * 100, 2) if transport_capacity else 0.0
        route_usage_avg = round(sum(route_usage_values) / len(route_usage_values), 2) if route_usage_values else 0.0

        safety_violations: list[int] = []
        safety_gaps: list[dict] = []
        max_violations: list[int] = []
        max_gaps: list[dict] = []
        for pid, safety_stock in dataset.safety_stock.items():
            inv_levels = [qty for (plant_id, _), qty in solver_result.inventory_plan.items() if plant_id == pid]
            min_inv = min(inv_levels) if inv_levels else float(dataset.inventory.get(pid, 0.0))
            if min_inv + 1e-6 < safety_stock:
                safety_violations.append(pid)
                safety_gaps.append(
                    {
                        "plant_id": pid,
                        "safety_stock": float(safety_stock),
                        "min_inventory": round(min_inv, 2),
                        "gap": round(float(safety_stock) - float(min_inv), 2),
                    }
                )

        for (pid, period), max_stock in dataset.max_closing_stocks.items():
            inv_level = solver_result.inventory_plan.get((pid, period))
            if inv_level is None:
                continue
            if max_stock < float("inf") and inv_level - 1e-6 > max_stock:
                max_violations.append(pid)
                max_gaps.append(
                    {
                        "plant_id": pid,
                        "period": period,
                        "max_stock": round(float(max_stock), 2),
                        "closing": round(float(inv_level), 2),
                        "gap": round(float(inv_level - max_stock), 2),
                    }
                )

        trips_by_route: Dict[int, float] = {}
        for (route_id, _), trips in solver_result.trips_plan.items():
            trips_by_route[route_id] = trips_by_route.get(route_id, 0.0) + trips

        shipments_by_destination: Dict[int, float] = {}
        for (route_id, _), qty in solver_result.shipment_plan.items():
            dest = route_dest.get(route_id)
            if dest is not None:
                shipments_by_destination[dest] = shipments_by_destination.get(dest, 0.0) + qty

        batch_multiplier_utilization: Dict[int, float] = {}
        for route_id, trips_total in trips_by_route.items():
            multiplier = dataset.batch_multipliers.get(route_id, 1.0)
            max_possible_trips = 0
            for route in dataset.routes:
                if route["id"] == route_id:
                    max_possible_trips = (route.get("max_trips_per_period") or 0) * dataset.periods
                    break
            if max_possible_trips > 0:
                batch_multiplier_utilization[route_id] = round((trips_total / max_possible_trips) * 100, 2)

        iugu_compliance: Dict[str, dict] = {}
        for constraint_key, (min_flow, max_flow) in dataset.iugu_constraints.items():
            parts = str(constraint_key).split("_")
            if len(parts) == 2:
                try:
                    source_id = int(parts[0].replace("IU", "").replace("GU", ""))
                    dest_id = int(parts[1].replace("IU", "").replace("GU", ""))
                    total_flow = 0.0
                    for (route_id, _period), qty in solver_result.shipment_plan.items():
                        if route_source.get(route_id) == source_id and route_dest.get(route_id) == dest_id:
                            total_flow += qty
                    satisfied = (total_flow >= min_flow - 1e-6) and (total_flow <= max_flow + 1e-6)
                    iugu_compliance[constraint_key] = {
                        "actual_flow": round(total_flow, 2),
                        "min_required": round(min_flow, 2),
                        "max_allowed": round(max_flow, 2) if max_flow < float("inf") else "unlimited",
                        "satisfied": satisfied,
                    }
                except (ValueError, KeyError):
                    pass

        flow_constraint_diag = []
        for fc in dataset.flow_constraints:
            period = int(fc.get("period", 1))
            src = fc.get("source")
            dst = fc.get("destination")
            mode = fc.get("mode")
            bound_type = fc.get("type")
            target_val = float(fc.get("value", 0.0))
            relevant_routes = [
                r for r in dataset.routes
                if r.get("source") == src
                and (dst is None or r.get("destination") == dst)
                and (mode is None or r.get("mode") == mode)
            ]
            actual_flow = sum(solver_result.shipment_plan.get((r["id"], period), 0.0) for r in relevant_routes)
            satisfied = True
            if bound_type == "E":
                satisfied = abs(actual_flow - target_val) <= 1e-6
            elif bound_type == "L":
                satisfied = actual_flow + 1e-6 >= target_val
            elif bound_type == "G":
                satisfied = actual_flow - 1e-6 <= target_val
            flow_constraint_diag.append(
                {
                    "source": src,
                    "destination": dst,
                    "mode": mode,
                    "period": period,
                    "lhs": round(actual_flow, 2),
                    "rhs": round(target_val, 2),
                    "type": bound_type,
                    "satisfied": satisfied,
                }
            )

        constraint_all_satisfied = all(item.get("satisfied", True) for item in flow_constraint_diag)

        diagnostics_messages = []
        if not solver_result.feasible:
            diagnostics_messages.append(f"Solver status: {solver_result.status}")
        supply_capacity = total_capacity + sum(dataset.inventory.values())
        if total_demand > supply_capacity + 1e-6:
            diagnostics_messages.append(
                f"Total demand {total_demand:.2f} exceeds supply capacity {supply_capacity:.2f}. Shortage captured in results."
            )
        for plant in dataset.plants:
            pid = plant.get("id")
            if demand_totals.get(pid, 0.0) > 0:
                inbound_routes = [r for r in dataset.routes if r.get("destination") == pid]
                if not inbound_routes and plant.get("type") == "GU":
                    diagnostics_messages.append(f"GU {plant.get('code', pid)} has demand but no inbound routes.")
        if safety_gaps:
            diagnostics_messages.append(f"Safety stock violations at {len(safety_gaps)} nodes.")
        if max_gaps:
            diagnostics_messages.append(f"Max inventory exceeded at {len(max_gaps)} nodes/periods.")
        if unmet_demand_total > 0:
            diagnostics_messages.append(f"Unmet demand: {unmet_demand_total:.2f} captured as shortage_cost.")

        cost_summary = {
            "production": production_cost,
            "transport": transport_cost,
            "transport_by_mode": {k: round(v, 2) for k, v in transport_cost_by_mode.items()},
            "holding": holding_cost,
            "shortage": shortage_cost,
            "penalty_min_fulfillment": penalty_ful,
            "penalty_min_stock": penalty_min,
            "penalty_max_stock": penalty_max,
            "total": total_cost,
        }

        kpis = {
            "service_level_pct": service_level,
            "service_level_by_node": service_level_by_node,
            "transport_cost": transport_cost,
            "transport_cost_by_mode": {k: round(v, 2) for k, v in transport_cost_by_mode.items()},
            "production_cost": production_cost,
            "holding_cost": holding_cost,
            "shortage_cost": shortage_cost,
            "penalty_min_fulfillment": penalty_ful,
            "penalty_min_stock": penalty_min,
            "penalty_max_stock": penalty_max,
            "total_cost": total_cost,
            "reliability_score": reliability_score,
            "risk_exposure": risk_exposure,
            "inventory_slack_ratio": inventory_slack,
            "feasible": solver_result.feasible,
            "production_utilization_pct": production_utilization,
            "production_utilization_avg_pct": production_utilization_avg,
            "production_utilization_by_iu": production_utilization_by_iu,
            "transport_utilization_pct": transport_utilization,
            "route_utilization_avg_pct": route_usage_avg,
            "route_usage": route_usage,
            "total_shipped": total_shipped,
            "total_produced": total_produced,
            "safety_stock_violations": safety_violations,
            "safety_stock_gaps": safety_gaps,
            "safety_stock_ok": len(safety_gaps) == 0,
            "max_stock_violations": max_violations,
            "max_stock_gaps": max_gaps,
            "max_stock_ok": len(max_gaps) == 0,
            "trips_by_route": trips_by_route,
            "shipments_by_destination": shipments_by_destination,
            "cost_summary": cost_summary,
            "batch_multiplier_utilization": batch_multiplier_utilization,
            "iugu_compliance": iugu_compliance,
            "flow_constraint_diagnostics": flow_constraint_diag,
            "constraints_ok": constraint_all_satisfied,
            "iugu_all_satisfied": all(c.get("satisfied", False) for c in iugu_compliance.values()) if iugu_compliance else True,
            "unmet_demand_total": unmet_demand_total,
            "diagnostics_messages": diagnostics_messages,
            "id_to_code": dataset.metadata.get("id_to_code", {}),
        }

        solver_result.diagnostics = {**(solver_result.diagnostics or {}), "messages": diagnostics_messages}

        ui_views = self._build_ui_views(dataset, solver_result, cost_summary, kpis)

        return ParsedSolution(
            total_cost=solver_result.objective_value,
            shipment_plan=solver_result.shipment_plan,
            trips_plan=getattr(solver_result, "trips_plan", {}),
            production_plan=solver_result.production_plan,
            inventory_plan=solver_result.inventory_plan,
            fulfillment_plan=solver_result.fulfillment_plan,
            shortage_plan=solver_result.shortage_plan,
            cost_breakdown=solver_result.cost_breakdown,
            kpis=kpis,
            diagnostics=solver_result.diagnostics,
            runtime_seconds=solver_result.runtime_seconds,
            ui_views=ui_views,
            solver_status=solver_result.status,
            feasible=solver_result.feasible,
        )

    def _build_ui_views(self, dataset: CanonicalDataset, solver_result: SolverResult, cost_summary: dict, kpis: dict) -> dict:
        """Produce UI-friendly tables for dispatch, production, inventory, and summary."""
        id_to_code = dataset.metadata.get("id_to_code", {}) if dataset.metadata else {}
        route_index = {
            route["id"]: {
                "from_id": route.get("source"),
                "to_id": route.get("destination"),
                "from_code": id_to_code.get(route.get("source"), str(route.get("source"))),
                "to_code": id_to_code.get(route.get("destination"), str(route.get("destination"))),
                "mode": route.get("mode"),
                "multiplier": dataset.batch_multipliers.get(route["id"], 1.0),
                "trip_capacity": route.get("trip_capacity", dataset.batch_multipliers.get(route["id"], 1.0)),
                "min_batch": route.get("min_batch_quantity", 0.0),
            }
            for route in dataset.routes
        }

        dispatch_rows = []
        for (route_id, period), qty in solver_result.shipment_plan.items():
            lookup = route_index.get(route_id, {})
            trips = solver_result.trips_plan.get((route_id, period), 0.0)
            freight = dataset.freight_costs.get(route_id, [0.0] * dataset.periods)[period - 1]
            handling = dataset.handling_costs.get(route_id, [0.0] * dataset.periods)[period - 1]
            unit_trip_cost = freight + handling
            dispatch_rows.append(
                {
                    "period": period,
                    "from": lookup.get("from_code"),
                    "to": lookup.get("to_code"),
                    "mode": lookup.get("mode"),
                    "trips": float(trips),
                    "qty": float(qty),
                    "multiplier": float(lookup.get("multiplier", 1.0)),
                    "trip_capacity": float(lookup.get("trip_capacity", lookup.get("multiplier", 0.0))),
                    "min_batch": float(lookup.get("min_batch", 0.0)),
                    "unit_trip_cost": float(unit_trip_cost),
                    "route_cost": float(unit_trip_cost * trips),
                    "route_id": route_id,
                }
            )
        dispatch_rows.sort(key=lambda r: (r["period"], r.get("mode", ""), r.get("from", "")))

        production_rows = []
        opening_by_period: Dict[Tuple[int, int], float] = {}
        closing_by_period: Dict[Tuple[int, int], float] = {}
        for plant in dataset.plants:
            if plant.get("type") != "IU":
                continue
            pid = plant.get("id")
            caps = dataset.period_capacities.get(pid, [plant.get("production_capacity", 0.0)] * dataset.periods)
            costs = dataset.period_specific_costs.get(pid, [plant.get("production_cost", 0.0)] * dataset.periods)
            for period in range(1, dataset.periods + 1):
                produced = solver_result.production_plan.get((pid, period), 0.0)
                cap = caps[period - 1] if len(caps) >= period else caps[-1]
                cost = costs[period - 1] if len(costs) >= period else costs[-1]
                utilization = round((produced / cap) * 100, 2) if cap else 0.0
                internal_demand = float(dataset.demand.get(pid, [0.0] * dataset.periods)[period - 1])
                opening_val = opening_by_period.get((pid, period), float(dataset.inventory.get(pid, 0.0)))
                closing_val = closing_by_period.get((pid, period), opening_val)
                net_exported = produced + opening_val - internal_demand - closing_val
                production_rows.append(
                    {
                        "code": plant.get("code", id_to_code.get(pid, str(pid))),
                        "period": period,
                        "produced": float(produced),
                        "capacity": float(cap),
                        "utilization_pct": utilization,
                        "cost_per_ton": float(cost),
                        "cost": float(cost * produced),
                        "internal_demand": internal_demand,
                        "net_exported": float(net_exported),
                    }
                )

        inbound = {}
        outbound = {}
        for (route_id, period), qty in solver_result.shipment_plan.items():
            lookup = route_index.get(route_id)
            if not lookup:
                continue
            src = lookup.get("from_id")
            dst = lookup.get("to_id")
            outbound[(src, period)] = outbound.get((src, period), 0.0) + float(qty)
            inbound[(dst, period)] = inbound.get((dst, period), 0.0) + float(qty)

        inventory_rows = []
        for plant in dataset.plants:
            pid = plant.get("id")
            code = plant.get("code", id_to_code.get(pid, str(pid)))
            opening = float(dataset.inventory.get(pid, 0.0))
            for period in range(1, dataset.periods + 1):
                inbound_qty = inbound.get((pid, period), 0.0)
                outbound_ship = outbound.get((pid, period), 0.0)
                demand_qty = float(dataset.demand.get(pid, [0.0] * dataset.periods)[period - 1])
                closing = solver_result.inventory_plan.get((pid, period))
                if closing is None:
                    closing = opening + inbound_qty - outbound_ship - demand_qty
                min_close = float(dataset.min_closing_stocks.get((pid, period), dataset.safety_stock.get(pid, 0.0)))
                max_close = dataset.max_closing_stocks.get((pid, period), float("inf"))
                max_close_val = float(max_close) if max_close != float("inf") else None
                outbound_total = outbound_ship + demand_qty
                opening_by_period[(pid, period)] = float(opening)
                closing_by_period[(pid, period)] = float(closing)
                inventory_rows.append(
                    {
                        "code": code,
                        "period": period,
                        "opening": float(opening),
                        "inbound": float(inbound_qty),
                        "outbound_ship": float(outbound_ship),
                        "demand": float(demand_qty),
                        "outbound_total": float(outbound_total),
                        "closing": float(closing),
                        "min_close": float(min_close),
                        "max_close": max_close_val,
                        "below_min": closing + 1e-6 < min_close,
                        "above_max": True if (max_close_val is not None and closing - 1e-6 > max_close_val) else False,
                        "meets_safety_stock": not (closing + 1e-6 < min_close),
                        "within_capacity": False if (max_close_val is not None and closing - 1e-6 > max_close_val) else True,
                    }
                )
                opening = closing

        summary = {
            "status": solver_result.status,
            "objective": float(solver_result.objective_value),
            "runtime_seconds": float(solver_result.runtime_seconds or 0.0),
            "mip_gap": solver_result.diagnostics.get("mip_gap"),
            "feasible": bool(solver_result.feasible),
            "service_level_pct": kpis.get("service_level_pct"),
            "demand_fulfillment_pct": kpis.get("service_level_pct"),
            "unmet_demand_total": kpis.get("unmet_demand_total", 0.0),
            "route_utilization_avg_pct": kpis.get("route_utilization_avg_pct", 0.0),
            "production_utilization_pct": kpis.get("production_utilization_pct", 0.0),
            "costs": cost_summary,
        }

        return {
            "route_index": route_index,
            "dispatch": dispatch_rows,
            "production": production_rows,
            "inventory": inventory_rows,
            "summary": summary,
        }
