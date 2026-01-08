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
        # Enhanced validations for new dataset structure
        self._check_batch_multipliers(dataset)
        self._check_period_specific_params(dataset)
        self._check_iugu_constraints(dataset)
        self._check_mathematical_feasibility(dataset)

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
    def _check_batch_multipliers(self, dataset: CanonicalDataset) -> None:
        """Validate batch multipliers are positive and consistent with SBQ constraints."""
        for route in dataset.routes:
            route_id = route.get("id")
            multiplier = dataset.batch_multipliers.get(route_id, 1.0)
            if multiplier <= 0:
                raise ValidationError(f"Route {route_id} has invalid batch multiplier {multiplier} (must be > 0)")
            
            # Check that multiplier × max_trips can meet minimum batch quantity
            sbq = float(route.get("min_batch_quantity", 0.0) or 0.0)
            max_trips = route.get("max_trips_per_period")
            if sbq > 0 and max_trips is not None and max_trips > 0:
                # With multiplier, flow = trips × multiplier
                # We need: trips × multiplier ≥ sbq when trips > 0
                # Minimum feasible trips: ceil(sbq / multiplier)
                min_trips_needed = sbq / multiplier if multiplier > 0 else float("inf")
                if min_trips_needed > max_trips + 1e-6:
                    raise ValidationError(
                        f"Route {route_id}: Cannot satisfy SBQ {sbq} with multiplier {multiplier} "
                        f"and max_trips {max_trips} (would need {min_trips_needed:.2f} trips)"
                    )
    
    def _check_period_specific_params(self, dataset: CanonicalDataset) -> None:
        """Validate period-specific parameters have correct dimensions and non-negative values."""
        periods = dataset.periods
        
        # Check freight costs
        for route_id, costs in dataset.freight_costs.items():
            if len(costs) != periods:
                raise ValidationError(f"Route {route_id} freight costs have {len(costs)} periods, expected {periods}")
            if any(c < 0 for c in costs):
                raise ValidationError(f"Route {route_id} has negative freight costs")
        
        # Check handling costs
        for route_id, costs in dataset.handling_costs.items():
            if len(costs) != periods:
                raise ValidationError(f"Route {route_id} handling costs have {len(costs)} periods, expected {periods}")
            if any(c < 0 for c in costs):
                raise ValidationError(f"Route {route_id} has negative handling costs")
        
        # Check period capacities
        for plant_id, caps in dataset.period_capacities.items():
            if len(caps) != periods:
                raise ValidationError(f"Plant {plant_id} capacities have {len(caps)} periods, expected {periods}")
            if any(c < 0 for c in caps):
                raise ValidationError(f"Plant {plant_id} has negative capacity in some period")
    
    def _check_iugu_constraints(self, dataset: CanonicalDataset) -> None:
        """Validate IUGU constraints reference valid plants and have sensible bounds."""
        plant_ids = {p["id"] for p in dataset.plants}
        plant_types = {p["id"]: p.get("type") for p in dataset.plants}
        
        for constraint_key, (min_val, max_val) in dataset.iugu_constraints.items():
            # Parse constraint key like "IU1_GU2" or "1_5" (plant IDs)
            parts = str(constraint_key).split("_")
            if len(parts) != 2:
                raise ValidationError(f"Invalid IUGU constraint key format: {constraint_key}")
            
            try:
                # Try to extract plant IDs (may need mapping from codes to IDs)
                # For now, assume numeric IDs
                source_id = int(parts[0].replace("IU", "").replace("GU", ""))
                dest_id = int(parts[1].replace("IU", "").replace("GU", ""))
            except ValueError:
                # Skip validation if we can't parse IDs (may be symbolic codes)
                continue
            
            if source_id in plant_ids and dest_id in plant_ids:
                # Validate types if possible
                if plant_types.get(source_id) not in ["IU", None]:
                    raise ValidationError(f"IUGU constraint {constraint_key}: source {source_id} is not an IU")
                if plant_types.get(dest_id) not in ["GU", "IU", None]:  # Allow IU-to-IU as well
                    raise ValidationError(f"IUGU constraint {constraint_key}: destination {dest_id} is not a GU/IU")
            
            # Check bounds are sensible
            if min_val < 0:
                raise ValidationError(f"IUGU constraint {constraint_key} has negative minimum {min_val}")
            if max_val < min_val:
                raise ValidationError(f"IUGU constraint {constraint_key} has max {max_val} < min {min_val}")
    
    def _check_mathematical_feasibility(self, dataset: CanonicalDataset) -> None:
        """Perform mathematical pre-checks for supply-demand balance and constraint consistency."""
        periods = dataset.periods
        plant_lookup = {p["id"]: p for p in dataset.plants}
        iu_ids = [pid for pid, plant in plant_lookup.items() if plant.get("type") == "IU"]
        
        # Aggregate supply capacity across all periods
        total_production_capacity = 0.0
        for plant_id in iu_ids:
            if plant_id in dataset.period_capacities:
                total_production_capacity += sum(dataset.period_capacities[plant_id])
            else:
                cap = float(plant_lookup[plant_id].get("production_capacity", 0.0))
                total_production_capacity += cap * periods
        
        # Initial inventory
        total_initial_inventory = sum(dataset.inventory.values())
        
        # Total demand across all periods
        total_demand = sum(sum(per_period) for per_period in dataset.demand.values())
        
        # Check supply vs demand (warning level, not blocking unless strict mode)
        total_supply_available = total_production_capacity + total_initial_inventory
        if total_supply_available < total_demand - 1e-6:
            # This is a warning; solver may need shortage variables
            import warnings
            warnings.warn(
                f"Total supply ({total_supply_available:.2f}) may be insufficient for total demand ({total_demand:.2f}). "
                "Consider enabling shortage variables or adjusting capacities.",
                UserWarning,
            )
        
        # Check connectivity: each GU with demand should have at least one inbound route
        routes_by_dest = {}
        for route in dataset.routes:
            routes_by_dest.setdefault(route["destination"], []).append(route)
        
        for plant_id, demand_series in dataset.demand.items():
            if any(d > 0 for d in demand_series):
                plant_type = plant_lookup.get(plant_id, {}).get("type")
                if plant_type == "GU" and not routes_by_dest.get(plant_id):
                    raise ValidationError(f"GU {plant_id} has demand but no inbound routes")
        
        # Check route capacity can handle peak demand per period (rough heuristic)
        for t in range(1, periods + 1):
            period_demand = sum(
                dataset.demand.get(pid, [0.0] * periods)[t - 1] for pid in plant_lookup
            )
            # Total transport capacity available in this period
            period_transport_capacity = 0.0
            for route in dataset.routes:
                route_id = route["id"]
                max_trips = route.get("max_trips_per_period") or 0
                multiplier = dataset.batch_multipliers.get(route_id, 1.0)
                # Max flow per route: max_trips × multiplier
                period_transport_capacity += max_trips * multiplier
            
            if period_transport_capacity < period_demand and period_transport_capacity > 0:
                import warnings
                warnings.warn(
                    f"Period {t}: Transport capacity ({period_transport_capacity:.2f}) may be less than demand ({period_demand:.2f})",
                    UserWarning,
                )