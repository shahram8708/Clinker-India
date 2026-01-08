"""Robust worst-case optimization solver with structured result storage."""
from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .data_mapper import CanonicalDataset
from .model_builder import ModelDefinition
from .results_parser import ParsedSolution, ResultsParser
from .scenario_manager import ScenarioPlan
from .solver_adapter import SolverAdapter, SolverResult


@dataclass
class FeasibilityCheck:
    name: str
    passed: bool
    severity: str
    details: dict

    def as_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "severity": self.severity, "details": self.details}


@dataclass
class RobustOutcome:
    label: str
    parsed: ParsedSolution
    multiplier: float
    probability: float
    tier_used: str
    diagnostics: dict
    dataset: CanonicalDataset


@dataclass
class ScenarioResultRecord:
    scenario_id: Any
    organization_id: Any
    mode: str
    status: str
    runtime: float
    mip_gap: Optional[float]
    feasibility_mode_used: str
    notes: str | None = None

    def as_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "organization_id": self.organization_id,
            "mode": self.mode,
            "status": self.status,
            "runtime": self.runtime,
            "mip_gap": self.mip_gap,
            "feasibility_mode_used": self.feasibility_mode_used,
            "notes": self.notes,
        }


@dataclass
class CostResultRecord:
    scenario_id: Any
    total_cost: float
    production_cost: float
    transport_cost: float
    holding_cost: float
    penalty_cost: float
    scenario_expected_cost: Optional[float]
    worst_case_cost: Optional[float]

    def as_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "total_cost": self.total_cost,
            "production_cost": self.production_cost,
            "transport_cost": self.transport_cost,
            "holding_cost": self.holding_cost,
            "penalty_cost": self.penalty_cost,
            "scenario_expected_cost": self.scenario_expected_cost,
            "worst_case_cost": self.worst_case_cost,
        }


@dataclass
class ShipmentPlanRecord:
    scenario_id: Any
    period: int
    source_plant_id: Any
    destination_plant_id: Any
    mode: Optional[str]
    trips: Optional[float]
    quantity: float
    sbq_used: Optional[float]
    route_cost: Optional[float]

    def as_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "period": self.period,
            "source_plant_id": self.source_plant_id,
            "destination_plant_id": self.destination_plant_id,
            "mode": self.mode,
            "trips": self.trips,
            "quantity": self.quantity,
            "sbq_used": self.sbq_used,
            "route_cost": self.route_cost,
        }


@dataclass
class InventoryResultRecord:
    scenario_id: Any
    period: int
    plant_id: Any
    inventory_level: float
    safety_status: str

    def as_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "period": self.period,
            "plant_id": self.plant_id,
            "inventory_level": self.inventory_level,
            "safety_status": self.safety_status,
        }


@dataclass
class ProductionResultRecord:
    scenario_id: Any
    plant_id: Any
    period: int
    production_quantity: float

    def as_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "plant_id": self.plant_id,
            "period": self.period,
            "production_quantity": self.production_quantity,
        }


class RobustSolver:
    """Plans for worst-case performance across stressed scenarios."""

    def __init__(self, solver: SolverAdapter, parser: ResultsParser):
        self.solver = solver
        self.parser = parser

    def solve(
        self,
        base_model: ModelDefinition,
        scenarios: List[ScenarioPlan],
        runtime_limit: int | None = None,
    ) -> ParsedSolution:
        start_time = time.monotonic()
        if not scenarios:
            scenarios = [ScenarioPlan(dataset=base_model.dataset, probability=1.0, label="R1", stress="worst_case", multiplier=1.0)]

        # Min-max robust: shared first-stage decisions feasible for all scenarios, minimize worst-case cost.
        if base_model.metadata.get("robust_formulation") == "minmax":
            result = self.solver.solve(base_model, scenarios=scenarios, time_limit=runtime_limit, mode_override="robust")
            parsed = self.parser.parse(result, base_model.dataset)
            parsed.kpis["scenario_count"] = len(scenarios)
            parsed.kpis["worst_case_cost"] = result.cost_breakdown.get("worst_case_cost", result.objective_value)
            parsed.kpis["formulation"] = "minmax"
            return parsed

        base_precheck = self._pre_solve_screen(base_model.dataset, base_model.allow_shortage)
        outcomes: List[RobustOutcome] = []

        for scenario in scenarios:
            scenario_diag: dict[str, object] = {"precheck": self._pre_solve_screen(scenario.dataset, base_model.allow_shortage)}
            outcome = self._solve_with_fallbacks(base_model, scenario, runtime_limit, scenario_diag)
            outcomes.append(outcome)

        feasible_outcomes = [o for o in outcomes if o.parsed.feasible]
        target_pool = feasible_outcomes or outcomes
        worst = max(target_pool, key=lambda o: o.parsed.total_cost)
        worst_cost = worst.parsed.total_cost
        feasible = len(feasible_outcomes) == len(outcomes)
        status = "optimal" if feasible else "infeasible"

        aggregate_result = SolverResult(
            feasible=feasible,
            status=status,
            objective_value=round(worst_cost, 2),
            shipment_plan=worst.parsed.shipment_plan,
            production_plan=worst.parsed.production_plan,
            inventory_plan=worst.parsed.inventory_plan,
            shortage_plan=worst.parsed.shortage_plan,
            cost_breakdown={
                "worst_case_cost": round(worst_cost, 2),
                "transport_cost": worst.parsed.cost_breakdown.get("transport_cost", 0.0),
                "production_cost": worst.parsed.cost_breakdown.get("production_cost", 0.0),
                "holding_cost": worst.parsed.cost_breakdown.get("holding_cost", 0.0),
                "shortage_cost": worst.parsed.cost_breakdown.get("shortage_cost", 0.0),
                "total_cost": round(worst_cost, 2),
            },
            diagnostics={
                "scenario_costs": [
                    {
                        "label": o.label,
                        "cost": o.parsed.total_cost,
                        "service_level": o.parsed.kpis.get("service_level_pct"),
                        "multiplier": o.multiplier,
                        "probability": o.probability,
                        "tier_used": o.tier_used,
                        "status": o.parsed.solver_status,
                        "feasible": o.parsed.feasible,
                        "validation": o.diagnostics.get("validation"),
                        "precheck": o.diagnostics.get("precheck"),
                    }
                    for o in outcomes
                ],
                "worst_case_label": worst.label,
                "fallback_applied": any(o.tier_used != "baseline" for o in outcomes),
                "precheck": base_precheck,
            },
            runtime_seconds=round(time.monotonic() - start_time, 4),
        )
        worst_dataset = worst.dataset
        parsed = self.parser.parse(aggregate_result, worst_dataset)
        parsed.kpis["worst_case_cost"] = round(worst_cost, 2)
        parsed.kpis["scenario_count"] = len(outcomes)
        parsed.kpis["worst_case_label"] = worst.label
        parsed.kpis["fallback_applied"] = any(o.tier_used != "baseline" for o in outcomes)
        parsed.kpis["validation"] = worst.diagnostics.get("validation", {})
        parsed.kpis["validation_issues"] = parsed.kpis["validation"].get("issues", parsed.kpis["validation"])

        structured_payload = self._build_storage_payload(worst, outcomes, aggregate_result, base_precheck)
        kpi_block = self._extract_kpis(worst.parsed, worst.dataset, structured_payload)
        narrative = self._build_narrative(structured_payload, kpi_block)
        comparison = self._build_comparison(outcomes, worst)
        digital_twin = self._build_digital_twin_view(worst, outcomes, structured_payload, kpi_block)
        comparative_intel = self._build_comparative_intelligence(outcomes, worst, structured_payload, kpi_block)

        structured_payload["digital_twin"] = digital_twin
        structured_payload["narrative"] = narrative
        structured_payload["analytics"] = kpi_block
        structured_payload["comparative_intelligence"] = comparative_intel

        report_layer = self._build_reporting_layer(worst, outcomes, structured_payload, kpi_block, narrative, comparison)
        structured_payload["reports"] = report_layer["reports"]
        structured_payload["export_manifests"] = report_layer["exports"]
        structured_payload["reporting_center"] = report_layer["reporting_center"]

        parsed.kpis["storage_payload"] = structured_payload
        parsed.kpis["analytics"] = kpi_block
        parsed.kpis["narrative_insights"] = narrative
        parsed.kpis["comparison"] = comparison
        parsed.kpis["digital_twin"] = digital_twin
        parsed.kpis["comparative_intelligence"] = comparative_intel
        parsed.kpis["reports"] = report_layer["reports"]
        parsed.kpis["export_manifests"] = report_layer["exports"]
        parsed.kpis["reporting_center"] = report_layer["reporting_center"]
        return parsed

    def _pre_solve_screen(self, dataset: CanonicalDataset, allow_shortage: bool) -> dict:
        checks = [
            self._check_supply_vs_demand(dataset, allow_shortage),
            self._check_connectivity(dataset),
            self._check_inventory_logic(dataset),
            self._check_integer_traps(dataset),
        ]
        blocking = [c.name for c in checks if (not c.passed and c.severity == "error")]
        warnings = [c.name for c in checks if (not c.passed and c.severity != "error")]
        return {
            "blocking": blocking,
            "warnings": warnings,
            "checks": [c.as_dict() for c in checks],
            "ok": not blocking,
        }

    def _check_supply_vs_demand(self, dataset: CanonicalDataset, allow_shortage: bool) -> FeasibilityCheck:
        plant_lookup = {p["id"]: p for p in dataset.plants}
        iu_ids = [pid for pid, plant in plant_lookup.items() if plant.get("type") == "IU"]
        total_supply = sum(float(plant_lookup[i].get("production_capacity", 0.0)) for i in iu_ids) * dataset.periods
        initial_inventory = sum(float(qty) for qty in dataset.inventory.values())
        total_demand = sum(sum(periods) for periods in dataset.demand.values())
        ok = (total_supply + initial_inventory) >= total_demand
        severity = "warning" if allow_shortage and not ok else "error"
        return FeasibilityCheck(
            name="supply_vs_demand",
            passed=ok or allow_shortage,
            severity=severity,
            details={"total_supply_available": total_supply + initial_inventory, "total_demand": total_demand},
        )

    def _check_connectivity(self, dataset: CanonicalDataset) -> FeasibilityCheck:
        plant_lookup = {p["id"]: p for p in dataset.plants}
        gu_ids = [pid for pid, plant in plant_lookup.items() if plant.get("type") == "GU"]
        inbound = {pid: [] for pid in gu_ids}
        issues: list[str] = []
        for route in dataset.routes:
            dest = route.get("destination")
            if dest in inbound:
                inbound[dest].append(route)
            sbq = float(route.get("min_batch_quantity", 0.0))
            cap = float(route.get("trip_capacity", 0.0))
            if sbq > cap:
                issues.append(f"route_{route['id']}_sbq_gt_capacity")

        for gu_id, demand in dataset.demand.items():
            if gu_id not in inbound:
                continue
            has_demand = any(val > 0 for val in demand)
            if has_demand and not inbound.get(gu_id):
                issues.append(f"no_inbound_route_for_gu_{gu_id}")

        return FeasibilityCheck(name="network_connectivity", passed=not issues, severity="error", details={"issues": issues})

    def _check_inventory_logic(self, dataset: CanonicalDataset) -> FeasibilityCheck:
        plant_lookup = {p["id"]: p for p in dataset.plants}
        violations: list[str] = []
        for pid, plant in plant_lookup.items():
            max_cap = float(plant.get("max_inventory_capacity", 0.0) or 0.0)
            safety = float(dataset.safety_stock.get(pid, 0.0))
            if max_cap > 0 and safety > max_cap:
                violations.append(f"safety_exceeds_capacity_{pid}")
            init_inv = float(dataset.inventory.get(pid, 0.0))
            if max_cap > 0 and init_inv > max_cap:
                violations.append(f"initial_inventory_exceeds_capacity_{pid}")
        return FeasibilityCheck(name="inventory_logic", passed=not violations, severity="error", details={"issues": violations})

    def _check_integer_traps(self, dataset: CanonicalDataset) -> FeasibilityCheck:
        traps: list[str] = []
        demand_peaks = {pid: max(periods) if periods else 0.0 for pid, periods in dataset.demand.items()}
        for route in dataset.routes:
            sbq = float(route.get("min_batch_quantity", 0.0))
            cap = float(route.get("trip_capacity", 0.0))
            dest = route.get("destination")
            peak_demand = demand_peaks.get(dest, 0.0)
            if sbq > 0 and peak_demand > 0 and sbq > max(peak_demand, cap):
                traps.append(f"sbq_too_high_for_demand_route_{route['id']}")
            trips_cap = route.get("max_trips_per_period")
            if trips_cap == 0:
                traps.append(f"route_{route['id']}_trips_zero")
        return FeasibilityCheck(name="integer_traps", passed=not traps, severity="warning", details={"issues": traps})

    def _solve_with_fallbacks(
        self,
        base_model: ModelDefinition,
        scenario: ScenarioPlan,
        runtime_limit: int | None,
        scenario_diag: dict,
    ) -> RobustOutcome:
        tiers: List[dict] = []
        tiers.append({"tier": "baseline", "dataset": scenario.dataset, "overrides": {"allow_shortage": base_model.allow_shortage}})

        if not base_model.allow_shortage:
            relaxed_dataset = self._relax_routes(scenario.dataset)
            tiers.append(
                {
                    "tier": "soft_relaxation",
                    "dataset": relaxed_dataset,
                    "overrides": {
                        "allow_shortage": True,
                        "shortage_penalty": max(base_model.shortage_penalty or 0.0, 1000.0),
                        "metadata": {**base_model.metadata, "relaxed": True},
                    },
                }
            )

        tiers.append(
            {
                "tier": "simplified_model",
                "dataset": self._aggregate_dataset(scenario.dataset),
                "overrides": {"allow_shortage": True, "shortage_penalty": max(base_model.shortage_penalty or 0.0, 500.0)},
            }
        )

        tiers.append(
            {
                "tier": "heuristic_backup",
                "dataset": scenario.dataset,
                "overrides": {"shortage_penalty": max(base_model.shortage_penalty or 0.0, 250.0)},
            }
        )

        attempts_log: List[dict] = []
        chosen_parsed: ParsedSolution | None = None
        chosen_tier = "baseline"
        dataset_used = scenario.dataset

        for tier in tiers:
            tier_label = tier["tier"]
            tier_dataset = tier["dataset"]
            start = time.monotonic()
            if tier_label == "heuristic_backup":
                result = self._heuristic_solution(
                    tier_dataset,
                    shortage_penalty=tier["overrides"].get("shortage_penalty", base_model.shortage_penalty or 0.0),
                    tier_label=tier_label,
                )
            else:
                model = self._build_model(base_model, tier_dataset, tier["overrides"])
                result = self.solver.solve(model, time_limit=runtime_limit)
            elapsed = time.monotonic() - start
            result.diagnostics["tier"] = tier_label
            result.runtime_seconds = result.runtime_seconds or elapsed

            parsed = self.parser.parse(result, tier_dataset)
            validation_issues = self._post_validate(
                parsed,
                tier_dataset,
                allow_shortage=tier["overrides"].get("allow_shortage", base_model.allow_shortage),
            )
            parsed.kpis["validation_issues"] = validation_issues
            attempts_log.append(
                {
                    "tier": tier_label,
                    "status": result.status,
                    "feasible": result.feasible,
                    "objective_value": result.objective_value,
                    "runtime_seconds": result.runtime_seconds,
                    "validation_issues": validation_issues,
                }
            )

            chosen_parsed = parsed
            chosen_tier = tier_label
            dataset_used = tier_dataset
            if result.feasible:
                break

        validation = chosen_parsed.kpis.get("validation_issues") if chosen_parsed else []
        scenario_diag["attempts"] = attempts_log
        scenario_diag["validation"] = validation
        return RobustOutcome(
            label=scenario.label,
            parsed=chosen_parsed if chosen_parsed else self.parser.parse(self._heuristic_solution(dataset_used, base_model.shortage_penalty or 0.0, "heuristic_backup"), dataset_used),
            multiplier=scenario.multiplier,
            probability=scenario.probability,
            tier_used=chosen_tier,
            diagnostics=scenario_diag,
            dataset=dataset_used,
        )

    def _build_model(self, base_model: ModelDefinition, dataset: CanonicalDataset, overrides: dict | None = None) -> ModelDefinition:
        overrides = overrides or {}
        metadata: dict = {**dataset.metadata, **base_model.metadata}
        metadata.update(overrides.get("metadata", {}))
        if "scenario_label" not in metadata:
            scenario_label = overrides.get("metadata", {}).get("scenario_label") or metadata.get("label") or metadata.get("name") or dataset.metadata.get("scenario_label")
            if scenario_label:
                metadata["scenario_label"] = scenario_label

        return ModelDefinition(
            dataset=dataset,
            mode="robust",
            uncertainty=base_model.uncertainty,
            scenario_weights=base_model.scenario_weights,
            metadata=metadata,
            allow_shortage=overrides.get("allow_shortage", base_model.allow_shortage),
            shortage_penalty=overrides.get("shortage_penalty", base_model.shortage_penalty),
            service_level_target=base_model.service_level_target,
        )

    def _relax_routes(self, dataset: CanonicalDataset) -> CanonicalDataset:
        relaxed_routes = []
        for route in dataset.routes:
            max_trips = route.get("max_trips_per_period")
            relaxed_max = None
            if isinstance(max_trips, (int, float)):
                relaxed_max = max_trips * 2 if max_trips else None
            sbq = float(route.get("min_batch_quantity", 0.0))
            cap = float(route.get("trip_capacity", 0.0))
            relaxed_routes.append(
                {
                    **route,
                    "max_trips_per_period": relaxed_max,
                    "min_batch_quantity": min(sbq, cap) if cap else sbq,
                }
            )

        return CanonicalDataset(
            organization_id=dataset.organization_id,
            scenario_id=dataset.scenario_id,
            periods=dataset.periods,
            plants=copy.deepcopy(dataset.plants),
            routes=relaxed_routes,
            inventory=copy.deepcopy(dataset.inventory),
            demand=copy.deepcopy(dataset.demand),
            safety_stock=copy.deepcopy(dataset.safety_stock),
            min_fulfillment=copy.deepcopy(dataset.min_fulfillment),
            metadata={**dataset.metadata, "relaxed_routes": True},
        )

    def _aggregate_dataset(self, dataset: CanonicalDataset) -> CanonicalDataset:
        aggregated_demand = {pid: [sum(periods)] for pid, periods in dataset.demand.items()}
        aggregated_min = {pid: [min(periods) if periods else 1.0] for pid, periods in dataset.min_fulfillment.items()}
        return CanonicalDataset(
            organization_id=dataset.organization_id,
            scenario_id=dataset.scenario_id,
            periods=1,
            plants=copy.deepcopy(dataset.plants),
            routes=copy.deepcopy(dataset.routes),
            inventory=copy.deepcopy(dataset.inventory),
            demand=aggregated_demand,
            safety_stock=copy.deepcopy(dataset.safety_stock),
            min_fulfillment=aggregated_min,
            metadata={**dataset.metadata, "aggregated_periods": True},
        )

    def _heuristic_solution(self, dataset: CanonicalDataset, shortage_penalty: float, tier_label: str) -> SolverResult:
        shortage_plan: Dict[Tuple[int, int], float] = {}
        total_shortage = 0.0
        for plant_id, periods in dataset.demand.items():
            for idx, qty in enumerate(periods, start=1):
                if qty > 0:
                    shortage_plan[(plant_id, idx)] = qty
                    total_shortage += qty

        cost = round((shortage_penalty or 0.0) * total_shortage, 2)
        return SolverResult(
            feasible=True,
            status="heuristic_feasible",
            objective_value=cost,
            shipment_plan={},
            production_plan={},
            inventory_plan={},
            shortage_plan=shortage_plan,
            cost_breakdown={
                "production_cost": 0.0,
                "transport_cost": 0.0,
                "holding_cost": 0.0,
                "shortage_cost": cost,
                "total_cost": cost,
            },
            diagnostics={"tier": tier_label, "note": "heuristic_shortage_only"},
            runtime_seconds=0.0,
        )

    def _post_validate(self, parsed: ParsedSolution, dataset: CanonicalDataset, allow_shortage: bool) -> dict:
        qa_checks = [
            self._qa_mass_balance(parsed, dataset),
            self._qa_inventory_recursion(parsed, dataset),
            self._qa_integer_trips(parsed, dataset),
            self._qa_sbq_enforcement(parsed, dataset),
            self._qa_production_capacity(parsed, dataset),
            self._qa_inventory_limits(parsed, dataset),
            self._qa_demand_fulfillment(parsed, dataset, allow_shortage),
        ]
        issues = [chk["name"] for chk in qa_checks if not chk.get("passed", False)]
        return {"passed": not issues, "issues": issues, "checks": qa_checks}

    def _build_storage_payload(
        self,
        worst: RobustOutcome,
        outcomes: List[RobustOutcome],
        aggregate_result: SolverResult,
        base_precheck: dict,
    ) -> dict:
        scenario_record = ScenarioResultRecord(
            scenario_id=worst.dataset.scenario_id,
            organization_id=worst.dataset.organization_id,
            mode="robust",
            status=aggregate_result.status,
            runtime=aggregate_result.runtime_seconds,
            mip_gap=worst.parsed.kpis.get("mip_gap"),
            feasibility_mode_used=worst.tier_used,
            notes="worst_case_selection",
        )

        cost_record = CostResultRecord(
            scenario_id=worst.dataset.scenario_id,
            total_cost=float(worst.parsed.total_cost),
            production_cost=float(worst.parsed.cost_breakdown.get("production_cost", 0.0)),
            transport_cost=float(worst.parsed.cost_breakdown.get("transport_cost", 0.0)),
            holding_cost=float(worst.parsed.cost_breakdown.get("holding_cost", 0.0)),
            penalty_cost=float(worst.parsed.cost_breakdown.get("shortage_cost", 0.0)),
            scenario_expected_cost=aggregate_result.objective_value,
            worst_case_cost=float(worst.parsed.total_cost),
        )

        shipment_records = [rec.as_dict() for rec in self._structure_shipment_plan(worst, worst.dataset)]
        inventory_records = [rec.as_dict() for rec in self._structure_inventory_plan(worst, worst.dataset)]
        production_records = [rec.as_dict() for rec in self._structure_production_plan(worst)]

        comparison = self._build_comparison(outcomes, worst)

        return {
            "scenario_result": scenario_record.as_dict(),
            "cost_result": cost_record.as_dict(),
            "shipment_plan": shipment_records,
            "inventory": inventory_records,
            "production": production_records,
            "comparison": comparison,
            "audit": {
                "precheck": base_precheck,
                "attempts": worst.diagnostics.get("attempts", []),
                "validation": worst.diagnostics.get("validation", {}),
            },
        }

    def _structure_shipment_plan(self, outcome: RobustOutcome, dataset: CanonicalDataset) -> List[ShipmentPlanRecord]:
        shipments: List[ShipmentPlanRecord] = []
        route_index = {}
        for route in dataset.routes:
            key = (route.get("source"), route.get("destination"))
            route_index.setdefault(key, []).append(route)

        for key, quantity in outcome.parsed.shipment_plan.items():
            period = 1
            source = None
            dest = None
            mode = None
            trips = None
            sbq_used = None
            route_cost = None

            if isinstance(key, tuple):
                if len(key) == 3:
                    source, dest, period = key
                elif len(key) == 4:
                    source, dest, period, mode = key
                elif len(key) == 2:
                    source, dest = key

            route_candidates = route_index.get((source, dest), []) if source is not None and dest is not None else []
            chosen_route = route_candidates[0] if route_candidates else None
            if chosen_route:
                mode = mode or chosen_route.get("mode")
                sbq_used = self._safe_float(chosen_route.get("min_batch_quantity"))
                unit_cost = self._safe_float(
                    chosen_route.get("transport_cost")
                    or chosen_route.get("variable_cost")
                    or chosen_route.get("cost")
                    or chosen_route.get("unit_cost")
                )
                route_cost = round(unit_cost * float(quantity), 4)
                trips = chosen_route.get("max_trips_per_period")

            shipments.append(
                ShipmentPlanRecord(
                    scenario_id=dataset.scenario_id,
                    period=int(period),
                    source_plant_id=source,
                    destination_plant_id=dest,
                    mode=mode,
                    trips=trips if trips is None else float(trips),
                    quantity=float(quantity),
                    sbq_used=sbq_used,
                    route_cost=route_cost,
                )
            )
        return shipments

    def _structure_inventory_plan(self, outcome: RobustOutcome, dataset: CanonicalDataset) -> List[InventoryResultRecord]:
        records: List[InventoryResultRecord] = []
        safety_stock = dataset.safety_stock or {}
        for (plant_id, period), qty in outcome.parsed.inventory_plan.items():
            safety = safety_stock.get(plant_id, 0.0)
            status = "safe"
            if qty < safety:
                status = "violation"
            elif qty < safety * 1.1:
                status = "warning"
            records.append(
                InventoryResultRecord(
                    scenario_id=dataset.scenario_id,
                    period=int(period),
                    plant_id=plant_id,
                    inventory_level=float(qty),
                    safety_status=status,
                )
            )
        return records

    def _structure_production_plan(self, outcome: RobustOutcome) -> List[ProductionResultRecord]:
        records: List[ProductionResultRecord] = []
        for (plant_id, period), qty in outcome.parsed.production_plan.items():
            records.append(
                ProductionResultRecord(
                    scenario_id=outcome.dataset.scenario_id,
                    plant_id=plant_id,
                    period=int(period),
                    production_quantity=float(qty),
                )
            )
        return records

    def _extract_kpis(self, parsed: ParsedSolution, dataset: CanonicalDataset, storage: dict) -> dict:
        shipments = storage.get("shipment_plan", [])
        inventory = storage.get("inventory", [])
        production = storage.get("production", [])
        total_moved = sum(item.get("quantity", 0.0) for item in shipments)
        routes_used = {}
        for item in shipments:
            key = (item.get("source_plant_id"), item.get("destination_plant_id"))
            routes_used.setdefault(key, []).append(item)

        route_utilization = {
            f"{src}->{dst}": sum(r.get("quantity", 0.0) for r in vals)
            for (src, dst), vals in routes_used.items()
        }

        prod_by_plant = {}
        for prod in production:
            prod_by_plant[prod["plant_id"]] = prod_by_plant.get(prod["plant_id"], 0.0) + prod.get("production_quantity", 0.0)
        top_prod = sorted(prod_by_plant.items(), key=lambda x: x[1], reverse=True)
        hub = top_prod[0][0] if top_prod else None

        safety_issues = [rec for rec in inventory if rec.get("safety_status") == "violation"]
        warning_issues = [rec for rec in inventory if rec.get("safety_status") == "warning"]

        cost = storage.get("cost_result", {})
        cost_efficiency = None
        total_demand = sum(sum(periods) for periods in dataset.demand.values()) or 1.0
        if cost:
            cost_efficiency = round(cost.get("total_cost", 0.0) / total_demand, 4)

        risk_exposure = parsed.kpis.get("shortage_units", 0.0)
        stability_score = self._calc_inventory_stability(inventory)

        return {
            "total_clinker_moved": total_moved,
            "average_shipment_per_route": round(total_moved / max(len(routes_used), 1), 4),
            "route_utilization": route_utilization,
            "mode_efficiency": self._mode_efficiency(shipments),
            "most_utilized_unit": hub,
            "production_load": prod_by_plant,
            "inventory_violations": [r.get("plant_id") for r in safety_issues],
            "inventory_warnings": [r.get("plant_id") for r in warning_issues],
            "cost_structure": {
                "transport": cost.get("transport_cost", 0.0) if cost else 0.0,
                "production": cost.get("production_cost", 0.0) if cost else 0.0,
                "holding": cost.get("holding_cost", 0.0) if cost else 0.0,
                "penalty": cost.get("penalty_cost", 0.0) if cost else 0.0,
            },
            "cost_efficiency_index": cost_efficiency,
            "risk_exposure_units": risk_exposure,
            "stability_score": stability_score,
        }

    def _build_narrative(self, storage: dict, kpis: dict) -> List[str]:
        narrative: List[str] = []
        cost = storage.get("cost_result", {})
        scenario = storage.get("scenario_result", {})
        total_cost = cost.get("total_cost")
        worst = cost.get("worst_case_cost")
        if total_cost is not None and worst is not None:
            narrative.append(
                f"Robust plan selects worst-case scenario with total cost {total_cost:.2f} and guardrail at {worst:.2f}."
            )

        hub = kpis.get("most_utilized_unit")
        if hub:
            narrative.append(f"Unit {hub} emerges as primary supply hub based on production allocation patterns.")

        moved = kpis.get("total_clinker_moved")
        if moved is not None:
            narrative.append(f"Network moves {moved:.2f} total units across all routes, emphasizing mode efficiency gains.")

        if kpis.get("inventory_violations"):
            narrative.append(
                "Inventory risk detected at " + ", ".join(map(str, kpis["inventory_violations"])) + "; consider capacity or reroute adjustments."
            )
        elif kpis.get("inventory_warnings"):
            narrative.append("Inventory stable with warnings at " + ", ".join(map(str, kpis["inventory_warnings"])) + ".")
        else:
            narrative.append("All plants maintain inventory above safety thresholds across periods.")

        comp = storage.get("comparison", {})
        if comp.get("summary"):
            narrative.append(comp["summary"])

        if scenario.get("feasibility_mode_used"):
            narrative.append(
                f"Feasibility achieved via tier '{scenario['feasibility_mode_used']}', preserving audit trail for fallback behavior."
            )
        return narrative

    def _build_comparison(self, outcomes: List[RobustOutcome], worst: RobustOutcome) -> dict:
        summary = ""
        feasible_count = sum(1 for o in outcomes if o.parsed.feasible)
        if len(outcomes) > 1:
            summary = (
                f"Compared {len(outcomes)} stressed scenarios; {feasible_count} feasible. "
                f"Worst-case labeled '{worst.label}' chosen for execution."
            )
        breakdown = [
            {
                "label": o.label,
                "cost": o.parsed.total_cost,
                "feasible": o.parsed.feasible,
                "tier": o.tier_used,
                "probability": o.probability,
                "multiplier": o.multiplier,
            }
            for o in outcomes
        ]
        return {"summary": summary, "scenarios": breakdown}

    def _calc_inventory_stability(self, inventory_records: List[dict]) -> float:
        if not inventory_records:
            return 1.0
        safe = sum(1 for rec in inventory_records if rec.get("safety_status") == "safe")
        return round(safe / len(inventory_records), 4)

    def _mode_efficiency(self, shipments: List[dict]) -> dict:
        efficiency: Dict[str, float] = {}
        totals: Dict[str, float] = {}
        for shp in shipments:
            mode = shp.get("mode") or "unknown"
            totals[mode] = totals.get(mode, 0.0) + shp.get("quantity", 0.0)
            if shp.get("route_cost"):
                efficiency[mode] = efficiency.get(mode, 0.0) + shp.get("route_cost", 0.0)
        for mode, total_qty in totals.items():
            if total_qty > 0 and mode in efficiency:
                efficiency[mode] = round(efficiency[mode] / total_qty, 4)
        return efficiency

    def _safe_float(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _build_digital_twin_view(
        self,
        worst: RobustOutcome,
        outcomes: List[RobustOutcome],
        storage: dict,
        kpis: dict,
    ) -> dict:
        scenario_status = storage.get("scenario_result", {}).get("status")
        shipments = storage.get("shipment_plan", [])
        inventory = storage.get("inventory", [])
        production = storage.get("production", [])
        cost = storage.get("cost_result", {})

        nodes = self._build_nodes(worst.dataset, production, inventory)
        flows = self._build_flows(worst.dataset, shipments)
        heat_layers = self._build_heat_layers(flows, inventory, cost, kpis)
        bottlenecks = self._detect_bottlenecks(flows, inventory)
        dashboards = self._build_dashboards(worst, shipments, production, inventory, cost, kpis)
        variants = self._build_variant_views(outcomes)

        return {
            "status": scenario_status,
            "nodes": nodes,
            "flows": flows,
            "heat_layers": heat_layers,
            "bottlenecks": bottlenecks,
            "dashboards": dashboards,
            "variants": variants,
            "cta": self._build_cta(scenario_status),
        }

    def _build_nodes(self, dataset: CanonicalDataset, production: List[dict], inventory: List[dict]) -> List[dict]:
        plants = dataset.plants or []
        safety_lookup = dataset.safety_stock or {}
        inventory_latest = self._latest_inventory_snapshot(inventory)
        production_totals: Dict[Any, float] = {}
        for rec in production:
            production_totals[rec.get("plant_id")] = production_totals.get(rec.get("plant_id"), 0.0) + rec.get("production_quantity", 0.0)

        nodes: List[dict] = []
        for plant in plants:
            pid = plant.get("id")
            plant_type = plant.get("type") or "unknown"
            demand_total = sum(dataset.demand.get(pid, [])) if dataset.demand else 0.0
            inventory_info = inventory_latest.get(pid, {})
            safety = safety_lookup.get(pid, 0.0)
            status = inventory_info.get("safety_status") or "safe"
            role = "supply" if plant_type == "IU" else "demand" if plant_type == "GU" else "hybrid"
            nodes.append(
                {
                    "id": pid,
                    "name": plant.get("name") or plant.get("code") or str(pid),
                    "type": plant_type,
                    "role": role,
                    "production": round(production_totals.get(pid, 0.0), 4),
                    "demand": round(demand_total, 4),
                    "inventory": {
                        "level": inventory_info.get("inventory_level", 0.0),
                        "safety": safety,
                        "status": status,
                    },
                    "location": {
                        "lat": plant.get("lat") or plant.get("latitude"),
                        "lon": plant.get("lon") or plant.get("longitude"),
                    },
                    "glow": "critical" if status == "violation" else "warning" if status == "warning" else "active",
                    "metadata": {"scenario_id": dataset.scenario_id, "organization_id": dataset.organization_id},
                }
            )
        return nodes

    def _build_flows(self, dataset: CanonicalDataset, shipments: List[dict]) -> List[dict]:
        route_lookup = self._route_lookup(dataset.routes)
        flows: List[dict] = []
        for shp in shipments:
            src = shp.get("source_plant_id")
            dst = shp.get("destination_plant_id")
            period = shp.get("period")
            qty = shp.get("quantity", 0.0) or 0.0
            route = route_lookup.get((src, dst))
            cap = self._route_capacity(route)
            utilization = round(qty / cap, 4) if cap else None
            severity = "ok"
            if utilization is not None:
                if utilization > 1.0:
                    severity = "critical"
                elif utilization >= 0.85:
                    severity = "warning"

            flow_id = f"{src}->{dst}|p{period}"
            flows.append(
                {
                    "id": flow_id,
                    "source": src,
                    "target": dst,
                    "period": period,
                    "mode": shp.get("mode") or (route.get("mode") if route else None),
                    "quantity": round(qty, 4),
                    "utilization": utilization,
                    "severity": severity,
                    "cost": shp.get("route_cost"),
                    "sbq": shp.get("sbq_used") or (route.get("min_batch_quantity") if route else None),
                    "trips": shp.get("trips") or (route.get("max_trips_per_period") if route else None),
                    "heat": {
                        "cost": shp.get("route_cost"),
                        "utilization": utilization,
                        "risk": 1.0 if severity == "critical" else 0.6 if severity == "warning" else 0.2,
                    },
                }
            )
        return flows

    def _build_heat_layers(self, flows: List[dict], inventory: List[dict], cost: dict, kpis: dict) -> dict:
        cost_values = [f.get("heat", {}).get("cost") for f in flows if f.get("heat", {}).get("cost") is not None]
        util_values = [f.get("utilization") for f in flows if f.get("utilization") is not None]
        risk_values = [f.get("heat", {}).get("risk") for f in flows if f.get("heat", {}).get("risk") is not None]
        inv_flags = {rec.get("plant_id"): rec.get("safety_status") for rec in inventory}

        cost_layer = self._normalize_heat(flows, cost_values, "cost")
        util_layer = self._normalize_heat(flows, util_values, "utilization")
        risk_layer = self._normalize_heat(flows, risk_values, "risk")

        return {
            "cost": cost_layer,
            "utilization": util_layer,
            "risk": risk_layer,
            "inventory": {"violations": [pid for pid, status in inv_flags.items() if status == "violation"], "warnings": [pid for pid, status in inv_flags.items() if status == "warning"]},
            "totals": {
                "network_cost": cost.get("total_cost"),
                "transport_cost": cost.get("transport_cost"),
                "stability": kpis.get("stability_score"),
            },
        }

    def _normalize_heat(self, flows: List[dict], values: List[float], key: str) -> List[dict]:
        if not flows or not values:
            return []
        max_val = max(values) or 1.0
        layer = []
        for flow in flows:
            val = flow.get("heat", {}).get(key)
            if val is None:
                continue
            intensity = round(float(val) / float(max_val), 4)
            layer.append({"id": flow.get("id"), "source": flow.get("source"), "target": flow.get("target"), "intensity": intensity})
        return layer

    def _detect_bottlenecks(self, flows: List[dict], inventory: List[dict]) -> dict:
        critical_routes = [f for f in flows if f.get("severity") == "critical"]
        warning_routes = [f for f in flows if f.get("severity") == "warning"]
        inv_issues = [rec for rec in inventory if rec.get("safety_status") in {"warning", "violation"}]
        return {
            "routes": {
                "critical": sorted(critical_routes, key=lambda x: x.get("utilization") or 0, reverse=True),
                "warnings": sorted(warning_routes, key=lambda x: x.get("utilization") or 0, reverse=True),
            },
            "inventory": inv_issues,
        }

    def _build_dashboards(
        self,
        worst: RobustOutcome,
        shipments: List[dict],
        production: List[dict],
        inventory: List[dict],
        cost: dict,
        kpis: dict,
    ) -> dict:
        inventory_trend = self._inventory_trend(inventory)
        capacity_util = self._capacity_utilization(worst.dataset, production)
        cost_breakdown = {
            "total": cost.get("total_cost", 0.0),
            "production": cost.get("production_cost", 0.0),
            "transport": cost.get("transport_cost", 0.0),
            "holding": cost.get("holding_cost", 0.0),
            "penalty": cost.get("penalty_cost", 0.0),
        }
        transport_mix = self._mode_efficiency(shipments)
        return {
            "inventory_trend": inventory_trend,
            "capacity_utilization": capacity_util,
            "cost_breakdown": cost_breakdown,
            "transport_mix": transport_mix,
            "kpis": {
                "stability_score": kpis.get("stability_score"),
                "risk_exposure_units": kpis.get("risk_exposure_units"),
                "service_level_pct": worst.parsed.kpis.get("service_level_pct"),
                "worst_case_cost": worst.parsed.total_cost,
            },
        }

    def _build_variant_views(self, outcomes: List[RobustOutcome]) -> List[dict]:
        variants: List[dict] = []
        for outcome in outcomes:
            shipments = [rec.as_dict() for rec in self._structure_shipment_plan(outcome, outcome.dataset)]
            inventory = [rec.as_dict() for rec in self._structure_inventory_plan(outcome, outcome.dataset)]
            production = [rec.as_dict() for rec in self._structure_production_plan(outcome)]
            variants.append(
                {
                    "label": outcome.label,
                    "tier": outcome.tier_used,
                    "feasible": outcome.parsed.feasible,
                    "status": outcome.parsed.solver_status,
                    "total_cost": outcome.parsed.total_cost,
                    "service_level_pct": outcome.parsed.kpis.get("service_level_pct"),
                    "nodes": self._build_nodes(outcome.dataset, production, inventory),
                    "flows": self._build_flows(outcome.dataset, shipments),
                }
            )
        return variants

    def _build_cta(self, status: Optional[str]) -> str:
        if status is None:
            return "Run optimization first to activate Digital Twin."
        if status.lower() not in {"optimal", "feasible"}:
            return "Digital Twin active with fallback; review bottlenecks to regain feasibility."
        return "Digital Twin live. Explore flows, costs, and bottlenecks."

    def _route_lookup(self, routes: List[dict]) -> Dict[Tuple[Any, Any], dict]:
        lookup: Dict[Tuple[Any, Any], dict] = {}
        for route in routes or []:
            key = (route.get("source"), route.get("destination"))
            if key not in lookup:
                lookup[key] = route
        return lookup

    def _route_capacity(self, route: Optional[dict]) -> Optional[float]:
        if not route:
            return None
        trip_capacity = self._safe_float(route.get("trip_capacity")) or 0.0
        max_trips = route.get("max_trips_per_period")
        max_trips_val = self._safe_float(max_trips) if max_trips is not None else None
        if max_trips_val is None:
            return trip_capacity if trip_capacity > 0 else None
        capacity = trip_capacity * max_trips_val
        return capacity if capacity > 0 else None

    def _latest_inventory_snapshot(self, inventory: List[dict]) -> Dict[Any, dict]:
        snapshot: Dict[Any, dict] = {}
        for rec in inventory:
            pid = rec.get("plant_id")
            period = rec.get("period") or 0
            existing = snapshot.get(pid)
            if existing is None or period >= existing.get("period", 0):
                snapshot[pid] = rec
        return snapshot

    def _inventory_trend(self, inventory: List[dict]) -> Dict[Any, List[dict]]:
        trend: Dict[Any, List[dict]] = {}
        for rec in inventory:
            pid = rec.get("plant_id")
            trend.setdefault(pid, []).append({"period": rec.get("period"), "level": rec.get("inventory_level"), "status": rec.get("safety_status")})
        for pid, series in trend.items():
            series.sort(key=lambda x: x.get("period") or 0)
        return trend

    def _capacity_utilization(self, dataset: CanonicalDataset, production: List[dict]) -> Dict[Any, dict]:
        capacity: Dict[Any, float] = {}
        for plant in dataset.plants or []:
            pid = plant.get("id")
            capacity[pid] = self._safe_float(plant.get("production_capacity"))
        usage: Dict[Any, float] = {}
        for rec in production:
            pid = rec.get("plant_id")
            usage[pid] = usage.get(pid, 0.0) + rec.get("production_quantity", 0.0)
        utilization: Dict[Any, dict] = {}
        for pid, cap in capacity.items():
            used = usage.get(pid, 0.0)
            util = round(used / cap, 4) if cap else None
            utilization[pid] = {"used": used, "capacity": cap, "utilization": util}
        return utilization

    def _build_comparative_intelligence(self, outcomes: List[RobustOutcome], worst: RobustOutcome, storage: dict, kpis: dict) -> dict:
        baseline = self._select_baseline_outcome(outcomes)
        shipments = storage.get("shipment_plan", [])
        inventory = storage.get("inventory", [])
        cost_records = storage.get("cost_result", {})
        flows = self._build_flows(worst.dataset, shipments)

        deterministic_cost = baseline.parsed.total_cost if baseline else None
        expected_cost = self._expected_cost(outcomes)
        robust_cost = worst.parsed.total_cost
        cost_metrics = {
            "deterministic_cost": deterministic_cost,
            "stochastic_expected_cost": expected_cost,
            "robust_worst_cost": robust_cost,
            "additional_cost_of_robustness_pct": self._safe_pct(robust_cost - deterministic_cost, deterministic_cost) if deterministic_cost is not None else None,
            "risk_premium_pct": self._safe_pct(robust_cost - expected_cost, expected_cost) if expected_cost is not None else None,
            "stochastic_expected_benefit_pct": self._safe_pct(deterministic_cost - expected_cost, deterministic_cost) if deterministic_cost is not None and expected_cost is not None else None,
        }

        service_metrics = self._service_metrics(outcomes, baseline, worst)
        inventory_block = self._inventory_stability_block(inventory)
        transport_block = self._transport_stress_indicators(flows)
        risk_block = self._risk_exposure_analytics(outcomes, baseline, worst, service_metrics, transport_block)
        visuals = self._build_visuals_for_comparison(cost_metrics, service_metrics, inventory_block, transport_block, risk_block)
        narrative = self._narrative_engine(cost_metrics, service_metrics, risk_block)

        return {
            "cost": cost_metrics,
            "service": service_metrics,
            "inventory": inventory_block,
            "transport": transport_block,
            "risk": risk_block,
            "visuals": visuals,
            "narrative": narrative,
            "source": {
                "scenario_count": len(outcomes),
                "baseline_label": baseline.label if baseline else None,
                "worst_label": worst.label,
                "scenario_id": worst.dataset.scenario_id,
                "organization_id": worst.dataset.organization_id,
                "cost_record": cost_records,
            },
        }

    def _select_baseline_outcome(self, outcomes: List[RobustOutcome]) -> Optional[RobustOutcome]:
        feasible = [o for o in outcomes if o.parsed.feasible]
        pool = feasible or outcomes
        if not pool:
            return None
        return min(pool, key=lambda o: o.parsed.total_cost)

    def _expected_cost(self, outcomes: List[RobustOutcome]) -> Optional[float]:
        if not outcomes:
            return None
        total_prob = sum(o.probability for o in outcomes) or 0.0
        if total_prob <= 0:
            return None
        val = sum(o.parsed.total_cost * o.probability for o in outcomes)
        return round(val / total_prob, 4)

    def _safe_pct(self, numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
        if numerator is None or denominator in (None, 0):
            return None
        return round((numerator / denominator) * 100.0, 4)

    def _service_metrics(self, outcomes: List[RobustOutcome], baseline: Optional[RobustOutcome], worst: RobustOutcome) -> dict:
        demand_total = sum(sum(periods) for periods in worst.dataset.demand.values()) or 1.0
        shortage_flags = [self._shortage_units(o.parsed) > 0 for o in outcomes]
        shortage_probability = round(sum(1 for f in shortage_flags if f) / len(outcomes), 4) if outcomes else 0.0
        expected_service = None
        if outcomes:
            total_prob = sum(o.probability for o in outcomes) or 0.0
            if total_prob > 0:
                expected_service = round(
                    sum((o.parsed.kpis.get("service_level_pct") or 0.0) * o.probability for o in outcomes) / total_prob,
                    4,
                )

        deterministic_service = baseline.parsed.kpis.get("service_level_pct") if baseline else None
        robust_service = worst.parsed.kpis.get("service_level_pct")
        guaranteed = self._shortage_units(worst.parsed) <= 1e-6
        shortage_units = self._shortage_units(worst.parsed)
        fulfilled_pct = round(((demand_total - shortage_units) / demand_total) * 100.0, 4)
        service_score = None
        parts = [p for p in [deterministic_service, expected_service, robust_service] if p is not None]
        if parts:
            service_score = round((0.1 * (deterministic_service or 0) + 0.3 * (expected_service or 0) + 0.6 * (robust_service or 0)), 4)

        return {
            "fulfilled_demand_pct": fulfilled_pct,
            "shortage_probability": shortage_probability,
            "guaranteed_service": guaranteed,
            "service_level_score": service_score,
            "deterministic_service_pct": deterministic_service,
            "stochastic_expected_service_pct": expected_service,
            "robust_service_pct": robust_service,
        }

    def _inventory_stability_block(self, inventory: List[dict]) -> dict:
        if not inventory:
            return {"stability_index": 100.0, "min_safety_distance": None, "variability": None, "risk_dip_frequency": 0.0}
        total = len(inventory)
        safe = sum(1 for rec in inventory if rec.get("safety_status") == "safe")
        warnings = sum(1 for rec in inventory if rec.get("safety_status") == "warning")
        violations = sum(1 for rec in inventory if rec.get("safety_status") == "violation")
        weighted = (safe + 0.6 * warnings + 0.0 * violations) / total
        stability_index = round(weighted * 100.0, 4)
        min_safety_distance, variability = self._inventory_gap_stats(inventory)
        risk_dip_frequency = round((warnings + violations) / total, 4)
        return {
            "stability_index": stability_index,
            "min_safety_distance": min_safety_distance,
            "variability": variability,
            "risk_dip_frequency": risk_dip_frequency,
        }

    def _inventory_gap_stats(self, inventory: List[dict]) -> Tuple[Optional[float], Optional[float]]:
        levels: List[float] = []
        for rec in inventory:
            level = self._safe_float(rec.get("inventory_level"))
            levels.append(level)
        min_distance = round(min(levels), 4) if levels else None
        variability = None
        if levels:
            mean = sum(levels) / len(levels)
            variance = sum((lvl - mean) ** 2 for lvl in levels) / len(levels)
            variability = round(math.sqrt(variance), 4)
        return min_distance, variability

    def _transport_stress_indicators(self, flows: List[dict]) -> dict:
        if not flows:
            return {"overload_risk_pct": None, "route_utilization": {}, "mode_reliability": {}, "stress_signal": None}
        overload = [f for f in flows if (f.get("utilization") or 0) > 1.0]
        warnings = [f for f in flows if 0.9 <= (f.get("utilization") or 0) <= 1.0]
        overload_risk_pct = self._safe_pct(len(overload) + len(warnings), len(flows))
        utilization = {f.get("id"): f.get("utilization") for f in flows if f.get("utilization") is not None}
        mode_reliability: Dict[str, float] = {}
        mode_counts: Dict[str, int] = {}
        for f in flows:
            mode = f.get("mode") or "unknown"
            mode_counts[mode] = mode_counts.get(mode, 0) + 1
            util = f.get("utilization")
            if util is not None:
                mode_reliability[mode] = mode_reliability.get(mode, 0.0) + max(0.0, 1.0 - util)
        for mode, score in mode_reliability.items():
            mode_reliability[mode] = round(score / mode_counts.get(mode, 1), 4)
        stress_signal = "green"
        if overload:
            stress_signal = "red"
        elif warnings:
            stress_signal = "yellow"
        return {
            "overload_risk_pct": overload_risk_pct,
            "route_utilization": utilization,
            "mode_reliability": mode_reliability,
            "stress_signal": stress_signal,
        }

    def _risk_exposure_analytics(
        self,
        outcomes: List[RobustOutcome],
        baseline: Optional[RobustOutcome],
        worst: RobustOutcome,
        service_metrics: dict,
        transport: dict,
    ) -> dict:
        baseline_shortage = self._shortage_units(baseline.parsed) if baseline else None
        robust_shortage = self._shortage_units(worst.parsed)
        reduction_pct = self._safe_pct((baseline_shortage or 0.0) - robust_shortage, baseline_shortage) if baseline_shortage else None
        cost_vals = [o.parsed.total_cost for o in outcomes] if outcomes else []
        volatility = None
        if cost_vals:
            mean = sum(cost_vals) / len(cost_vals)
            variance = sum((c - mean) ** 2 for c in cost_vals) / len(cost_vals)
            volatility = round(math.sqrt(variance), 4)

        failure_risk = service_metrics.get("shortage_probability")
        cost_explosion_risk = self._safe_pct(worst.parsed.total_cost - (baseline.parsed.total_cost if baseline else worst.parsed.total_cost), baseline.parsed.total_cost if baseline else worst.parsed.total_cost)
        disruption_frequency = transport.get("overload_risk_pct")

        return {
            "risk_exposure_reduction_pct": reduction_pct,
            "failure_risk": failure_risk,
            "cost_volatility": volatility,
            "cost_worst_case_explosion_pct": cost_explosion_risk,
            "disruption_frequency": disruption_frequency,
        }

    def _build_visuals_for_comparison(self, cost: dict, service: dict, inventory: dict, transport: dict, risk: dict) -> dict:
        cost_bars = [
            {"label": "Deterministic", "value": cost.get("deterministic_cost")},
            {"label": "Stochastic Expected", "value": cost.get("stochastic_expected_cost")},
            {"label": "Robust Worst", "value": cost.get("robust_worst_cost")},
        ]
        deltas = {
            "robust_vs_det_pct": cost.get("additional_cost_of_robustness_pct"),
            "robust_vs_stoch_pct": cost.get("risk_premium_pct"),
            "stoch_vs_det_pct": cost.get("stochastic_expected_benefit_pct"),
        }
        service_gauge = {
            "value": service.get("robust_service_pct"),
            "confidence": service.get("service_level_score"),
            "failure_probability": service.get("shortage_probability"),
        }
        inventory_overlay = {
            "stability_index": inventory.get("stability_index"),
            "min_safety_distance": inventory.get("min_safety_distance"),
            "variability": inventory.get("variability"),
        }
        risk_traffic = {
            "demand_reliability": "green" if service.get("guaranteed_service") else "yellow",
            "transport_resilience": transport.get("stress_signal") or "green",
            "stockout_probability": "green" if (service.get("shortage_probability") if service.get("shortage_probability") is not None else 1.0) < 0.05 else "red" if (service.get("shortage_probability") if service.get("shortage_probability") is not None else 1.0) > 0.15 else "yellow",
            "cost_volatility": "green" if (risk.get("cost_volatility") or 0) < 1 else "yellow",
        }
        return {
            "cost_bars": cost_bars,
            "deltas": deltas,
            "service_gauge": service_gauge,
            "inventory_overlay": inventory_overlay,
            "risk_traffic_lights": risk_traffic,
        }

    def _narrative_engine(self, cost: dict, service: dict, risk: dict) -> dict:
        deterministic_msg = "Deterministic solution provides the lowest immediate cost but is sensitive to demand volatility." if cost.get("deterministic_cost") is not None else "Deterministic baseline not supplied; using robust insights only."
        stochastic_msg = "Scenario-based plan balances cost and stability, lowering expected risk." if cost.get("stochastic_expected_cost") is not None else "Stochastic expectation unavailable; consider supplying scenario weights."
        robust_msg = (
            "Robust optimization guarantees coverage in stressed demand; cost premium secures service continuity."
        )
        recommendations = [
            "If priority is minimum cost choose deterministic.",
            "If balanced resilience is needed choose stochastic.",
            "If failure is unacceptable choose robust.",
        ]
        if service.get("guaranteed_service"):
            robust_msg += " Service guarantee holds across evaluated stresses."
        return {
            "deterministic": deterministic_msg,
            "stochastic": stochastic_msg,
            "robust": robust_msg,
            "recommendations": recommendations,
        }

    def _build_reporting_layer(
        self,
        worst: RobustOutcome,
        outcomes: List[RobustOutcome],
        storage: dict,
        kpis: dict,
        narrative: List[str],
        comparison: dict,
    ) -> dict:
        scenario = storage.get("scenario_result", {})
        org = self._safe_slug(scenario.get("organization_id") or "ORG")
        scenario_code = self._safe_slug(scenario.get("scenario_id") or "SCENARIO")
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        reports = {
            "shipment": self._build_shipment_report(storage, kpis, worst.dataset),
            "cost": self._build_cost_report(storage),
            "inventory": self._build_inventory_report(storage, worst.dataset),
            "executive_summary": self._build_executive_summary(storage, kpis, narrative, comparison),
            "comparative": self._build_comparative_report(storage),
        }
        exports = self._build_export_manifest(org, scenario_code, reports, timestamp)
        reporting_center = {
            "available": True,
            "badge": "ready",
            "message": "Reports ready for download; share CSV or PDF for offline review.",
            "actions": [
                "Download Shipment Report",
                "Download Cost Report",
                "Download Inventory Report",
                "Download Executive Summary",
            ],
        }
        return {"reports": reports, "exports": exports, "reporting_center": reporting_center}

    def _build_export_manifest(self, org: str, scenario: str, reports: dict, timestamp: str) -> List[dict]:
        manifest: List[dict] = []
        for code, report in reports.items():
            label = report.get("title") or code
            report_code = self._safe_slug(report.get("code") or code).upper()
            for fmt in ("csv", "pdf"):
                manifest.append(
                    {
                        "report_type": code,
                        "title": label,
                        "format": fmt,
                        "file_name": f"{org}_{scenario}_{report_code}_{timestamp}.{fmt}",
                        "ready": True,
                    }
                )
        return manifest

    def _safe_slug(self, value: Any) -> str:
        text = str(value) if value is not None else ""
        cleaned = "".join(ch if str(ch).isalnum() else "_" for ch in text)
        cleaned = cleaned.strip("_")
        return cleaned or "VALUE"

    def _build_shipment_report(self, storage: dict, kpis: dict, dataset: CanonicalDataset) -> dict:
        shipments = storage.get("shipment_plan", [])
        columns = ["Period", "Source Plant", "Destination Plant", "Mode", "Trips", "Qty", "Cost", "SBQ"]
        rows: List[dict] = []
        for rec in shipments:
            rows.append(
                {
                    "Period": rec.get("period"),
                    "Source Plant": rec.get("source_plant_id"),
                    "Destination Plant": rec.get("destination_plant_id"),
                    "Mode": rec.get("mode"),
                    "Trips": rec.get("trips"),
                    "Qty": rec.get("quantity"),
                    "Cost": rec.get("route_cost"),
                    "SBQ": rec.get("sbq_used"),
                }
            )

        period_summary = self._period_summary(shipments)
        hub = kpis.get("most_utilized_unit")
        demand_total = sum(sum(periods) for periods in dataset.demand.values()) or 1.0
        hub_supply = 0.0
        for rec in storage.get("production", []):
            if rec.get("plant_id") == hub:
                hub_supply += rec.get("production_quantity", 0.0)
        hub_share = self._safe_pct(hub_supply, demand_total)
        exec_line = None
        if hub and hub_share is not None:
            exec_line = f"{hub} acts as primary supply hub supporting {hub_share:.2f}% of total demand."
        elif hub:
            exec_line = f"{hub} acts as primary supply hub for this scenario."

        return {
            "code": "shipment",
            "title": "Shipment Plan Report",
            "formats": ["csv", "pdf"],
            "columns": columns,
            "rows": rows,
            "insights": [
                f"Total shipments: {len(rows)}",
                f"Total moved quantity: {round(kpis.get('total_clinker_moved', 0.0) or 0.0, 4)}",
                f"Average shipment per route: {kpis.get('average_shipment_per_route')}",
                f"Routes used: {len(kpis.get('route_utilization', {}))}",
            ],
            "interpretation": exec_line or "Network routes are allocated with clear source-destination mapping for execution.",
            "period_summary": period_summary,
            "narrative": "Period-wise shipment clarity with mode, trips, quantity, and cost for boardroom-ready export.",
        }

    def _build_cost_report(self, storage: dict) -> dict:
        cost = storage.get("cost_result", {}) or {}
        total = cost.get("total_cost", 0.0) or 0.0
        shares = {
            "production_pct": self._cost_share(cost.get("production_cost"), total),
            "transport_pct": self._cost_share(cost.get("transport_cost"), total),
            "holding_pct": self._cost_share(cost.get("holding_cost"), total),
            "penalty_pct": self._cost_share(cost.get("penalty_cost"), total),
        }
        exec_statement = None
        if shares.get("transport_pct") is not None and shares.get("production_pct") is not None:
            exec_statement = (
                f"Transport represents {shares['transport_pct']:.2f}% of total cost; production contributes {shares['production_pct']:.2f}% - action on logistics can unlock savings."
            )
        else:
            exec_statement = "Cost structure captured for executive review with production, transport, holding, and penalty components."

        return {
            "code": "cost",
            "title": "Cost Breakdown Report",
            "formats": ["csv", "pdf"],
            "totals": cost,
            "cost_share": shares,
            "table": [
                {"Cost Type": "Total", "Value": total},
                {"Cost Type": "Production", "Value": cost.get("production_cost")},
                {"Cost Type": "Transport", "Value": cost.get("transport_cost")},
                {"Cost Type": "Holding", "Value": cost.get("holding_cost")},
                {"Cost Type": "Penalties", "Value": cost.get("penalty_cost")},
                {"Cost Type": "Worst-case Guardrail", "Value": cost.get("worst_case_cost")},
                {"Cost Type": "Expected Scenario Cost", "Value": cost.get("scenario_expected_cost")},
            ],
            "narrative": exec_statement,
            "kpi_highlights": {
                "cost_efficiency_index": storage.get("analytics", {}).get("cost_efficiency_index"),
                "risk_exposure_units": storage.get("analytics", {}).get("risk_exposure_units"),
            },
        }

    def _build_inventory_report(self, storage: dict, dataset: CanonicalDataset) -> dict:
        inventory = storage.get("inventory", [])
        columns = ["Plant", "Period", "Ending Inventory", "Safety Stock", "Status"]
        rows: List[dict] = []
        safety_lookup = dataset.safety_stock or {}
        for rec in inventory:
            pid = rec.get("plant_id")
            rows.append(
                {
                    "Plant": pid,
                    "Period": rec.get("period"),
                    "Ending Inventory": rec.get("inventory_level"),
                    "Safety Stock": safety_lookup.get(pid, 0.0),
                    "Status": rec.get("safety_status"),
                }
            )
        statuses = self._inventory_status_counts(inventory)
        narrative = "Inventory stable with safety guardrails respected." if statuses.get("violation", 0) == 0 else "Inventory dips detected-monitor at-risk plants and adjust replenishment."
        return {
            "code": "inventory",
            "title": "Inventory Evolution Report",
            "formats": ["csv", "pdf"],
            "columns": columns,
            "rows": rows,
            "narrative": narrative,
            "insights": [
                f"Safe periods: {statuses.get('safe', 0)}",
                f"Warnings: {statuses.get('warning', 0)}",
                f"Violations: {statuses.get('violation', 0)}",
            ],
            "interpretation": "Inventory trajectories against safety stock with clear status flags for risk assurance.",
        }

    def _build_executive_summary(self, storage: dict, kpis: dict, narrative: List[str], comparison: dict) -> dict:
        scenario = storage.get("scenario_result", {})
        cost = storage.get("cost_result", {})
        summary_points = narrative[:4] if narrative else []
        summary_points.append(comparison.get("summary"))
        summary_points = [pt for pt in summary_points if pt]
        kpi_block = {
            "worst_case_cost": cost.get("worst_case_cost"),
            "service_level_pct": storage.get("analytics", {}).get("stability_score"),
            "inventory_stability": storage.get("analytics", {}).get("stability_score"),
            "route_utilization": list((kpis.get("route_utilization") or {}).items())[:3],
        }
        return {
            "code": "executive_summary",
            "title": "Executive Summary Report",
            "formats": ["pdf"],
            "scenario_context": {
                "scenario_id": scenario.get("scenario_id"),
                "organization_id": scenario.get("organization_id"),
                "mode": scenario.get("mode"),
                "status": scenario.get("status"),
            },
            "highlights": summary_points,
            "kpis": kpi_block,
            "narrative": "; ".join(summary_points) if summary_points else "Robust optimization completed with executive-ready insights.",
            "recommendations": [
                "Maintain current hub allocation while monitoring transport utilization hotspots.",
                "Keep safety stock buffers at flagged plants until demand stabilizes.",
                "Use cost share view to prioritize transport savings without risking service levels.",
            ],
        }

    def _build_comparative_report(self, storage: dict) -> dict:
        comparative = storage.get("comparative_intelligence", {})
        comparison = storage.get("comparison", {})
        return {
            "code": "comparative",
            "title": "Optimization Comparative Summary Report",
            "formats": ["pdf"],
            "cost": comparative.get("cost", {}),
            "service": comparative.get("service", {}),
            "risk": comparative.get("risk", {}),
            "narrative": comparative.get("narrative", {}),
            "scenarios": comparison.get("scenarios", []),
            "summary": comparison.get("summary"),
            "recommendation": "Robust mode selected when service assurance outweighs cost minimization; refer to deltas for quantified trade-off.",
        }

    def _period_summary(self, shipments: List[dict]) -> List[dict]:
        summary: Dict[int, dict] = {}
        for rec in shipments:
            period = int(rec.get("period", 0))
            summary.setdefault(period, {"period": period, "trips": 0.0, "quantity": 0.0, "routes": 0})
            summary[period]["trips"] = (summary[period]["trips"] or 0.0) + (rec.get("trips") or 0.0)
            summary[period]["quantity"] = (summary[period]["quantity"] or 0.0) + (rec.get("quantity") or 0.0)
            summary[period]["routes"] = summary[period]["routes"] + 1
        return sorted(summary.values(), key=lambda x: x.get("period"))

    def _inventory_status_counts(self, inventory: List[dict]) -> Dict[str, int]:
        counts = {"safe": 0, "warning": 0, "violation": 0}
        for rec in inventory:
            status = rec.get("safety_status") or "safe"
            if status in counts:
                counts[status] += 1
        return counts

    # --- Mathematical QA Engine helpers ---

    def _qa_mass_balance(self, parsed: ParsedSolution, dataset: CanonicalDataset) -> dict:
        tol = 1e-5
        shipments = self._iter_shipments(parsed.shipment_plan)
        inbound, outbound = self._shipment_balance(shipments)
        production = parsed.production_plan or {}
        inventory = parsed.inventory_plan or {}
        shortage = parsed.shortage_plan or {}
        demand_map = dataset.demand or {}
        plants = self._all_plants(dataset, shipments)
        violations: List[dict] = []

        for pid in plants:
            prev_inv = self._safe_float(dataset.inventory.get(pid, 0.0))
            periods = self._period_count(pid, dataset)
            for period in range(1, periods + 1):
                inv_end = self._safe_float(inventory.get((pid, period), 0.0))
                prod = self._safe_float(production.get((pid, period), 0.0))
                inbound_qty = self._safe_float(inbound.get((pid, period), 0.0))
                outbound_qty = self._safe_float(outbound.get((pid, period), 0.0))
                demand = self._demand_for(demand_map, pid, period)
                shortage_qty = self._safe_float(shortage.get((pid, period), 0.0))

                rhs = prev_inv + prod + inbound_qty - outbound_qty - demand + shortage_qty
                gap = inv_end - rhs
                if abs(gap) > tol:
                    violations.append(
                        {
                            "plant": pid,
                            "period": period,
                            "lhs_inventory": round(inv_end, 6),
                            "rhs_balance": round(rhs, 6),
                            "gap": round(gap, 6),
                        }
                    )
                prev_inv = inv_end

        return {
            "name": "mass_balance_conservation",
            "passed": not violations,
            "severity": "error",
            "violations": violations,
            "tolerance": tol,
            "statement": "Inventory(t) = Inventory(t-1) + Production + Inbound - Outbound - Demand + Shortage",
        }

    def _qa_inventory_recursion(self, parsed: ParsedSolution, dataset: CanonicalDataset) -> dict:
        tol = 1e-6
        inventory = parsed.inventory_plan or {}
        demand_map = dataset.demand or {}
        shipments = self._iter_shipments(parsed.shipment_plan)
        inbound, outbound = self._shipment_balance(shipments)
        production = parsed.production_plan or {}
        shortage = parsed.shortage_plan or {}
        violations: List[dict] = []

        for pid in self._all_plants(dataset, shipments):
            prev_inv = self._safe_float(dataset.inventory.get(pid, 0.0))
            periods = self._period_count(pid, dataset)
            for period in range(1, periods + 1):
                end_inv = self._safe_float(inventory.get((pid, period), 0.0))
                if end_inv < -tol:
                    violations.append({"plant": pid, "period": period, "inventory": round(end_inv, 6), "issue": "negative_inventory"})
                demand = self._demand_for(demand_map, pid, period)
                shortage_qty = self._safe_float(shortage.get((pid, period), 0.0))
                inbound_qty = self._safe_float(inbound.get((pid, period), 0.0))
                outbound_qty = self._safe_float(outbound.get((pid, period), 0.0))
                prod = self._safe_float(production.get((pid, period), 0.0))
                implied_end = prev_inv + prod + inbound_qty - outbound_qty - demand + shortage_qty
                drift = end_inv - implied_end
                if abs(drift) > tol:
                    violations.append({"plant": pid, "period": period, "inventory": round(end_inv, 6), "expected": round(implied_end, 6), "issue": "recursion_mismatch"})
                prev_inv = end_inv

        return {
            "name": "inventory_recursion",
            "passed": not violations,
            "severity": "error",
            "violations": violations,
            "tolerance": tol,
        }

    def _qa_integer_trips(self, parsed: ParsedSolution, dataset: CanonicalDataset) -> dict:
        tol = 1e-4
        violations: List[dict] = []
        shipments = self._iter_shipments(parsed.shipment_plan)
        for shp in shipments:
            route = self._match_route(dataset.routes, shp)
            if not route:
                continue
            cap = self._safe_float(route.get("trip_capacity"))
            max_trips = route.get("max_trips_per_period")
            if cap <= 0:
                continue
            implied_trips = shp["quantity"] / cap
            rounded = round(implied_trips)
            if abs(implied_trips - rounded) > tol:
                violations.append({"source": shp["source"], "destination": shp["destination"], "period": shp["period"], "implied_trips": round(implied_trips, 6), "issue": "fractional_trip"})
            if max_trips not in (None, "", False):
                max_trips_val = self._safe_float(max_trips)
                if implied_trips - max_trips_val > tol:
                    violations.append({"source": shp["source"], "destination": shp["destination"], "period": shp["period"], "implied_trips": round(implied_trips, 6), "limit": max_trips_val, "issue": "trips_exceed_limit"})

        return {
            "name": "integer_trip_enforcement",
            "passed": not violations,
            "severity": "warning",
            "violations": violations,
            "tolerance": tol,
        }

    def _qa_sbq_enforcement(self, parsed: ParsedSolution, dataset: CanonicalDataset) -> dict:
        tol = 1e-5
        violations: List[dict] = []
        shipments = self._iter_shipments(parsed.shipment_plan)
        for shp in shipments:
            if shp["quantity"] <= tol:
                continue
            route = self._match_route(dataset.routes, shp)
            if not route:
                continue
            sbq = self._safe_float(route.get("min_batch_quantity"))
            cap = self._safe_float(route.get("trip_capacity"))
            max_trips = route.get("max_trips_per_period")
            if sbq > 0 and shp["quantity"] + tol < sbq:
                violations.append({"source": shp["source"], "destination": shp["destination"], "period": shp["period"], "quantity": round(shp["quantity"], 6), "sbq": sbq, "issue": "below_min_batch"})
            if cap > 0 and max_trips not in (None, "", False):
                max_cap = cap * self._safe_float(max_trips)
                if shp["quantity"] - max_cap > tol:
                    violations.append({"source": shp["source"], "destination": shp["destination"], "period": shp["period"], "quantity": round(shp["quantity"], 6), "capacity_limit": max_cap, "issue": "exceeds_trip_capacity"})

        return {
            "name": "sbq_enforcement",
            "passed": not violations,
            "severity": "error",
            "violations": violations,
            "tolerance": tol,
        }

    def _qa_production_capacity(self, parsed: ParsedSolution, dataset: CanonicalDataset) -> dict:
        tol = 1e-5
        violations: List[dict] = []
        prod = parsed.production_plan or {}
        capacity_lookup = {plant.get("id"): self._safe_float(plant.get("production_capacity")) for plant in dataset.plants or []}
        for (pid, period), qty in prod.items():
            cap = capacity_lookup.get(pid)
            if cap is None or cap <= 0:
                continue
            if qty - cap > tol:
                violations.append({"plant": pid, "period": period, "quantity": round(self._safe_float(qty), 6), "capacity": cap, "issue": "production_exceeds_capacity"})
        return {
            "name": "production_capacity",
            "passed": not violations,
            "severity": "error",
            "violations": violations,
            "tolerance": tol,
        }

    def _qa_inventory_limits(self, parsed: ParsedSolution, dataset: CanonicalDataset) -> dict:
        tol = 1e-6
        violations: List[dict] = []
        inv = parsed.inventory_plan or {}
        plant_lookup = {plant.get("id"): plant for plant in dataset.plants or []}
        safety_lookup = dataset.safety_stock or {}
        for (pid, period), qty in inv.items():
            plant = plant_lookup.get(pid, {})
            max_cap = self._safe_float(plant.get("max_inventory_capacity"))
            if max_cap > 0 and qty - max_cap > tol:
                violations.append({"plant": pid, "period": period, "inventory": round(self._safe_float(qty), 6), "capacity": max_cap, "issue": "inventory_above_capacity"})
            safety = self._safe_float(safety_lookup.get(pid, 0.0))
            if qty + tol < safety:
                violations.append({"plant": pid, "period": period, "inventory": round(self._safe_float(qty), 6), "safety_stock": safety, "issue": "safety_stock_breach"})
            if qty < -tol:
                violations.append({"plant": pid, "period": period, "inventory": round(self._safe_float(qty), 6), "issue": "negative_inventory"})

        return {
            "name": "inventory_limits",
            "passed": not violations,
            "severity": "error",
            "violations": violations,
            "tolerance": tol,
        }

    def _qa_demand_fulfillment(self, parsed: ParsedSolution, dataset: CanonicalDataset, allow_shortage: bool) -> dict:
        tol = 1e-5
        shipments = self._iter_shipments(parsed.shipment_plan)
        inbound, outbound = self._shipment_balance(shipments)
        production = parsed.production_plan or {}
        inventory = parsed.inventory_plan or {}
        shortage = parsed.shortage_plan or {}
        demand_map = dataset.demand or {}
        violations: List[dict] = []

        for pid in self._all_plants(dataset, shipments):
            prev_inv = self._safe_float(dataset.inventory.get(pid, 0.0))
            periods = self._period_count(pid, dataset)
            for period in range(1, periods + 1):
                demand = self._demand_for(demand_map, pid, period)
                inbound_qty = self._safe_float(inbound.get((pid, period), 0.0))
                outbound_qty = self._safe_float(outbound.get((pid, period), 0.0))
                prod = self._safe_float(production.get((pid, period), 0.0))
                end_inv = self._safe_float(inventory.get((pid, period), 0.0))
                shortage_qty = self._safe_float(shortage.get((pid, period), 0.0))
                served = prev_inv + prod + inbound_qty - outbound_qty - end_inv
                gap = demand - (served + shortage_qty)
                if gap > tol:
                    violations.append({"plant": pid, "period": period, "unserved_demand": round(gap, 6), "issue": "demand_not_met"})
                if not allow_shortage and shortage_qty > tol:
                    violations.append({"plant": pid, "period": period, "shortage": round(shortage_qty, 6), "issue": "shortage_not_allowed"})
                prev_inv = end_inv

        severity = "warning" if allow_shortage else "error"
        return {
            "name": "demand_fulfillment",
            "passed": not violations,
            "severity": severity,
            "violations": violations,
            "tolerance": tol,
        }

    def _iter_shipments(self, shipment_plan: Dict[Any, float]) -> List[dict]:
        records: List[dict] = []
        for key, qty in (shipment_plan or {}).items():
            source = destination = mode = None
            period = 1
            if isinstance(key, tuple):
                if len(key) == 4:
                    source, destination, period, mode = key
                elif len(key) == 3:
                    source, destination, period = key
                elif len(key) == 2:
                    source, destination = key
            records.append({"source": source, "destination": destination, "period": int(period), "mode": mode, "quantity": self._safe_float(qty)})
        return records

    def _shipment_balance(self, shipments: List[dict]) -> Tuple[Dict[Tuple[Any, int], float], Dict[Tuple[Any, int], float]]:
        inbound: Dict[Tuple[Any, int], float] = {}
        outbound: Dict[Tuple[Any, int], float] = {}
        for shp in shipments:
            key_in = (shp.get("destination"), shp.get("period"))
            key_out = (shp.get("source"), shp.get("period"))
            if key_in[0] is not None:
                inbound[key_in] = inbound.get(key_in, 0.0) + shp.get("quantity", 0.0)
            if key_out[0] is not None:
                outbound[key_out] = outbound.get(key_out, 0.0) + shp.get("quantity", 0.0)
        return inbound, outbound

    def _match_route(self, routes: List[dict], shipment: dict) -> Optional[dict]:
        candidates = [r for r in routes or [] if r.get("source") == shipment.get("source") and r.get("destination") == shipment.get("destination")]
        if not candidates:
            return None
        if shipment.get("mode"):
            for route in candidates:
                if route.get("mode") == shipment.get("mode"):
                    return route
        return candidates[0]

    def _all_plants(self, dataset: CanonicalDataset, shipments: List[dict]) -> List[Any]:
        ids = {plant.get("id") for plant in (dataset.plants or [])}
        ids.update(dataset.inventory.keys())
        ids.update(dataset.demand.keys())
        for shp in shipments:
            ids.add(shp.get("source"))
            ids.add(shp.get("destination"))
        return [pid for pid in ids if pid is not None]

    def _period_count(self, plant_id: Any, dataset: CanonicalDataset) -> int:
        demand = dataset.demand.get(plant_id)
        if demand:
            return max(len(demand), dataset.periods)
        return dataset.periods or 1

    def _demand_for(self, demand_map: Dict[Any, List[float]], plant_id: Any, period: int) -> float:
        series = demand_map.get(plant_id, [])
        if period - 1 < len(series):
            return self._safe_float(series[period - 1])
        return 0.0

    def _cost_share(self, value: Optional[float], total: float) -> Optional[float]:
        if total in (None, 0) or value is None:
            return None
        return round((float(value) / float(total)) * 100.0, 4)

    def _shortage_units(self, parsed: ParsedSolution) -> float:
        if parsed.kpis.get("shortage_units") is not None:
            return float(parsed.kpis.get("shortage_units"))
        if parsed.shortage_plan:
            return float(sum(parsed.shortage_plan.values()))
        return 0.0
