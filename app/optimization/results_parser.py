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
        total_capacity = sum(float(p.get("production_capacity", 0.0)) for p in dataset.plants if p.get("type") == "IU") * dataset.periods
        production_utilization = round((total_produced / total_capacity) * 100, 2) if total_capacity else 0.0

        transport_capacity = 0.0
        for route in dataset.routes:
            max_trips = route.get("max_trips_per_period") or 0
            transport_capacity += float(route.get("trip_capacity", 0.0)) * max_trips * dataset.periods
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
