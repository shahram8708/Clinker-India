"""Uncertainty helpers for stochastic and robust optimization."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

from .data_mapper import CanonicalDataset


@dataclass
class ScenarioProfile:
    """Lightweight description of a scenario multiplier and probability."""

    multiplier: float
    probability: float
    label: str
    stress: str = "baseline"


def normalize_probabilities(weights: Iterable[float]) -> list[float]:
    total = float(sum(weights)) or 1.0
    return [float(w) / total for w in weights]


def build_multiplier_grid(samples: int) -> list[float]:
    samples = max(int(samples or 1), 1)
    base_set = [0.9, 1.0, 1.1]
    if samples <= 3:
        return base_set[:samples] if samples > 1 else [1.0]
    step = 0.05
    start = 0.85
    return [round(start + step * i, 3) for i in range(samples)]


def make_demand_scenario(dataset: CanonicalDataset, multiplier: float, probability: float, label: str, stress: str) -> CanonicalDataset:
    adjusted = {
        plant_id: [qty * multiplier for qty in periods]
        for plant_id, periods in dataset.demand.items()
    }
    return CanonicalDataset(
        organization_id=dataset.organization_id,
        scenario_id=dataset.scenario_id,
        periods=dataset.periods,
        plants=dataset.plants,
        routes=dataset.routes,
        inventory=dataset.inventory,
        demand=adjusted,
        safety_stock=dataset.safety_stock,
        min_fulfillment=dataset.min_fulfillment,
        metadata={
            **dataset.metadata,
            "scenario_probability": probability,
            "scenario_label": label,
            "demand_multiplier": multiplier,
            "stress": stress,
        },
    )


def chance_constraint_ok(service_level_pct: float, alpha: float | None) -> bool:
    if alpha is None:
        return True
    return service_level_pct >= max(min(alpha, 1.0), 0.0) * 100


def weighted_service_level(service_levels: Iterable[float], probabilities: Iterable[float]) -> float:
    levels = list(service_levels)
    probs = normalize_probabilities(list(probabilities))
    if not levels:
        return 0.0
    return round(sum(l * p for l, p in zip(levels, probs)), 2)


def robust_stress_multipliers(uplift_pct: float | None) -> List[float]:
    uplift = max(float(uplift_pct or 0.0), 0.0)
    if uplift == 0:
        return [1.0]
    base = 1.0 + uplift
    mild = 1.0 + uplift / 2 if uplift > 0 else 1.0
    return [base, mild, 1.0]

