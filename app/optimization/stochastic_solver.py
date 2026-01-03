"""Scenario-based stochastic solver aggregation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .model_builder import ModelDefinition
from .results_parser import ParsedSolution, ResultsParser
from .scenario_manager import ScenarioPlan
from .solver_adapter import SolverAdapter, SolverResult
from .uncertainty_utils import chance_constraint_ok, weighted_service_level


@dataclass
class ScenarioOutcome:
    label: str
    probability: float
    parsed: ParsedSolution
    multiplier: float


class StochasticSolver:
    """Evaluates scenario set and returns expected-cost solution."""

    def __init__(self, solver: SolverAdapter, parser: ResultsParser):
        self.solver = solver
        self.parser = parser

    def solve(
        self,
        base_model: ModelDefinition,
        scenarios: List[ScenarioPlan],
        runtime_limit: int | None = None,
        service_level_target: float | None = None,
    ) -> ParsedSolution:
        if not scenarios:
            scenarios = [ScenarioPlan(dataset=base_model.dataset, probability=1.0, label="S1", stress="baseline", multiplier=1.0)]

        # Always solve the extensive-form stochastic program to produce a single feasible here-and-now plan.
        result = self.solver.solve(base_model, scenarios=scenarios, time_limit=runtime_limit, mode_override="stochastic")
        parsed = self.parser.parse(result, base_model.dataset)
        reliability = weighted_service_level(
            (parsed.kpis.get("service_level_pct", 0.0) for _ in scenarios),
            (s.probability for s in scenarios),
        )
        parsed.kpis["reliability_score"] = reliability
        parsed.kpis["scenario_count"] = len(scenarios)
        parsed.kpis["chance_constraint_satisfied"] = chance_constraint_ok(reliability, service_level_target)
        parsed.kpis["formulation"] = "extensive"
        parsed.kpis["scenario_outcomes"] = [
            {
                "label": s.label,
                "probability": s.probability,
                "multiplier": s.multiplier,
            }
            for s in scenarios
        ]
        return parsed
