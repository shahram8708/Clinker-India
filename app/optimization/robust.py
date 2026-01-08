"""Helpers for robust (worst-case) optimization settings."""
from __future__ import annotations

from dataclasses import dataclass

from .data_mapper import CanonicalDataset


@dataclass
class RobustConfig:
    demand_uplift_pct: float = 0.0


def apply_robust_adjustments(dataset: CanonicalDataset, config: RobustConfig) -> CanonicalDataset:
    uplift = 1 + max(config.demand_uplift_pct, 0.0)
    stressed = {plant_id: [qty * uplift for qty in periods] for plant_id, periods in dataset.demand.items()}
    return CanonicalDataset(
        organization_id=dataset.organization_id,
        scenario_id=dataset.scenario_id,
        periods=dataset.periods,
        plants=dataset.plants,
        routes=dataset.routes,
        inventory=dataset.inventory,
        demand=stressed,
        safety_stock=dataset.safety_stock,
        min_fulfillment=dataset.min_fulfillment,
        metadata={**dataset.metadata, "robust": True, "demand_uplift_pct": config.demand_uplift_pct},
    )
