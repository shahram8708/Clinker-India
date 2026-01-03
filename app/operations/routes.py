"""Supply chain planning + operations blueprint."""
import csv
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from typing import Iterable

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import (
    ActivityLog,
    Inventory,
    Notification,
    OptimizationJob,
    OptimizationResult,
    Organization,
    PlanningScenario,
    Plant,
    TransportRoute,
)
from ..tenant.utils import active_org_id, admin_required, get_tenant_record_or_404, tenant_required
from .forms import (
    ActivityLogFilterForm,
    CsvExportForm,
    InventoryFilterForm,
    InventoryUpdateForm,
    NotificationInboxFilterForm,
    OptimizationRunForm,
    PdfReportForm,
    PlantFilterForm,
    PlantForm,
    ScenarioForm,
    TransportFilterForm,
    TransportRouteForm,
)
from ..optimization.engine import OptimizationEngine, OptimizationRequest
from ..optimization.exceptions import OptimizationError, ValidationError

ops_bp = Blueprint("ops", __name__, template_folder="../templates/operations")

SAFE_EXPORT_DATASETS = {"plants", "transport_routes", "inventory", "scenarios"}


def _org_id() -> int:
    org_id = active_org_id()
    if org_id is None:
        abort(403)
    if current_user.is_authenticated:
        if getattr(current_user, "organization_id", None) not in (None, org_id):
            abort(403)
    return org_id


def _safe_org_slug(org: Organization | None) -> str:
    if org is None or not org.name:
        return "org"
    return org.name.lower().replace(" ", "-")


def _plant_choices(org_id: int) -> list[tuple[int, str]]:
    plants = Plant.for_org(org_id).order_by(Plant.plant_name).all()
    return [(p.id, f"{p.plant_name} ({'IU' if p.plant_type == 'IU' else 'GU'})") for p in plants]


def _scenario_choices(org_id: int) -> list[tuple[int, str]]:
    scenarios = PlanningScenario.for_org(org_id).order_by(PlanningScenario.created_at.desc()).all()
    return [(s.id, f"{s.scenario_name} ({s.periods}p)") for s in scenarios]


def _estimate_cost(org_id: int, periods: int) -> float:
    active_routes = TransportRoute.for_org(org_id).filter_by(status="active")
    avg_cost = active_routes.with_entities(func.avg(TransportRoute.cost_per_trip)).scalar() or 0
    route_count = active_routes.count()
    return float(avg_cost) * float(periods) * float(route_count or 1)


def _log_activity(
    org_id: int,
    user_id: int | None,
    action_type: str,
    description: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    severity: str = "info",
    details: dict | None = None,
) -> None:
    entry = ActivityLog(
        organization_id=org_id,
        user_id=user_id,
        action_type=action_type,
        action_description=description,
        entity_type=entity_type,
        entity_id=entity_id,
        severity=severity,
        details=details or {},
    )
    db.session.add(entry)


def _notify(
    org_id: int,
    title: str,
    body: str,
    severity: str = "info",
    user_id: int | None = None,
) -> None:
    note = Notification(
        organization_id=org_id,
        user_id=user_id,
        title=title,
        body=body,
        severity=severity,
    )
    db.session.add(note)


def _csv_response(filename: str, headers: Iterable[str], rows: Iterable[Iterable]):
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(headers))
    for row in rows:
        writer.writerow(row)

    mem = BytesIO(buffer.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(
        mem,
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )


@ops_bp.route("/network")
@login_required
@tenant_required
def network():
    org_id = _org_id()

    export_form = CsvExportForm()
    pdf_form = PdfReportForm()
    notification_filter_form = NotificationInboxFilterForm()
    plant_filters_form = PlantFilterForm(request.args, meta={"csrf": False})
    transport_filters_form = TransportFilterForm(request.args, meta={"csrf": False})
    inventory_filters_form = InventoryFilterForm(request.args, meta={"csrf": False})
    optimization_form = OptimizationRunForm()

    plant_form = PlantForm()
    route_form = TransportRouteForm()
    inventory_form = InventoryUpdateForm()
    scenario_form = ScenarioForm()
    optimization_form.scenario_id.choices = _scenario_choices(org_id)

    plant_form.plant_type.data = plant_form.plant_type.data or "IU"
    plant_choices = _plant_choices(org_id)
    route_form.source_plant_id.choices = plant_choices
    route_form.destination_plant_id.choices = plant_choices
    inventory_form.plant_id.choices = plant_choices

    plant_query = Plant.for_org(org_id)
    if plant_filters_form.validate():
        if plant_filters_form.plant_type.data in {"IU", "GU"}:
            plant_query = plant_query.filter_by(plant_type=plant_filters_form.plant_type.data)
        if plant_filters_form.status.data in {"active", "disabled"}:
            plant_query = plant_query.filter_by(status=plant_filters_form.status.data)
        if plant_filters_form.region.data:
            like = f"%{plant_filters_form.region.data.lower()}%"
            plant_query = plant_query.filter(func.lower(Plant.location).like(like))
        if plant_filters_form.search.data:
            like = f"%{plant_filters_form.search.data.lower()}%"
            plant_query = plant_query.filter(func.lower(Plant.plant_name).like(like))

    page = max(int(request.args.get("page", 1) or 1), 1)
    plants = plant_query.order_by(Plant.created_at.desc()).paginate(page=page, per_page=10, error_out=False)

    route_query = TransportRoute.for_org(org_id)
    if transport_filters_form.validate():
        if transport_filters_form.mode.data in {"Road", "Rail", "Sea"}:
            route_query = route_query.filter_by(mode=transport_filters_form.mode.data)
        if transport_filters_form.status.data in {"active", "disabled"}:
            route_query = route_query.filter_by(status=transport_filters_form.status.data)
        if transport_filters_form.min_cost.data is not None:
            route_query = route_query.filter(TransportRoute.cost_per_trip >= transport_filters_form.min_cost.data)
        if transport_filters_form.max_cost.data is not None:
            route_query = route_query.filter(TransportRoute.cost_per_trip <= transport_filters_form.max_cost.data)
        if transport_filters_form.active_only.data:
            route_query = route_query.filter_by(status="active")
    routes = route_query.order_by(TransportRoute.created_at.desc()).all()

    inventories = (
        Inventory.for_org(org_id)
        .join(Plant)
        .order_by(Plant.plant_name)
        .all()
    )
    if inventory_filters_form.validate():
        if inventory_filters_form.below_safety_only.data:
            inventories = [inv for inv in inventories if inv.below_safety]
        if inventory_filters_form.critical_only.data:
            inventories = [inv for inv in inventories if inv.below_safety and float(inv.current_inventory) == 0]
        if inventory_filters_form.sort_by.data == "level_high":
            inventories = sorted(inventories, key=lambda inv: float(inv.current_inventory), reverse=True)
        elif inventory_filters_form.sort_by.data == "level_low":
            inventories = sorted(inventories, key=lambda inv: float(inv.current_inventory))
    inventory_alerts = [inv for inv in inventories if inv.below_safety]

    scenarios = (
        PlanningScenario.for_org(org_id)
        .order_by(PlanningScenario.created_at.desc())
        .all()
    )

    recent_jobs = (
        OptimizationJob.for_org(org_id)
        .order_by(OptimizationJob.id.desc())
        .limit(5)
        .all()
    )

    inventory_edit_forms = {}
    for inv in inventories:
        inv_form = InventoryUpdateForm(obj=inv)
        inv_form.plant_id.choices = plant_choices
        inv_form.plant_id.data = inv.plant_id
        inventory_edit_forms[inv.id] = inv_form

    plant_edit_forms = {}
    for plant in plants.items:
        plant_form_instance = PlantForm(obj=plant)
        plant_edit_forms[plant.id] = plant_form_instance

    route_edit_forms = {}
    for route in routes:
        route_form_instance = TransportRouteForm(obj=route)
        route_form_instance.source_plant_id.choices = plant_choices
        route_form_instance.destination_plant_id.choices = plant_choices
        route_form_instance.source_plant_id.data = route.source_plant_id
        route_form_instance.destination_plant_id.data = route.destination_plant_id
        route_edit_forms[route.id] = route_form_instance

    scenario_edit_forms = {}
    for scenario in scenarios:
        scenario_form_instance = ScenarioForm(obj=scenario)
        scenario_edit_forms[scenario.id] = scenario_form_instance

    metrics = {
        "total_plants": Plant.for_org(org_id).count(),
        "iu_count": Plant.for_org(org_id).filter_by(plant_type="IU").count(),
        "gu_count": Plant.for_org(org_id).filter_by(plant_type="GU").count(),
        "active_routes": TransportRoute.for_org(org_id).filter_by(status="active").count(),
        "inventory_alerts": sum(1 for inv in inventories if inv.below_safety),
        "safe_inventory": sum(1 for inv in inventories if not inv.below_safety),
        "total_inventory": len(inventories),
    }

    notifications = (
        Notification.for_org(org_id)
        .order_by(Notification.created_at.desc())
        .limit(8)
        .all()
    )

    return render_template(
        "operations/network.html",
        plant_form=plant_form,
        route_form=route_form,
        inventory_form=inventory_form,
        scenario_form=scenario_form,
        optimization_form=optimization_form,
        export_form=export_form,
        pdf_form=pdf_form,
        notification_filter_form=notification_filter_form,
        plant_filters_form=plant_filters_form,
        transport_filters_form=transport_filters_form,
        inventory_filters_form=inventory_filters_form,
        plants=plants,
        routes=routes,
        inventories=inventories,
        inventory_alerts=inventory_alerts,
        scenarios=scenarios,
        recent_jobs=recent_jobs,
        metrics=metrics,
        inventory_edit_forms=inventory_edit_forms,
        plant_edit_forms=plant_edit_forms,
        route_edit_forms=route_edit_forms,
        scenario_edit_forms=scenario_edit_forms,
        filters={
            "type": plant_filters_form.plant_type.data,
            "status": plant_filters_form.status.data,
            "q": plant_filters_form.search.data,
            "region": plant_filters_form.region.data,
        },
        notifications=notifications,
    )


@ops_bp.route("/plants", methods=["POST"])
@login_required
@tenant_required
@admin_required
def create_plant():
    org_id = _org_id()
    form = PlantForm()
    if form.validate_on_submit():
        existing = Plant.for_org(org_id).filter_by(plant_name=form.plant_name.data.strip()).first()
        if existing:
            flash("A plant with that name already exists for this organization.", "warning")
            return redirect(url_for("ops.network", _anchor="plants"))

        plant = Plant(
            organization_id=org_id,
            plant_name=form.plant_name.data.strip(),
            plant_type=form.plant_type.data,
            location=form.location.data.strip(),
            production_capacity=form.production_capacity.data,
            consumption_capacity=form.consumption_capacity.data,
            max_inventory_capacity=form.max_inventory_capacity.data,
            safety_stock_level=form.safety_stock_level.data,
            status=form.status.data,
        )
        db.session.add(plant)
        db.session.flush()
        _log_activity(
            org_id,
            current_user.id,
            "plant_created",
            f"Plant {plant.plant_name} created",
            entity_type="plant",
            entity_id=plant.id,
            details={"status": plant.status, "type": plant.plant_type},
        )
        _notify(org_id, "Plant added", f"{plant.plant_name} registered with status {plant.status}.")
        db.session.commit()
        flash("Plant created and capacity registered.", "success")
    else:
        flash("Please correct the plant details and try again.", "danger")
    return redirect(url_for("ops.network", _anchor="plants"))


@ops_bp.route("/plants/<int:plant_id>/edit", methods=["POST"])
@login_required
@tenant_required
@admin_required
def edit_plant(plant_id: int):
    org_id = _org_id()
    plant = get_tenant_record_or_404(Plant, plant_id)
    form = PlantForm()

    if form.validate_on_submit():
        plant.plant_name = form.plant_name.data.strip()
        plant.plant_type = form.plant_type.data
        plant.location = form.location.data.strip()
        plant.production_capacity = form.production_capacity.data
        plant.consumption_capacity = form.consumption_capacity.data
        plant.max_inventory_capacity = form.max_inventory_capacity.data
        plant.safety_stock_level = form.safety_stock_level.data
        plant.status = form.status.data

        try:
            db.session.add(plant)
            db.session.flush()
            _log_activity(
                org_id,
                current_user.id,
                "plant_updated",
                f"Plant {plant.plant_name} updated",
                entity_type="plant",
                entity_id=plant.id,
                details={"status": plant.status, "type": plant.plant_type},
            )
            db.session.commit()
            flash("Plant updated.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("A plant with that name already exists for this organization.", "warning")
    else:
        flash("Please correct the plant details and try again.", "danger")

    return redirect(url_for("ops.network", _anchor="plants"))


@ops_bp.route("/plants/<int:plant_id>/status", methods=["POST"])
@login_required
@tenant_required
@admin_required
def update_plant_status(plant_id: int):
    plant = get_tenant_record_or_404(Plant, plant_id)
    desired = request.form.get("status")
    if desired not in {"active", "disabled"}:
        abort(400)
    plant.status = desired
    _log_activity(
        plant.organization_id,
        current_user.id,
        "plant_status_updated",
        f"Plant {plant.plant_name} marked {desired}",
        entity_type="plant",
        entity_id=plant.id,
        details={"status": desired},
    )
    db.session.commit()
    flash("Plant status updated.", "success")
    return redirect(url_for("ops.network", _anchor="plants"))


@ops_bp.route("/routes", methods=["POST"])
@login_required
@tenant_required
@admin_required
def create_route():
    org_id = _org_id()
    form = TransportRouteForm()
    form.source_plant_id.choices = _plant_choices(org_id)
    form.destination_plant_id.choices = form.source_plant_id.choices

    if form.validate_on_submit():
        source = Plant.for_org(org_id).filter_by(id=form.source_plant_id.data).first()
        destination = Plant.for_org(org_id).filter_by(id=form.destination_plant_id.data).first()
        if not source or not destination:
            abort(404)

        route = TransportRoute(
            organization_id=org_id,
            source_plant=source,
            destination_plant=destination,
            mode=form.mode.data,
            trip_capacity=form.trip_capacity.data,
            min_batch_quantity=form.min_batch_quantity.data,
            max_trips_per_period=form.max_trips_per_period.data,
            cost_per_trip=form.cost_per_trip.data,
            status=form.status.data,
        )

        try:
            route.validate_org_alignment()
            db.session.add(route)
            db.session.flush()
            _log_activity(
                org_id,
                current_user.id,
                "transport_created",
                f"Route {source.plant_name} -> {destination.plant_name} via {route.mode}",
                entity_type="route",
                entity_id=route.id,
                details={"mode": route.mode, "status": route.status},
            )
            db.session.commit()
            flash("Transport route created.", "success")
        except ValueError as exc:  # pragma: no cover - validation path
            db.session.rollback()
            flash(str(exc), "danger")
    else:
        flash("Please fix the transport route details and try again.", "danger")

    return redirect(url_for("ops.network", _anchor="routes"))


@ops_bp.route("/routes/<int:route_id>/edit", methods=["POST"])
@login_required
@tenant_required
@admin_required
def edit_route(route_id: int):
    org_id = _org_id()
    route = get_tenant_record_or_404(TransportRoute, route_id)
    form = TransportRouteForm()
    form.source_plant_id.choices = _plant_choices(org_id)
    form.destination_plant_id.choices = form.source_plant_id.choices

    if form.validate_on_submit():
        source = Plant.for_org(org_id).filter_by(id=form.source_plant_id.data).first()
        destination = Plant.for_org(org_id).filter_by(id=form.destination_plant_id.data).first()
        if not source or not destination:
            abort(404)

        route.source_plant = source
        route.destination_plant = destination
        route.mode = form.mode.data
        route.trip_capacity = form.trip_capacity.data
        route.min_batch_quantity = form.min_batch_quantity.data
        route.max_trips_per_period = form.max_trips_per_period.data
        route.cost_per_trip = form.cost_per_trip.data
        route.status = form.status.data

        try:
            route.validate_org_alignment()
            db.session.add(route)
            db.session.flush()
            _log_activity(
                org_id,
                current_user.id,
                "transport_updated",
                f"Route {route.source_plant.plant_name} -> {route.destination_plant.plant_name} updated",
                entity_type="route",
                entity_id=route.id,
                details={"mode": route.mode, "status": route.status},
            )
            db.session.commit()
            flash("Transport route updated.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        except IntegrityError:
            db.session.rollback()
            flash("A similar route already exists for this organization.", "warning")
    else:
        flash("Please fix the transport route details and try again.", "danger")

    return redirect(url_for("ops.network", _anchor="routes"))


@ops_bp.route("/routes/<int:route_id>/status", methods=["POST"])
@login_required
@tenant_required
@admin_required
def update_route_status(route_id: int):
    route = get_tenant_record_or_404(TransportRoute, route_id)
    desired = request.form.get("status")
    if desired not in {"active", "disabled"}:
        abort(400)
    route.status = desired
    _log_activity(
        route.organization_id,
        current_user.id,
        "transport_status_updated",
        f"Route {route.id} marked {desired}",
        entity_type="route",
        entity_id=route.id,
        details={"status": desired},
    )
    db.session.commit()
    flash("Route status updated.", "success")
    return redirect(url_for("ops.network", _anchor="routes"))


@ops_bp.route("/inventory", methods=["POST"])
@login_required
@tenant_required
def update_inventory():
    org_id = _org_id()
    form = InventoryUpdateForm()
    form.plant_id.choices = _plant_choices(org_id)

    if form.validate_on_submit():
        plant = Plant.for_org(org_id).filter_by(id=form.plant_id.data).first()
        if not plant or plant.status != "active":
            flash("Inventory can only be updated for active plants.", "warning")
            return redirect(url_for("ops.network", _anchor="inventory"))

        inventory = Inventory.for_org(org_id).filter_by(plant_id=plant.id).first()
        if inventory is None:
            inventory = Inventory(organization_id=org_id, plant_id=plant.id)

        inventory.current_inventory = Decimal(form.current_inventory.data)
        inventory.last_updated = datetime.utcnow()

        try:
            inventory.apply_bounds(plant)
            db.session.add(inventory)
            db.session.flush()
            _log_activity(
                org_id,
                current_user.id,
                "inventory_updated",
                f"Inventory for {plant.plant_name} set to {inventory.current_inventory}",
                entity_type="inventory",
                entity_id=inventory.id,
                details={"utilization": inventory.utilization_pct},
            )
            if inventory.below_safety:
                _notify(
                    org_id,
                    "Inventory below safety",
                    f"{plant.plant_name} is below safety stock threshold.",
                    severity="warning",
                )
            db.session.commit()
            flash("Inventory updated.", "success")
        except ValueError as exc:  # pragma: no cover - validation path
            db.session.rollback()
            flash(str(exc), "danger")
    else:
        flash("Please provide a valid inventory value.", "danger")

    return redirect(url_for("ops.network", _anchor="inventory"))


@ops_bp.route("/scenarios", methods=["POST"])
@login_required
@tenant_required
def create_scenario():
    org_id = _org_id()
    form = ScenarioForm()
    if form.validate_on_submit():
        existing = PlanningScenario.for_org(org_id).filter_by(scenario_name=form.scenario_name.data.strip()).first()
        if existing:
            flash("A scenario with that name already exists.", "warning")
            return redirect(url_for("ops.network", _anchor="scenarios"))

        desired_status = form.status.data
        scenario = PlanningScenario(
            organization_id=org_id,
            scenario_name=form.scenario_name.data.strip(),
            periods=form.periods.data,
            status=desired_status,
        )

        if desired_status in {"executed", "completed"}:
            scenario.mark_executed(_estimate_cost(org_id, scenario.periods), {"periods": scenario.periods})
            if desired_status == "completed":
                scenario.mark_completed()

        db.session.add(scenario)
        db.session.flush()
        _log_activity(
            org_id,
            current_user.id,
            "scenario_created",
            f"Scenario {scenario.scenario_name} created with status {scenario.status}",
            entity_type="scenario",
            entity_id=scenario.id,
            details={"status": scenario.status, "periods": scenario.periods},
        )
        db.session.commit()
        flash("Scenario created.", "success")
    else:
        flash("Please review the scenario fields.", "danger")

    return redirect(url_for("ops.network", _anchor="scenarios"))


@ops_bp.route("/scenarios/<int:scenario_id>/edit", methods=["POST"])
@login_required
@tenant_required
def edit_scenario(scenario_id: int):
    org_id = _org_id()
    scenario = get_tenant_record_or_404(PlanningScenario, scenario_id)
    form = ScenarioForm()

    if form.validate_on_submit():
        existing = (
            PlanningScenario.for_org(org_id)
            .filter(PlanningScenario.id != scenario.id)
            .filter_by(scenario_name=form.scenario_name.data.strip())
            .first()
        )
        if existing:
            flash("A scenario with that name already exists.", "warning")
            return redirect(url_for("ops.network", _anchor="scenarios"))

        scenario.scenario_name = form.scenario_name.data.strip()
        scenario.periods = form.periods.data
        scenario.status = form.status.data

        try:
            db.session.add(scenario)
            db.session.flush()
            _log_activity(
                org_id,
                current_user.id,
                "scenario_updated",
                f"Scenario {scenario.scenario_name} updated",
                entity_type="scenario",
                entity_id=scenario.id,
                details={"status": scenario.status, "periods": scenario.periods},
            )
            db.session.commit()
            flash("Scenario updated.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("A scenario with that name already exists.", "warning")
    else:
        flash("Please review the scenario fields.", "danger")

    return redirect(url_for("ops.network", _anchor="scenarios"))


@ops_bp.route("/scenarios/<int:scenario_id>/status", methods=["POST"])
@login_required
@tenant_required
def update_scenario_status(scenario_id: int):
    org_id = _org_id()
    scenario = get_tenant_record_or_404(PlanningScenario, scenario_id)
    desired = request.form.get("status")
    if desired not in {"draft", "executed", "completed"}:
        abort(400)

    if desired == "draft":
        scenario.status = "draft"
        scenario.result_cost = None
        scenario.summary = {}
    elif desired == "executed":
        scenario.mark_executed(_estimate_cost(org_id, scenario.periods), {"periods": scenario.periods})
    else:
        if scenario.result_cost is None:
            scenario.mark_executed(_estimate_cost(org_id, scenario.periods), {"periods": scenario.periods})
        scenario.mark_completed()

    db.session.commit()
    _log_activity(
        org_id,
        current_user.id,
        "scenario_status_updated",
        f"Scenario {scenario.scenario_name} moved to {scenario.status}",
        entity_type="scenario",
        entity_id=scenario.id,
        details={"status": scenario.status},
    )
    flash("Scenario status updated.", "success")
    return redirect(url_for("ops.network", _anchor="scenarios"))


@ops_bp.route("/optimization/run", methods=["POST"])
@login_required
@tenant_required
def run_optimization():
    org_id = _org_id()
    form = OptimizationRunForm()
    form.scenario_id.choices = _scenario_choices(org_id)

    if not form.validate_on_submit():
        flash("Please review the optimization inputs.", "danger")
        return redirect(url_for("ops.network", _anchor="optimization"))

    scenario = get_tenant_record_or_404(PlanningScenario, form.scenario_id.data)
    allow_shortage = bool(form.allow_shortage_penalties.data and not form.strict_service.data)
    shortage_penalty = float(form.shortage_penalty.data or 1000)
    service_level_target = float(form.service_level_target.data) if form.service_level_target.data is not None else None
    if not allow_shortage:
        shortage_penalty = 0.0

    job = OptimizationJob(
        organization_id=org_id,
        scenario_id=scenario.id,
        mode=form.mode.data,
        status="pending",
        request_payload={
            "mode": form.mode.data,
            "runtime_limit": form.runtime_limit.data,
            "demand_uplift_pct": float(form.demand_uplift_pct.data or 0),
            "scenario_samples": form.scenario_samples.data,
            "allow_shortage": allow_shortage,
            "shortage_penalty": shortage_penalty,
            "service_level_target": service_level_target,
            "strict_service": bool(form.strict_service.data),
        },
    )
    db.session.add(job)
    db.session.flush()

    engine = OptimizationEngine(db.session)
    job.mark_running()
    db.session.flush()

    try:
        request_model = OptimizationRequest(
            organization_id=org_id,
            scenario_id=scenario.id,
            mode=form.mode.data,
            runtime_limit=form.runtime_limit.data,
            demand_uplift_pct=float(form.demand_uplift_pct.data or 0),
            scenario_samples=form.scenario_samples.data,
            allow_shortage=allow_shortage,
            shortage_penalty=shortage_penalty,
            service_level_target=service_level_target,
        )
        response = engine.run(request_model, scenario)
        job.mark_completed(response.solver_status, runtime_seconds=response.runtime_seconds)
        job.solver = "greedy"

        solution = response.solution

        def _stringify_keys(plan: dict) -> dict:
            if not isinstance(plan, dict):
                return plan
            converted = {}
            for key, value in plan.items():
                key_str = "|".join(map(str, key)) if isinstance(key, tuple) else str(key)
                converted[key_str] = value
            return converted

        production_plan = _stringify_keys(solution.production_plan)
        shipment_plan = _stringify_keys(solution.shipment_plan)
        inventory_plan = _stringify_keys(solution.inventory_plan)
        trips_plan = _stringify_keys(solution.trips_plan)
        shortage_plan = _stringify_keys(solution.shortage_plan)

        result = OptimizationResult(
            organization_id=org_id,
            scenario_id=scenario.id,
            job_id=job.id,
            total_cost=solution.total_cost,
            production_plan=production_plan,
            shipment_plan=shipment_plan,
            inventory_plan=inventory_plan,
            cost_breakdown=solution.cost_breakdown,
            kpis={**solution.kpis, "trips_plan": trips_plan, "shortage_plan": shortage_plan},
        )
        db.session.add(result)
        scenario.mark_executed(solution.total_cost, summary=solution.kpis)
        if form.mark_completed.data:
            scenario.mark_completed()
        db.session.commit()
        _log_activity(
            org_id,
            current_user.id,
            "optimization_completed",
            f"Optimization job {job.id} finished with status {job.status}",
            entity_type="optimization_job",
            entity_id=job.id,
            details={"mode": job.mode, "solver_status": job.solver_status},
        )
        flash("Optimization completed successfully.", "success")
    except ValidationError as exc:
        db.session.rollback()
        job.mark_failed(str(exc))
        db.session.add(job)
        db.session.commit()
        flash(str(exc), "danger")
    except OptimizationError as exc:
        current_app.logger.exception("Optimization failed: %s", exc)
        db.session.rollback()
        job.mark_failed(str(exc))
        db.session.add(job)
        db.session.commit()
        flash("Optimization failed. See activity log for details.", "danger")
    except Exception as exc:  # pragma: no cover - defensive
        current_app.logger.exception("Unexpected optimization failure: %s", exc)
        db.session.rollback()
        job.mark_failed("Unexpected error during optimization")
        db.session.add(job)
        db.session.commit()
        flash("Unexpected error during optimization run.", "danger")

    return redirect(url_for("ops.network", _anchor="optimization"))


@ops_bp.route("/exports/csv", methods=["POST"])
@login_required
@tenant_required
def export_csv():
    org_id = _org_id()
    org = Organization.query.get(org_id)
    form = CsvExportForm()
    if not form.validate_on_submit():
        flash("Please choose a valid dataset to export.", "danger")
        return redirect(url_for("ops.network"))

    dataset = form.dataset.data
    if dataset not in SAFE_EXPORT_DATASETS:
        abort(400)

    include_inactive = form.include_inactive.data
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    filename = f"{dataset}-{_safe_org_slug(org)}-{timestamp}.csv"

    if dataset == "plants":
        query = Plant.for_org(org_id)
        if not include_inactive:
            query = query.filter_by(status="active")
        plants = query.order_by(Plant.plant_name).all()
        headers = [
            "id",
            "name",
            "type",
            "location",
            "production_capacity",
            "consumption_capacity",
            "max_inventory_capacity",
            "safety_stock_level",
            "status",
            "created_at",
        ]
        rows = [
            [
                p.id,
                p.plant_name,
                p.plant_type,
                p.location,
                p.production_capacity,
                p.consumption_capacity,
                p.max_inventory_capacity,
                p.safety_stock_level,
                p.status,
                p.created_at,
            ]
            for p in plants
        ]
    elif dataset == "transport_routes":
        query = TransportRoute.for_org(org_id)
        if not include_inactive:
            query = query.filter_by(status="active")
        routes = query.order_by(TransportRoute.created_at.desc()).all()
        headers = [
            "id",
            "source_plant",
            "destination_plant",
            "mode",
            "trip_capacity",
            "min_batch_quantity",
            "max_trips_per_period",
            "cost_per_trip",
            "status",
            "created_at",
        ]
        rows = [
            [
                r.id,
                r.source_plant.plant_name if r.source_plant else r.source_plant_id,
                r.destination_plant.plant_name if r.destination_plant else r.destination_plant_id,
                r.mode,
                r.trip_capacity,
                r.min_batch_quantity,
                r.max_trips_per_period,
                r.cost_per_trip,
                r.status,
                r.created_at,
            ]
            for r in routes
        ]
    elif dataset == "inventory":
        inventories = (
            Inventory.for_org(org_id)
            .join(Plant)
            .order_by(Plant.plant_name)
            .all()
        )
        headers = [
            "plant",
            "current_inventory",
            "max_capacity",
            "safety_stock",
            "utilization_pct",
            "status",
            "last_updated",
        ]
        rows = [
            [
                inv.plant.plant_name if inv.plant else inv.plant_id,
                inv.current_inventory,
                inv.plant.max_inventory_capacity if inv.plant else None,
                inv.plant.safety_stock_level if inv.plant else None,
                inv.utilization_pct,
                "Below safety" if inv.below_safety else "OK",
                inv.last_updated,
            ]
            for inv in inventories
            if include_inactive or (inv.plant and inv.plant.status == "active")
        ]
    elif dataset == "scenarios":
        scenarios = (
            PlanningScenario.for_org(org_id)
            .order_by(PlanningScenario.created_at.desc())
            .all()
        )
        headers = ["id", "name", "periods", "status", "result_cost", "created_at"]
        rows = [
            [s.id, s.scenario_name, s.periods, s.status, s.result_cost, s.created_at]
            for s in scenarios
            if include_inactive or s.status != "draft"
        ]
    else:
        abort(400)

    _log_activity(
        org_id,
        current_user.id,
        "export_csv",
        f"CSV export for {dataset}",
        entity_type=dataset,
        severity="info",
    )
    db.session.commit()
    current_app.logger.info("CSV export for %s by %s", dataset, current_user.email)
    return _csv_response(filename, headers, rows)


@ops_bp.route("/exports/pdf", methods=["POST"])
@login_required
@tenant_required
def export_pdf():
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas

    org_id = _org_id()
    org = Organization.query.get(org_id)
    form = PdfReportForm()
    if not form.validate_on_submit():
        flash("Please choose a valid report type.", "danger")
        return redirect(url_for("ops.network"))

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    margin = 0.65 * inch

    theme = {
        "bg": colors.HexColor("#0f172a"),
        "panel": colors.HexColor("#111827"),
        "border": colors.HexColor("#1f2937"),
        "primary": colors.HexColor("#2563eb"),
        "accent": colors.HexColor("#f97316"),
        "muted": colors.HexColor("#94a3b8"),
        "success": colors.HexColor("#22c55e"),
        "warning": colors.HexColor("#fbbf24"),
    }

    timeframe_labels = {
        "current": "Current snapshot",
        "last_30_days": "Last 30 days",
        "last_quarter": "Last quarter",
    }
    timeframe_start = None
    if form.timeframe.data == "last_30_days":
        timeframe_start = datetime.utcnow() - timedelta(days=30)
    elif form.timeframe.data == "last_quarter":
        timeframe_start = datetime.utcnow() - timedelta(days=90)

    def draw_background() -> None:
        c.setFillColor(theme["bg"])
        c.rect(0, 0, width, height, stroke=0, fill=1)

    def draw_header(title: str, subtitle: str) -> float:
        banner_h = 78
        c.setFillColor(theme["panel"])
        c.roundRect(margin, height - margin - banner_h, width - 2 * margin, banner_h, 16, stroke=0, fill=1)
        c.setStrokeColor(theme["border"])
        c.setLineWidth(1.2)
        c.roundRect(margin, height - margin - banner_h, width - 2 * margin, banner_h, 16, stroke=1, fill=0)

        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(margin + 16, height - margin - 26, title)
        c.setFont("Helvetica", 10)
        c.setFillColor(theme["muted"])
        c.drawString(margin + 16, height - margin - 42, subtitle)
        c.setFillColor(colors.white)
        c.setFont("Helvetica", 9)
        c.drawString(margin + 16, height - margin - 58, f"Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
        return height - margin - banner_h - 16

    def metric_card(x: float, y: float, w: float, h: float, title: str, value: str, subtitle: str, color) -> None:
        c.setFillColor(theme["panel"])
        c.roundRect(x, y - h, w, h, 12, stroke=0, fill=1)
        c.setStrokeColor(color)
        c.setLineWidth(1)
        c.roundRect(x, y - h, w, h, 12, stroke=1, fill=0)
        c.setFillColor(theme["muted"])
        c.setFont("Helvetica", 9)
        c.drawString(x + 12, y - 16, title)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(x + 12, y - 36, value)
        c.setFillColor(theme["muted"])
        c.setFont("Helvetica", 8)
        c.drawString(x + 12, y - 52, subtitle)

    def section_title(y_pos: float, label: str, accent_color) -> float:
        c.setFillColor(accent_color)
        c.rect(margin, y_pos - 4, 6, 18, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(margin + 14, y_pos + 10, label)
        return y_pos - 16

    def bullet_line(y_pos: float, text: str, color_choice, bold: bool = False) -> float:
        c.setFillColor(color_choice)
        c.circle(margin + 6, y_pos + 4, 2.5, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9)
        c.drawString(margin + 16, y_pos, text)
        return y_pos - 14

    draw_background()

    report_title = f"{org.name if org else 'Organization'} — {form.report_type.data.replace('_', ' ').title()}"
    time_text = timeframe_labels.get(form.timeframe.data, "Current snapshot")
    y = draw_header(report_title, time_text)

    if form.report_type.data == "inventory_health":
        inventory_query = Inventory.for_org(org_id).join(Plant).order_by(Plant.plant_name)
        if timeframe_start:
            inventory_query = inventory_query.filter(Inventory.last_updated >= timeframe_start)
        inventories = inventory_query.all()

        alerts = [inv for inv in inventories if inv.below_safety]
        safe = [inv for inv in inventories if not inv.below_safety]
        total = len(inventories)
        coverage = int(round((len(safe) / total * 100), 0)) if total else 0

        y = section_title(y, "Inventory Health", theme["primary"])
        card_w = (width - 2 * margin - 12) / 3
        card_h = 64
        metric_card(margin, y, card_w, card_h, "Plants monitored", str(total), time_text, theme["primary"])
        metric_card(margin + card_w + 6, y, card_w, card_h, "Safe range", str(len(safe)), f"Coverage {coverage}%", theme["success"])
        metric_card(margin + 2 * (card_w + 6), y, card_w, card_h, "Safety alerts", str(len(alerts)), "Focus required", theme["warning"])
        y -= card_h + 18

        y = bullet_line(y, "Highlights", theme["primary"], bold=True)
        y = bullet_line(y, f"Coverage {coverage}% | Safe {len(safe)} | Alerts {len(alerts)}", theme["muted"])
        if form.highlight_alerts.data and alerts:
            y = bullet_line(y, "Alerts are spotlighted in amber", theme["warning"])

        y -= 6
        c.setFillColor(theme["muted"])
        c.setFont("Helvetica-Bold", 10)
        c.drawString(margin, y, "Top safety alerts")
        y -= 16
        c.setFont("Helvetica", 9)
        max_rows = 14
        for inv in alerts[:max_rows]:
            row_color = theme["warning"] if form.highlight_alerts.data else theme["muted"]
            c.setFillColor(row_color)
            c.circle(margin + 4, y + 3, 2.2, stroke=0, fill=1)
            c.setFillColor(colors.white)
            plant_name = inv.plant.plant_name if inv.plant else inv.plant_id
            c.drawString(
                margin + 12,
                y,
                f"{plant_name} · Current {inv.current_inventory} | Safety {inv.plant.safety_stock_level if inv.plant else 'n/a'}",
            )
            y -= 14
            if y < margin + 60:
                c.showPage()
                draw_background()
                y = height - margin
    else:
        routes_query = TransportRoute.for_org(org_id).order_by(TransportRoute.created_at.desc())
        if timeframe_start:
            routes_query = routes_query.filter(TransportRoute.created_at >= timeframe_start)
        routes = routes_query.all()
        active_routes = [r for r in routes if r.status == "active"]
        avg_cost = sum(float(r.cost_per_trip or 0) for r in routes) / len(routes) if routes else 0

        y = section_title(y, "Transport Network", theme["accent"])
        card_w = (width - 2 * margin - 12) / 3
        card_h = 64
        metric_card(margin, y, card_w, card_h, "Total routes", str(len(routes)), time_text, theme["primary"])
        metric_card(margin + card_w + 6, y, card_w, card_h, "Active", str(len(active_routes)), "Operational", theme["success"])
        metric_card(margin + 2 * (card_w + 6), y, card_w, card_h, "Avg cost/trip", f"{avg_cost:.2f}", "Currency units", theme["accent"])
        y -= card_h + 20

        c.setFillColor(theme["muted"])
        c.setFont("Helvetica-Bold", 10)
        c.drawString(margin, y, "Recent routes")
        y -= 14
        c.setFont("Helvetica", 9)
        for r in routes[:18]:
            c.setFillColor(theme["muted"])
            c.circle(margin + 4, y + 3, 2.2, stroke=0, fill=1)
            c.setFillColor(colors.white)
            source = r.source_plant.plant_name if r.source_plant else r.source_plant_id
            dest = r.destination_plant.plant_name if r.destination_plant else r.destination_plant_id
            c.drawString(margin + 12, y, f"{source} → {dest} | {r.mode} | Cost {r.cost_per_trip}")
            y -= 14
            if y < margin + 60:
                c.showPage()
                draw_background()
                y = height - margin

    c.showPage()
    c.save()
    buffer.seek(0)

    filename = f"{form.report_type.data}-{_safe_org_slug(org)}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.pdf"
    _log_activity(
        org_id,
        current_user.id,
        "export_pdf",
        f"PDF report {form.report_type.data}",
        entity_type=form.report_type.data,
        severity="info",
    )
    db.session.commit()
    current_app.logger.info("PDF export for %s by %s", form.report_type.data, current_user.email)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name=filename)


@ops_bp.route("/activity")
@login_required
@tenant_required
def activity_log():
    org_id = _org_id()
    form = ActivityLogFilterForm(request.args, meta={"csrf": False})
    query = ActivityLog.for_org(org_id).order_by(ActivityLog.created_at.desc())

    if form.validate():
        if form.action_type.data:
            query = query.filter_by(action_type=form.action_type.data)
        if form.entity_type.data:
            query = query.filter_by(entity_type=form.entity_type.data)
        if form.user_id.data:
            query = query.filter_by(user_id=form.user_id.data)
        if form.severity.data:
            query = query.filter_by(severity=form.severity.data)
        if form.start_date.data:
            query = query.filter(ActivityLog.created_at >= datetime.combine(form.start_date.data, datetime.min.time()))
        if form.end_date.data:
            query = query.filter(ActivityLog.created_at <= datetime.combine(form.end_date.data, datetime.max.time()))
        if form.search.data:
            like = f"%{form.search.data.lower()}%"
            query = query.filter(func.lower(ActivityLog.action_description).like(like))

    page = max(int(request.args.get("page", 1) or 1), 1)
    logs = query.paginate(page=page, per_page=20, error_out=False)

    return render_template(
        "operations/activity_log.html",
        form=form,
        logs=logs,
    )


@ops_bp.route("/notifications", methods=["POST"])
@login_required
@tenant_required
def manage_notifications():
    org_id = _org_id()
    form = NotificationInboxFilterForm()
    if not form.validate_on_submit():
        flash("Unable to update notifications.", "danger")
        return redirect(url_for("ops.network"))

    query = Notification.for_org(org_id)
    if form.severity.data:
        query = query.filter_by(severity=form.severity.data)
    if form.unread_only.data:
        query = query.filter_by(is_read=False)

    updated = 0
    if form.acknowledge_all.data:
        updated = query.update({"is_read": True})
        db.session.commit()
        flash(f"Marked {updated} notification(s) as read.", "success")
    else:
        flash("Notification preferences saved.", "success")
    return redirect(url_for("ops.network"))


