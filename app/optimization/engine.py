"""Coordinated optimization engine pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .data_mapper import CanonicalDataset, DataMapper
from .exceptions import OptimizationError, SolverError, ValidationError
from .deterministic_solver import DeterministicSolver
from .model_builder import ModelBuilderFactory
from .robust_solver import RobustSolver
from .results_parser import ParsedSolution, ResultsParser
from .scenario_manager import ScenarioManager
from .solver_adapter import SolverAdapter, SolverResult
from .stochastic_solver import StochasticSolver
from .validators import DatasetValidator


@dataclass
class OptimizationRequest:
    organization_id: int
    scenario_id: int
    mode: str = "deterministic"
    runtime_limit: Optional[int] = None
    demand_uplift_pct: float | None = None
    scenario_samples: int | None = None
    allow_shortage: bool = False
    shortage_penalty: float = 1000.0
    service_level_target: float | None = None


@dataclass
class EngineResponse:
    solver_status: str
    solution: ParsedSolution
    diagnostics: dict
    runtime_seconds: float
    started_at: datetime
    completed_at: datetime


class OptimizationEngine:
    """Runs the full data → model → solver → results pipeline."""

    def __init__(self, session):
        self.session = session
        self.mapper = DataMapper(session)
        self.validator = DatasetValidator()
        self.parser = ResultsParser()
        self.solver = SolverAdapter()
        self.det_solver = DeterministicSolver(self.solver, self.parser)
        self.stoch_solver = StochasticSolver(self.solver, self.parser)
        self.robust_solver = RobustSolver(self.solver, self.parser)

    def run(self, request: OptimizationRequest, scenario) -> EngineResponse:
        started_at = datetime.utcnow()
        dataset = self.mapper.load_dataset(request.organization_id, scenario)
        self.validator.validate(dataset)

        builder = ModelBuilderFactory.for_mode(request.mode)
        model_def = builder.build(dataset, request)

        scenario_mgr = ScenarioManager(dataset)

        if request.mode == "stochastic":
            scenarios = scenario_mgr.stochastic(request.scenario_samples)
            solution = self.stoch_solver.solve(
                model_def,
                scenarios,
                runtime_limit=request.runtime_limit,
                service_level_target=request.service_level_target,
            )
        elif request.mode == "robust":
            scenarios = scenario_mgr.robust(request.demand_uplift_pct)
            solution = self.robust_solver.solve(model_def, scenarios, runtime_limit=request.runtime_limit)
        else:
            solution = self.det_solver.solve(model_def, runtime_limit=request.runtime_limit)

        # Always compute deterministic baseline for comparison when running uncertainty modes.
        if request.mode in {"stochastic", "robust"}:
            det_builder = ModelBuilderFactory.for_mode("deterministic")
            det_model = det_builder.build(dataset, request)
            det_solution = self.det_solver.solve(det_model, runtime_limit=request.runtime_limit)
            solution.kpis["comparison"] = {
                "deterministic": {
                    "total_cost": det_solution.total_cost,
                    "feasible": det_solution.feasible,
                    "service_level_pct": det_solution.kpis.get("service_level_pct"),
                },
                "uncertain": {
                    "total_cost": solution.total_cost,
                    "feasible": solution.feasible,
                    "service_level_pct": solution.kpis.get("service_level_pct"),
                },
            }

        completed_at = datetime.utcnow()
        diagnostics = {
            "mode": request.mode,
            "runtime_limit": request.runtime_limit,
            "demand_uplift_pct": request.demand_uplift_pct,
            "scenario_samples": request.scenario_samples,
            "allow_shortage": request.allow_shortage,
            "shortage_penalty": request.shortage_penalty,
            "service_level_target": request.service_level_target,
        }
        return EngineResponse(
            solver_status=solution.solver_status,
            solution=solution,
            diagnostics=diagnostics,
            runtime_seconds=(completed_at - started_at).total_seconds(),
            started_at=started_at,
            completed_at=completed_at,
        )
