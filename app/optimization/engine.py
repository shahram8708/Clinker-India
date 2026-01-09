"""Coordinated elastic optimization engine pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .data_mapper import CanonicalDataset, DataMapper
from .exceptions import OptimizationError, SolverError, ValidationError
from .deterministic_solver import DeterministicSolver
from .model_builder import ModelBuilderFactory
from .results_parser import ParsedSolution, ResultsParser
from .solver_adapter import SolverAdapter, SolverResult
from .validators import DatasetValidator


@dataclass
class OptimizationRequest:
    organization_id: int
    scenario_id: int
    workspace_id: int
    mode: str = "elastic"
    runtime_limit: Optional[int] = None
    demand_uplift_pct: float | None = None
    scenario_samples: int | None = None
    allow_shortage: bool | None = None
    shortage_penalty: float | None = None
    service_level_target: float | None = None
    strict_service: bool | None = None


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
        # Only elastic/deterministic path is supported now.

    def run(self, request: OptimizationRequest, scenario) -> EngineResponse:
        started_at = datetime.utcnow()
        dataset = self.mapper.load_dataset(request.organization_id, scenario, workspace_id=request.workspace_id)
        self.validator.validate(dataset)

        builder = ModelBuilderFactory.for_mode(request.mode)
        model_def = builder.build(dataset, request)
        solution = self.det_solver.solve(model_def, runtime_limit=request.runtime_limit)

        completed_at = datetime.utcnow()
        diagnostics = {
            "mode": request.mode,
            "runtime_limit": request.runtime_limit,
            "mode": "elastic",
        }
        return EngineResponse(
            solver_status=solution.solver_status,
            solution=solution,
            diagnostics=diagnostics,
            runtime_seconds=(completed_at - started_at).total_seconds(),
            started_at=started_at,
            completed_at=completed_at,
        )
