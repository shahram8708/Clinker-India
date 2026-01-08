"""Translate solver output into business-facing results."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from .data_mapper import CanonicalDataset
from .solver_adapter import SolverResult


@dataclass
class ParsedSolution:
    total_cost: float
    shipment_plan: dict
    trips_plan: dict
    production_plan: dict
    inventory_plan: dict
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
        route_dest = {route["id"]: route["destination"] for route in dataset.routes}
        route_source = {route["id"]: route["source"] for route in dataset.routes}
        
        delivered_by_dest: Dict[int, float] = {}
        for (route_id, _period), qty in solver_result.shipment_plan.items():
            dest = route_dest.get(route_id)
            if dest is not None:
                delivered_by_dest[dest] = delivered_by_dest.get(dest, 0.0) + qty

        total_demand = sum(sum(periods) for periods in dataset.demand.values()) or 1.0
        total_shortage = sum(solver_result.shortage_plan.values()) if solver_result.shortage_plan else 0.0
        total_delivered = max(total_demand - total_shortage, 0.0) or sum(delivered_by_dest.values())
        service_level = round((total_delivered / total_demand) * 100, 2)

        transport_cost = solver_result.cost_breakdown.get(
            "transport_cost",
            solver_result.cost_breakdown.get("transport", solver_result.objective_value),
        )
        production_cost = solver_result.cost_breakdown.get("production_cost", 0.0)
        holding_cost = solver_result.cost_breakdown.get("holding_cost", 0.0)
        shortage_cost = solver_result.cost_breakdown.get("shortage_cost", 0.0)
        total_cost = solver_result.cost_breakdown.get("total_cost", solver_result.objective_value)

        reliability_score = round(service_level, 2)
        risk_exposure = round(max(0.0, 100.0 - reliability_score), 2)
        inventory_slack = round(sum(solver_result.inventory_plan.values()) / total_demand, 3) if total_demand else 0.0

        total_shipped = sum(solver_result.shipment_plan.values())
        total_trips = sum(solver_result.trips_plan.values()) if solver_result.trips_plan else 0.0
        total_produced = sum(solver_result.production_plan.values())
        
        # Calculate production utilization using period-specific capacities if available
        total_capacity = 0.0
        for p in dataset.plants:
            if p.get("type") == "IU":
                pid = p["id"]
                if pid in dataset.period_capacities and len(dataset.period_capacities[pid]) == dataset.periods:
                    total_capacity += sum(dataset.period_capacities[pid])
                else:
                    total_capacity += float(p.get("production_capacity", 0.0)) * dataset.periods
        
        production_utilization = round((total_produced / total_capacity) * 100, 2) if total_capacity else 0.0

        # Calculate transport capacity considering batch multipliers
        transport_capacity = 0.0
        for route in dataset.routes:
            route_id = route["id"]
            max_trips = route.get("max_trips_per_period") or 0
            multiplier = dataset.batch_multipliers.get(route_id, 1.0)
            # Max flow per route = max_trips × multiplier × periods
            transport_capacity += max_trips * multiplier * dataset.periods
        transport_utilization = round((total_shipped / transport_capacity) * 100, 2) if transport_capacity else 0.0

        safety_violations: list[int] = []
        safety_gaps: list[dict] = []
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

        trips_by_route: Dict[int, float] = {}
        for (route_id, _), trips in solver_result.trips_plan.items():
            trips_by_route[route_id] = trips_by_route.get(route_id, 0.0) + trips

        shipments_by_destination: Dict[int, float] = {}
        for (route_id, _), qty in solver_result.shipment_plan.items():
            dest = route_dest.get(route_id)
            if dest is not None:
                shipments_by_destination[dest] = shipments_by_destination.get(dest, 0.0) + qty
        
        # Enhanced KPIs: batch multiplier utilization, IUGU constraint compliance
        batch_multiplier_utilization: Dict[int, float] = {}
        for route_id, trips_total in trips_by_route.items():
            multiplier = dataset.batch_multipliers.get(route_id, 1.0)
            # Actual flow vs theoretical max with multiplier
            max_possible_trips = 0
            for route in dataset.routes:
                if route["id"] == route_id:
                    max_possible_trips = (route.get("max_trips_per_period") or 0) * dataset.periods
                    break
            if max_possible_trips > 0:
                batch_multiplier_utilization[route_id] = round((trips_total / max_possible_trips) * 100, 2)
        
        # Check IUGU constraint satisfaction
        iugu_compliance: Dict[str, dict] = {}
        for constraint_key, (min_flow, max_flow) in dataset.iugu_constraints.items():
            parts = str(constraint_key).split("_")
            if len(parts) == 2:
                try:
                    source_id = int(parts[0].replace("IU", "").replace("GU", ""))
                    dest_id = int(parts[1].replace("IU", "").replace("GU", ""))
                    
                    # Sum flow from source to dest across all relevant routes and periods
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

        cost_summary = {
            "production": production_cost,
            "transport": transport_cost,
            "holding": holding_cost,
            "shortage": shortage_cost,
            "total": total_cost,
        }

        kpis = {
            "service_level_pct": service_level,
            "transport_cost": transport_cost,
            "production_cost": production_cost,
            "holding_cost": holding_cost,
            "shortage_cost": shortage_cost,
            "total_cost": total_cost,
            "shortage_units": total_shortage,
            "reliability_score": reliability_score,
            "risk_exposure": risk_exposure,
            "inventory_slack_ratio": inventory_slack,
            "feasible": solver_result.feasible,
            "production_utilization_pct": production_utilization,
            "transport_utilization_pct": transport_utilization,
            "total_shipped": total_shipped,
            "total_trips": total_trips,
            "total_produced": total_produced,
            "safety_stock_violations": safety_violations,
            "safety_stock_gaps": safety_gaps,
            "safety_stock_ok": len(safety_gaps) == 0,
            "trips_by_route": trips_by_route,
            "shipments_by_destination": shipments_by_destination,
            "cost_summary": cost_summary,
            # Enhanced KPIs
            "batch_multiplier_utilization": batch_multiplier_utilization,
            "iugu_compliance": iugu_compliance,
            "iugu_all_satisfied": all(c.get("satisfied", False) for c in iugu_compliance.values()) if iugu_compliance else True,
            # Keep routing metadata for UI rendering
            "id_to_code": dataset.metadata.get("id_to_code", {}),
        }

        ui_views = self._build_ui_views(dataset, solver_result, cost_summary, kpis)

        return ParsedSolution(
            total_cost=solver_result.objective_value,
            shipment_plan=solver_result.shipment_plan,
            trips_plan=solver_result.trips_plan,
            production_plan=solver_result.production_plan,
            inventory_plan=solver_result.inventory_plan,
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
                    "unit_trip_cost": float(unit_trip_cost),
                    "route_cost": float(unit_trip_cost * trips),
                    "route_id": route_id,
                }
            )
        dispatch_rows.sort(key=lambda r: (r["period"], r.get("mode", ""), r.get("from", "")))

        production_rows = []
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
                production_rows.append(
                    {
                        "code": plant.get("code", id_to_code.get(pid, str(pid))),
                        "period": period,
                        "produced": float(produced),
                        "capacity": float(cap),
                        "utilization_pct": utilization,
                        "cost_per_ton": float(cost),
                        "cost": float(cost * produced),
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
            "costs": cost_summary,
        }

        return {
            "route_index": route_index,
            "dispatch": dispatch_rows,
            "production": production_rows,
            "inventory": inventory_rows,
            "summary": summary,
        }
