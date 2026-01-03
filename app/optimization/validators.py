"""Validation helpers for optimization inputs."""
from __future__ import annotations

from typing import Iterable

from .exceptions import ValidationError
from .data_mapper import CanonicalDataset


class DatasetValidator:
    """Runs guardrails before model building."""

    def validate(self, dataset: CanonicalDataset) -> None:
        self._require(dataset.plants, "No plants available for optimization")
        self._require(dataset.routes, "Transport network is empty")
        self._require_any_consumers(dataset)
        self._require_any_producers(dataset)
        self._check_connectivity(dataset)
        self._check_demand(dataset)
        self._check_inventory_bounds(dataset)

    def _require(self, items: Iterable, message: str) -> None:
        if not list(items):
            raise ValidationError(message)

    def _require_any_consumers(self, dataset: CanonicalDataset) -> None:
        demand_present = any(sum(per_period) > 0 for per_period in dataset.demand.values())
        gu_with_capacity = any(
            p.get("type") == "GU" and p.get("consumption_capacity", 0) > 0 for p in dataset.plants
        )

        # Allow any plant with demand (from demand map) or a GU with consumption capacity
        if demand_present or gu_with_capacity:
            return

        raise ValidationError("At least one demand point (e.g., a grinding unit) is required")

    def _require_any_producers(self, dataset: CanonicalDataset) -> None:
        if not any(p.get("type") == "IU" and p.get("production_capacity", 0) > 0 for p in dataset.plants):
            raise ValidationError("At least one integrated unit with production capacity is required")

    def _check_connectivity(self, dataset: CanonicalDataset) -> None:
        plant_ids = {p["id"] for p in dataset.plants}
        for route in dataset.routes:
            if route["source"] not in plant_ids or route["destination"] not in plant_ids:
                raise ValidationError("Transport routes reference unknown plants")

    def _check_demand(self, dataset: CanonicalDataset) -> None:
        periods = dataset.periods
        for plant_id, per_period in dataset.demand.items():
            if any(val < 0 for val in per_period):
                raise ValidationError(f"Negative demand detected for plant {plant_id}")
            if len(per_period) < periods:
                raise ValidationError(f"Insufficient demand periods for plant {plant_id}")

    def _check_inventory_bounds(self, dataset: CanonicalDataset) -> None:
        for plant in dataset.plants:
            pid = plant.get("id")
            max_cap = float(plant.get("max_inventory_capacity", 0) or 0)
            init = float(dataset.inventory.get(pid, 0.0))
            if max_cap and init > max_cap + 1e-6:
                raise ValidationError(f"Initial inventory exceeds capacity for plant {pid}")
            safety = float(dataset.safety_stock.get(pid, 0.0))
            if max_cap and safety > max_cap + 1e-6:
                raise ValidationError(f"Safety stock exceeds capacity for plant {pid}")
