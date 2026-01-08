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
        }

        return ParsedSolution(
            total_cost=solver_result.objective_value,
            shipment_plan=solver_result.shipment_plan,
            trips_plan=solver_result.trips_plan,
            production_plan=solver_result.production_plan,
            inventory_plan=solver_result.inventory_plan,
            shortage_plan=solver_result.shortage_plan,
            cost_breakdown=solver_result.cost_breakdown,
            kpis=kpis,
            solver_status=solver_result.status,
            feasible=solver_result.feasible,
        )
