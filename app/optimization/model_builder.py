"""Model builders translating canonical data into solver-ready formulations."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .data_mapper import CanonicalDataset

if TYPE_CHECKING:  # pragma: no cover - type checking only
    from .engine import OptimizationRequest


@dataclass
class ModelDefinition:
    """Solver-ready structure describing the optimization problem."""

    dataset: CanonicalDataset
    mode: str
    stochastic_formulation: str = "extensive"
    uncertainty: dict[str, float] = field(default_factory=dict)
    scenario_weights: list[float] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    allow_shortage: bool = False
    shortage_penalty: float = 0.0
    service_level_target: float | None = None
    robust_multiplier: float | None = None


class DeterministicModelBuilder:
    def build(self, dataset: CanonicalDataset, request: "OptimizationRequest") -> ModelDefinition:
        return ModelDefinition(
            dataset=dataset,
            mode="deterministic",
            stochastic_formulation="deterministic",
            metadata={"periods": dataset.periods},
            allow_shortage=bool(getattr(request, "allow_shortage", False)),
            shortage_penalty=float(getattr(request, "shortage_penalty", 0.0) or 0.0),
            service_level_target=float(request.service_level_target) if getattr(request, "service_level_target", None) is not None else None,
        )


class StochasticModelBuilder:
    def build(self, dataset: CanonicalDataset, request: "OptimizationRequest") -> ModelDefinition:
        weight = 1.0 / max(request.scenario_samples or 1, 1)
        weights = [weight] * max(request.scenario_samples or 1, 1)
        formulation = "extensive"
        return ModelDefinition(
            dataset=dataset,
            mode="stochastic",
            stochastic_formulation=formulation,
            scenario_weights=weights,
            metadata={"periods": dataset.periods, "scenario_samples": request.scenario_samples or 1},
            allow_shortage=bool(getattr(request, "allow_shortage", False)),
            shortage_penalty=float(getattr(request, "shortage_penalty", 0.0) or 0.0),
            service_level_target=float(request.service_level_target) if getattr(request, "service_level_target", None) is not None else None,
        )


class RobustModelBuilder:
    def build(self, dataset: CanonicalDataset, request: "OptimizationRequest") -> ModelDefinition:
        uncertainty = {"demand_uplift_pct": request.demand_uplift_pct or 0.0}
        stressed = {
            plant_id: [qty * (1 + uncertainty["demand_uplift_pct"]) for qty in per_period]
            for plant_id, per_period in dataset.demand.items()
        }
        robust_dataset = CanonicalDataset(
            organization_id=dataset.organization_id,
            scenario_id=dataset.scenario_id,
            periods=dataset.periods,
            plants=dataset.plants,
            routes=dataset.routes,
            inventory=dataset.inventory,
            demand=stressed,
            safety_stock=dataset.safety_stock,
            metadata={**dataset.metadata, "robust_applied": True, "robust_formulation": "minmax"},
        )
        return ModelDefinition(
            dataset=robust_dataset,
            mode="robust",
            stochastic_formulation="robust",
            uncertainty=uncertainty,
            metadata=robust_dataset.metadata,
            allow_shortage=bool(getattr(request, "allow_shortage", False)),
            shortage_penalty=float(getattr(request, "shortage_penalty", 0.0) or 0.0),
            service_level_target=float(request.service_level_target) if getattr(request, "service_level_target", None) is not None else None,
            robust_multiplier=uncertainty["demand_uplift_pct"],
        )


class ModelBuilderFactory:
    """Factory to route to the correct model builder by mode."""

    _builders = {
        "deterministic": DeterministicModelBuilder(),
        "stochastic": StochasticModelBuilder(),
        "robust": RobustModelBuilder(),
    }

    @classmethod
    def for_mode(cls, mode: str):
        builder = cls._builders.get(mode)
        if not builder:
            raise ValueError(f"Unsupported optimization mode: {mode}")
        return builder
