"""Optimization engine package with pluggable solver adapters."""
from .engine import EngineResponse, OptimizationEngine, OptimizationRequest
from .exceptions import OptimizationError, SolverError, ValidationError
from .solver_adapter import SolverAdapter, SolverResult

__all__ = [
    "EngineResponse",
    "OptimizationEngine",
    "OptimizationRequest",
    "OptimizationError",
    "SolverError",
    "ValidationError",
    "SolverAdapter",
    "SolverResult",
]
