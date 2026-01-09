"""Model builder translating canonical data into the elastic MILP definition."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .data_mapper import CanonicalDataset

if TYPE_CHECKING:  # pragma: no cover - type checking only
    from .engine import OptimizationRequest


@dataclass
class ModelDefinition:
    """Solver-ready structure describing the elastic optimization problem."""

    dataset: CanonicalDataset
    mode: str = "elastic"
    metadata: dict[str, Any] = field(default_factory=dict)
    penalty_min_fulfillment: float = 1_000_000.0
    penalty_min_stock: float = 10_000.0
    penalty_max_stock: float = 1_000.0
    penalty_service_level: float = 1_000_000.0
    allow_shortage: bool = False
    shortage_penalty: float = 0.0
    service_level_target: float | None = None
    strict_service: bool = True


class ElasticModelBuilder:
    def build(self, dataset: CanonicalDataset, request: "OptimizationRequest") -> ModelDefinition:
        return ModelDefinition(
            dataset=dataset,
            mode="elastic",
            metadata={"periods": dataset.periods},
            penalty_min_fulfillment=1_000_000.0,
            penalty_min_stock=10_000.0,
            penalty_max_stock=1_000.0,
            penalty_service_level=1_000_000.0,
            allow_shortage=bool(request.allow_shortage),
            shortage_penalty=float(request.shortage_penalty or 0.0),
            service_level_target=request.service_level_target,
            strict_service=bool(request.strict_service if request.strict_service is not None else True),
        )


class ModelBuilderFactory:
    """Factory retained for compatibility; only elastic is supported."""

    _builders = {
        "elastic": ElasticModelBuilder(),
        "deterministic": ElasticModelBuilder(),  # backwards-compatible alias
    }

    @classmethod
    def for_mode(cls, mode: str):
        builder = cls._builders.get(mode)
        if not builder:
            raise ValueError(f"Unsupported optimization mode: {mode}")
        return builder
