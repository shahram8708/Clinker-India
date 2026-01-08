"""Scenario-based stochastic helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .data_mapper import CanonicalDataset


@dataclass
class ScenarioSample:
    demand_multiplier: float = 1.0
    probability: float = 1.0


def generate_scenarios(dataset: CanonicalDataset, samples: int) -> List[CanonicalDataset]:
    samples = max(samples, 1)
    probability = 1 / samples
    scenarios: List[CanonicalDataset] = []
    for idx in range(samples):
        multiplier = 1 + 0.05 * idx  # slight growth between scenarios
        adjusted = {
            plant_id: [qty * multiplier for qty in periods]
            for plant_id, periods in dataset.demand.items()
        }
        scenarios.append(
            CanonicalDataset(
                organization_id=dataset.organization_id,
                scenario_id=dataset.scenario_id,
                periods=dataset.periods,
                plants=dataset.plants,
                routes=dataset.routes,
                inventory=dataset.inventory,
                demand=adjusted,
                safety_stock=dataset.safety_stock,
                min_fulfillment=dataset.min_fulfillment,
                metadata={**dataset.metadata, "scenario_probability": probability, "sample": idx + 1},
            )
        )
    return scenarios
