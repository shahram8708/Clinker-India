"""Scenario orchestration for stochastic and robust modes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .data_mapper import CanonicalDataset
from .uncertainty_utils import build_multiplier_grid, make_demand_scenario, normalize_probabilities, robust_stress_multipliers


@dataclass
class ScenarioPlan:
    """Concrete scenario with probability and labeled stress descriptor."""

    dataset: CanonicalDataset
    probability: float
    label: str
    stress: str
    multiplier: float


class ScenarioManager:
    """Creates scenario sets for stochastic and robust optimization modes."""

    def __init__(self, base_dataset: CanonicalDataset):
        self.base_dataset = base_dataset

    def stochastic(self, samples: int | None = None) -> List[ScenarioPlan]:
        multipliers = build_multiplier_grid(samples or 1)
        center = (len(multipliers) - 1) / 2
        weights = [1.0 / (1 + abs(i - center)) for i in range(len(multipliers))]
        probabilities = normalize_probabilities(weights)
        scenarios: List[ScenarioPlan] = []
        for idx, (multiplier, prob) in enumerate(zip(multipliers, probabilities)):
            label = f"S{idx + 1}"
            ds = make_demand_scenario(self.base_dataset, multiplier, prob, label, stress="stochastic")
            scenarios.append(ScenarioPlan(dataset=ds, probability=prob, label=label, stress="stochastic", multiplier=multiplier))
        return scenarios

    def robust(self, uplift_pct: float | None = None) -> List[ScenarioPlan]:
        multipliers = robust_stress_multipliers(uplift_pct)
        probabilities = normalize_probabilities([1.0 for _ in multipliers])
        scenarios: List[ScenarioPlan] = []
        for idx, (multiplier, prob) in enumerate(zip(multipliers, probabilities)):
            stress = "worst_case" if idx == 0 else "stress_test"
            label = f"R{idx + 1}"
            ds = make_demand_scenario(self.base_dataset, multiplier, prob, label, stress=stress)
            scenarios.append(ScenarioPlan(dataset=ds, probability=prob, label=label, stress=stress, multiplier=multiplier))
        return scenarios
