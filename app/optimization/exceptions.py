"""Domain-specific exceptions for the optimization engine."""


class OptimizationError(Exception):
    """Base class for engine-level failures."""


class ValidationError(OptimizationError):
    """Raised when input data fails validation."""


class SolverError(OptimizationError):
    """Raised when the solver cannot produce a viable solution."""
