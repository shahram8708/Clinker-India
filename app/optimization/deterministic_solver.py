"""Deterministic optimization solver wrapper."""
from __future__ import annotations

from .model_builder import ModelDefinition
from .results_parser import ParsedSolution, ResultsParser
from .solver_adapter import SolverAdapter


class DeterministicSolver:
    """Runs deterministic optimization using the configured solver backend."""

    def __init__(self, solver: SolverAdapter, parser: ResultsParser):
        self.solver = solver
        self.parser = parser

    def solve(self, model_def: ModelDefinition, runtime_limit: int | None = None) -> ParsedSolution:
        solver_result = self.solver.solve(model_def, time_limit=runtime_limit)
        return self.parser.parse(solver_result, model_def.dataset)
