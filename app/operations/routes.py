"""Supply chain planning + operations blueprint."""
import csv
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import math
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
    ClinkerCapacity,
    ClinkerDemand,
    Inventory,
    HubOpeningStock,
    IUGUClosingStock,
    IUGUConstraint,
    IUGUOpeningStock,
    IUGUType,
    LogisticsIUGU,
    Notification,
    Organization,
    Plant,
    PlantDemand,
    ProductionCost,
    PlanningScenario,
    TransportRoute,
    Workspace,
    WorkspaceDataset,
    OptimizationJob,
    OptimizationResult,
)
from ..tenant.utils import admin_required, tenant_required, get_tenant_record_or_404
from .forms import (
    WorkspaceForm,
    PlantForm,
    PlantDemandForm,
    TransportRouteForm,
    InventoryUpdateForm,
    ScenarioForm,
    OptimizationRunForm,
    CsvExportForm,
    PdfReportForm,
    OptimizationExportForm,
    NotificationInboxFilterForm,
    IUGUTypeForm,
    ClinkerDemandInputForm,
    ClinkerCapacityForm,
    LogisticsIUGUForm,
    IUGUConstraintForm,
    IUGUOpeningStockForm,
    IUGUClosingStockForm,
    HubOpeningStockForm,
    ProductionCostForm,
    CsvUploadForm,
)
from ..optimization.engine import OptimizationEngine, OptimizationRequest


ops_bp = Blueprint("ops", __name__, url_prefix="/operations")


SAFE_EXPORT_DATASETS = {"plants", "transport_routes", "inventory"}


CSV_SCHEMAS = {
    "clinker_demand": ["IUGU CODE", "TIME PERIOD", "DEMAND", "MIN FULFILLMENT (%)"],
    "clinker_capacity": ["IU CODE", "TIME PERIOD", "CAPACITY"],
    "production_cost": ["IU CODE", "TIME PERIOD", "PRODUCTION COST"],
    "logistics_iugu": [
        "FROM IU CODE",
        "TO IUGU CODE",
        "TRANSPORT CODE",
        "TIME PERIOD",
        "FREIGHT COST",
        "HANDLING COST",
        "QUANTITY MULTIPLIER",
    ],
    "iugu_constraint": [
        "IU CODE",
        "TRANSPORT CODE",
        "IUGU CODE",
        "TIME PERIOD",
        "BOUND TYPEID",
        "VALUE TYPEID",
        "Value",
    ],
    "iugu_opening": ["IUGU CODE", "OPENING STOCK"],
    "hub_opening": ["IU", "IUGU", "Opening Stock"],
    "iugu_closing": ["IUGU CODE", "TIME PERIOD", "MIN CLOSE STOCK", "MAX CLOSE STOCK"],
    "iugu_type": ["IUGU CODE", "PLANT TYPE", "# Source"],
}


def _org_id() -> int:
    org_id = session.get("org_id") or getattr(current_user, "organization_id", None)
    if org_id is None:
        abort(403)
    return org_id


def _safe_org_slug(org: Organization) -> str:
    return (org.name or "org").lower().replace(" ", "-")


def _workspace_query(org_id: int):
    return Workspace.for_org(org_id).order_by(Workspace.created_at.desc())


def _unique_scenario_name(org_id: int, base: str) -> str:
    candidate = base
    suffix = 2
    while PlanningScenario.for_org(org_id).filter_by(scenario_name=candidate).first():
        candidate = f"{base} ({suffix})"
        suffix += 1
    return candidate


def _unique_workspace_name(org_id: int, base: str) -> str:
    candidate = base
    suffix = 2
    while Workspace.for_org(org_id).filter_by(name=candidate).first():
        candidate = f"{base} ({suffix})"
        suffix += 1
    return candidate


def _unique_dataset_label(org_id: int, workspace_id: int, base: str) -> str:
    candidate = base
    suffix = 2
    while (
        WorkspaceDataset.for_org(org_id)
        .filter_by(workspace_id=workspace_id, label=candidate)
        .first()
    ):
        candidate = f"{base} ({suffix})"
        suffix += 1
    return candidate


def _bootstrap_default_workspace(org_id: int, user_id: int | None):
    workspace = Workspace(
        organization_id=org_id,
        name=_unique_workspace_name(org_id, "Default Workspace"),
        description="Auto-created",
        created_by=user_id,
    )
    scenario = PlanningScenario(
        organization_id=org_id,
        scenario_name=_unique_scenario_name(org_id, "Default dataset"),
        periods=3,
        status="draft",
    )
    db.session.add_all([workspace, scenario])
    db.session.flush()
    dataset = WorkspaceDataset(
        organization_id=org_id,
        workspace_id=workspace.id,
        planning_scenario_id=scenario.id,
        label="v1",
        notes="Bootstrap dataset",
        created_by=user_id,
        is_active=True,
    )
    db.session.add(dataset)
    db.session.commit()
    return workspace, dataset


def _resolve_workspace(org_id: int, workspace_id: int | None, user_id: int | None) -> Workspace:
    workspace = None
    if workspace_id is not None:
        workspace = _workspace_query(org_id).filter_by(id=workspace_id).first()
    if workspace is None:
        workspace = _workspace_query(org_id).first()
    if workspace is None:
        workspace, _ = _bootstrap_default_workspace(org_id, user_id)
    else:
        # Ensure the workspace has at least one dataset and a linked scenario; create if missing.
        dataset = WorkspaceDataset.for_org(org_id).filter_by(workspace_id=workspace.id).first()
        if dataset is None:
            scenario_name = _unique_scenario_name(org_id, f"{workspace.name} dataset")
            scenario = PlanningScenario(
                organization_id=org_id,
                scenario_name=scenario_name,
                periods=3,
                status="draft",
            )
            db.session.add(scenario)
            db.session.flush()
            dataset = WorkspaceDataset(
                organization_id=org_id,
                workspace_id=workspace.id,
                planning_scenario_id=scenario.id,
                label="v1",
                notes="Auto-created dataset",
                created_by=user_id,
                is_active=True,
            )
            db.session.add(dataset)
            db.session.commit()
    return workspace


@ops_bp.route("/scenarios", methods=["POST"])
@login_required
@tenant_required
def create_scenario():
    org_id = _org_id()
    form = ScenarioForm()
    workspace = None
    workspace_ctx = request.form.get("workspace_id")
    if workspace_ctx:
        try:
            workspace = _resolve_workspace(org_id, int(workspace_ctx), getattr(current_user, "id", None))
        except ValueError:  # pragma: no cover - guard
            abort(400)

    if form.validate_on_submit():
        existing = PlanningScenario.for_org(org_id).filter_by(scenario_name=form.scenario_name.data.strip()).first()
        if existing:
            flash("A scenario with that name already exists.", "warning")
            anchor = "optimization" if workspace else "scenarios"
            return redirect(url_for("ops.network", workspace_id=workspace.id if workspace else None, _anchor=anchor))

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
        if workspace:
            WorkspaceDataset.for_org(org_id).filter_by(workspace_id=workspace.id).update({"is_active": False})
            dataset = WorkspaceDataset(
                organization_id=org_id,
                workspace_id=workspace.id,
                planning_scenario_id=scenario.id,
                label=_unique_dataset_label(org_id, workspace.id, form.scenario_name.data.strip()),
                notes="Workspace dataset",
                created_by=getattr(current_user, "id", None),
                is_active=True,
            )
            db.session.add(dataset)

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

    anchor = "optimization" if workspace else "scenarios"
    return redirect(url_for("ops.network", workspace_id=workspace.id if workspace else None, _anchor=anchor))




def _resolve_dataset(workspace: Workspace, dataset_id: int | None, user_id: int | None) -> WorkspaceDataset:
    datasets = WorkspaceDataset.for_org(workspace.organization_id).filter_by(workspace_id=workspace.id)
    dataset = None

    if dataset_id is not None:
        dataset = datasets.filter_by(id=dataset_id).first()
        # If the requested dataset does not belong to this workspace, ignore it and fall back.
        if dataset is not None and dataset.workspace_id != workspace.id:
            dataset = None

    if dataset is None:
        dataset = datasets.filter_by(is_active=True).order_by(WorkspaceDataset.created_at.desc()).first()

    if dataset is None:
        # No dataset for this workspace; create a fresh one with a unique label.
        scenario_name = _unique_scenario_name(workspace.organization_id, f"{workspace.name} dataset")
        scenario = PlanningScenario(
            organization_id=workspace.organization_id,
            scenario_name=scenario_name,
            periods=3,
            status="draft",
        )
        db.session.add(scenario)
        db.session.flush()
        dataset = WorkspaceDataset(
            organization_id=workspace.organization_id,
            workspace_id=workspace.id,
            planning_scenario_id=scenario.id,
            label=_unique_dataset_label(workspace.organization_id, workspace.id, "v1"),
            notes="Auto-created dataset",
            created_by=user_id,
            is_active=True,
        )
        db.session.add(dataset)
        db.session.commit()

    return dataset


def _dataset_scenario_ids(org_id: int, workspace_id: int) -> list[int]:
    datasets = WorkspaceDataset.for_org(org_id).filter_by(workspace_id=workspace_id).all()
    return [ds.planning_scenario_id for ds in datasets if ds.planning_scenario_id]


def _dataset_context_from_request() -> tuple[Workspace, WorkspaceDataset]:
    org_id = _org_id()
    workspace_raw = request.form.get("workspace_id") or request.args.get("workspace_id") or session.get("active_workspace_id")
    dataset_raw = request.form.get("dataset_id") or request.args.get("dataset_id") or session.get("active_dataset_id")

    try:
        workspace_id = int(workspace_raw) if workspace_raw is not None else None
    except ValueError:  # pragma: no cover - guard
        abort(400)

    try:
        dataset_id = int(dataset_raw) if dataset_raw is not None else None
    except ValueError:  # pragma: no cover - guard
        abort(400)

    workspace = _resolve_workspace(org_id, workspace_id, getattr(current_user, "id", None))
    dataset = _resolve_dataset(workspace, dataset_id, getattr(current_user, "id", None))

    session["active_workspace_id"] = workspace.id
    session["active_dataset_id"] = dataset.id
    return workspace, dataset


def _plant_choices(org_id: int) -> list[tuple[int, str]]:
    plants = Plant.for_org(org_id).order_by(Plant.plant_name).all()
    return [(p.id, f"{p.plant_name} ({'IU' if p.plant_type == 'IU' else 'GU'})") for p in plants]


def _scenario_choices(org_id: int, workspace_id: int | None = None, dataset_id: int | None = None) -> list[tuple[int, str]]:
    scenarios = PlanningScenario.for_org(org_id).order_by(PlanningScenario.created_at.desc())

    # If a specific dataset is provided, lock selection to its scenario only.
    if dataset_id is not None:
        ds = WorkspaceDataset.for_org(org_id).filter_by(id=dataset_id).first()
        if not ds:
            return []
        scenarios = scenarios.filter(PlanningScenario.id == ds.planning_scenario_id)
    elif workspace_id is not None:
        scenario_ids = _dataset_scenario_ids(org_id, workspace_id)
        if scenario_ids:
            scenarios = scenarios.filter(PlanningScenario.id.in_(scenario_ids))
        else:
            return []

    return [(s.id, f"{s.scenario_name} ({s.periods}p)") for s in scenarios.all()]


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


def _paginate(query, page: int, per_page: int = 5):
    total = query.count()
    pages = int(math.ceil(total / per_page)) if total else 0
    page = max(1, min(page, pages or 1))
    items = query.limit(per_page).offset((page - 1) * per_page).all()
    return {
        "items": items,
        "total": total,
        "page": page,
        "pages": pages,
        "has_prev": page > 1,
        "has_next": page < (pages or 1),
        "prev_page": page - 1 if page > 1 else None,
        "next_page": page + 1 if page < (pages or 1) else None,
    }


def _read_csv_rows(upload_file, expected_headers: list[str]) -> list[tuple[int, dict[str, str]]]:
    if upload_file is None:
        raise ValueError("Please choose a CSV file to upload.")

    try:
        content = upload_file.stream.read().decode("utf-8-sig")
    except Exception as exc:  # pragma: no cover - IO guard
        raise ValueError("Could not read CSV file.") from exc
    finally:
        upload_file.stream.seek(0)

    reader = csv.DictReader(StringIO(content))
    headers = [h.strip() if h else "" for h in (reader.fieldnames or [])]
    normalized_expected = [h.strip() for h in expected_headers]
    if headers != normalized_expected:
        raise ValueError(f"CSV header mismatch. Expected: {', '.join(normalized_expected)}")

    rows: list[tuple[int, dict[str, str]]] = []
    for idx, row in enumerate(reader, start=2):
        if row is None:
            continue
        cleaned = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()} if row else {}
        if not any((val or "").strip() for val in cleaned.values()):
            continue
        rows.append((idx, cleaned))
    return rows


def _to_int(value, label: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            return int(Decimal(str(value).strip()))
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise ValueError(f"{label} must be an integer.") from exc


def _to_decimal(value, label: str, allow_empty: bool = False) -> Decimal | None:
    if value is None or str(value).strip() == "":
        if allow_empty:
            return None
        raise ValueError(f"{label} is required.")
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc


@ops_bp.route("/workspaces", methods=["POST"])
@login_required
@tenant_required
def create_workspace():
    org_id = _org_id()
    form = WorkspaceForm()
    if form.validate_on_submit():
        workspace_name = _unique_workspace_name(org_id, form.name.data.strip())
        workspace = Workspace(
            organization_id=org_id,
            name=workspace_name,
            description=form.description.data.strip() if form.description.data else None,
            created_by=getattr(current_user, "id", None),
        )
        scenario = PlanningScenario(
            organization_id=org_id,
            scenario_name=_unique_scenario_name(org_id, f"{workspace.name} dataset"),
            periods=3,
            status="draft",
        )
        db.session.add(workspace)
        db.session.add(scenario)
        db.session.flush()
        dataset = WorkspaceDataset(
            organization_id=org_id,
            workspace_id=workspace.id,
            planning_scenario_id=scenario.id,
            label="v1",
            notes="Initial dataset",
            created_by=getattr(current_user, "id", None),
            is_active=True,
        )
        db.session.add(dataset)
        db.session.commit()
        flash("Workspace created.", "success")
        return redirect(url_for("ops.network", workspace_id=workspace.id))

    flash("Please correct the workspace fields.", "danger")
    return redirect(request.referrer or url_for("main.dashboard"))


@ops_bp.route("/workspaces", methods=["GET"])
@login_required
@tenant_required
def list_workspaces():
    org_id = _org_id()
    workspace_form = WorkspaceForm()
    workspaces = _workspace_query(org_id).all()
    ids = [w.id for w in workspaces]
    datasets = (
        WorkspaceDataset.for_org(org_id)
        .filter(WorkspaceDataset.workspace_id.in_(ids))
        .order_by(WorkspaceDataset.created_at.desc())
        .all()
        if ids
        else []
    )
    datasets_by_ws: dict[int, list[WorkspaceDataset]] = {}
    for ds in datasets:
        datasets_by_ws.setdefault(ds.workspace_id, []).append(ds)

    return render_template(
        "operations/workspaces.html",
        workspaces=workspaces,
        datasets_by_ws=datasets_by_ws,
        workspace_form=workspace_form,
    )


@ops_bp.route("/workspaces/<int:workspace_id>/datasets/<int:dataset_id>/activate", methods=["POST"])
@login_required
@tenant_required
def activate_workspace_dataset(workspace_id: int, dataset_id: int):
    org_id = _org_id()
    workspace = get_tenant_record_or_404(Workspace, workspace_id)
    dataset = get_tenant_record_or_404(WorkspaceDataset, dataset_id)
    if dataset.workspace_id != workspace.id or workspace.organization_id != org_id:
        abort(404)

    WorkspaceDataset.for_org(org_id).filter_by(workspace_id=workspace.id).update({"is_active": False})
    dataset.is_active = True
    db.session.add(dataset)
    db.session.commit()
    flash("Workspace dataset activated.", "success")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id))


@ops_bp.route("/network", defaults={"workspace_id": None, "dataset_id": None})
@ops_bp.route("/workspaces/<int:workspace_id>/network", defaults={"dataset_id": None})
@ops_bp.route("/workspaces/<int:workspace_id>/datasets/<int:dataset_id>/network")
@login_required
@tenant_required
def network(workspace_id: int | None, dataset_id: int | None):
    org_id = _org_id()

    workspace = _resolve_workspace(org_id, workspace_id, getattr(current_user, "id", None))
    dataset = _resolve_dataset(workspace, dataset_id, getattr(current_user, "id", None))
    session["active_workspace_id"] = workspace.id
    session["active_dataset_id"] = dataset.id

    export_form = CsvExportForm()
    pdf_form = PdfReportForm()
    notification_filter_form = NotificationInboxFilterForm()
    optimization_form = OptimizationRunForm()
    scenario_form = ScenarioForm()
    workspace_form = WorkspaceForm()
    scenario_form.workspace_id.data = workspace.id
    optimization_form.scenario_id.choices = _scenario_choices(org_id, workspace.id, dataset.id)
    optimization_export_form = OptimizationExportForm()
    optimization_export_form.scenario_id.choices = _scenario_choices(org_id, workspace.id, dataset.id)
    optimization_export_form.scenario_id.data = dataset.planning_scenario_id

    iugu_type_form = IUGUTypeForm()
    clinker_demand_form = ClinkerDemandInputForm()
    clinker_capacity_form = ClinkerCapacityForm()
    production_cost_form = ProductionCostForm()
    logistics_form = LogisticsIUGUForm()
    constraint_form = IUGUConstraintForm()
    iugu_opening_form = IUGUOpeningStockForm()
    hub_opening_form = HubOpeningStockForm()
    closing_form = IUGUClosingStockForm()
    iugu_type_csv_form = CsvUploadForm(prefix="iugu_type_csv")
    clinker_demand_csv_form = CsvUploadForm(prefix="clinker_demand_csv")
    clinker_capacity_csv_form = CsvUploadForm(prefix="clinker_capacity_csv")
    production_cost_csv_form = CsvUploadForm(prefix="production_cost_csv")
    logistics_csv_form = CsvUploadForm(prefix="logistics_csv")
    constraint_csv_form = CsvUploadForm(prefix="constraint_csv")
    iugu_opening_csv_form = CsvUploadForm(prefix="iugu_opening_csv")
    hub_opening_csv_form = CsvUploadForm(prefix="hub_opening_csv")
    closing_csv_form = CsvUploadForm(prefix="closing_csv")

    for csv_form in (
        iugu_type_csv_form,
        clinker_demand_csv_form,
        clinker_capacity_csv_form,
        production_cost_csv_form,
        logistics_csv_form,
        constraint_csv_form,
        iugu_opening_csv_form,
        hub_opening_csv_form,
        closing_csv_form,
    ):
        csv_form.workspace_id.data = workspace.id
        csv_form.dataset_id.data = dataset.id

    scenario_id = dataset.planning_scenario_id

    iugu_page = max(1, int(request.args.get("iugu_page", 1) or 1))
    demand_page = max(1, int(request.args.get("demand_page", 1) or 1))
    capacity_page = max(1, int(request.args.get("capacity_page", 1) or 1))
    prod_page = max(1, int(request.args.get("prod_page", 1) or 1))
    logistics_page = max(1, int(request.args.get("logistics_page", 1) or 1))
    constraint_page = max(1, int(request.args.get("constraint_page", 1) or 1))
    opening_page = max(1, int(request.args.get("opening_page", 1) or 1))
    hub_opening_page = max(1, int(request.args.get("hub_opening_page", 1) or 1))
    closing_page = max(1, int(request.args.get("closing_page", 1) or 1))

    iugu_types_page = _paginate(
        IUGUType.for_org(org_id)
        .filter_by(planning_scenario_id=scenario_id)
        .order_by(IUGUType.created_at.desc(), IUGUType.id.desc()),
        iugu_page,
    )
    clinker_demands_page = _paginate(
        ClinkerDemand.for_org(org_id)
        .filter_by(planning_scenario_id=scenario_id)
        .order_by(ClinkerDemand.created_at.desc(), ClinkerDemand.id.desc()),
        demand_page,
    )
    clinker_capacities_page = _paginate(
        ClinkerCapacity.for_org(org_id)
        .filter_by(planning_scenario_id=scenario_id)
        .order_by(ClinkerCapacity.created_at.desc(), ClinkerCapacity.id.desc()),
        capacity_page,
    )
    production_costs_page = _paginate(
        ProductionCost.for_org(org_id)
        .filter_by(planning_scenario_id=scenario_id)
        .order_by(ProductionCost.created_at.desc(), ProductionCost.id.desc()),
        prod_page,
    )
    logistics_records_page = _paginate(
        LogisticsIUGU.for_org(org_id)
        .filter_by(planning_scenario_id=scenario_id)
        .order_by(LogisticsIUGU.created_at.desc(), LogisticsIUGU.id.desc()),
        logistics_page,
    )
    constraint_records_page = _paginate(
        IUGUConstraint.for_org(org_id)
        .filter_by(planning_scenario_id=scenario_id)
        .order_by(IUGUConstraint.created_at.desc(), IUGUConstraint.id.desc()),
        constraint_page,
    )
    iugu_opening_stocks_page = _paginate(
        IUGUOpeningStock.for_org(org_id)
        .filter_by(planning_scenario_id=scenario_id)
        .order_by(IUGUOpeningStock.created_at.desc(), IUGUOpeningStock.id.desc()),
        opening_page,
    )
    hub_opening_stocks_page = _paginate(
        HubOpeningStock.for_org(org_id)
        .filter_by(planning_scenario_id=scenario_id)
        .order_by(HubOpeningStock.created_at.desc(), HubOpeningStock.id.desc()),
        hub_opening_page,
    )
    closing_stocks_page = _paginate(
        IUGUClosingStock.for_org(org_id)
        .filter_by(planning_scenario_id=scenario_id)
        .order_by(IUGUClosingStock.created_at.desc(), IUGUClosingStock.id.desc()),
        closing_page,
    )

    iugu_types = iugu_types_page["items"]
    clinker_demands = clinker_demands_page["items"]
    clinker_capacities = clinker_capacities_page["items"]
    production_costs = production_costs_page["items"]
    logistics_records = logistics_records_page["items"]
    constraint_records = constraint_records_page["items"]
    iugu_opening_stocks = iugu_opening_stocks_page["items"]
    hub_opening_stocks = hub_opening_stocks_page["items"]
    closing_stocks = closing_stocks_page["items"]

    scenarios = (
        PlanningScenario.for_org(org_id)
        .filter(PlanningScenario.id == scenario_id)
        .order_by(PlanningScenario.created_at.desc())
        .all()
    )

    recent_jobs = (
        OptimizationJob.for_org(org_id)
        .filter_by(scenario_id=scenario_id)
        .order_by(OptimizationJob.id.desc())
        .limit(5)
        .all()
    )

    workspace_datasets = (
        WorkspaceDataset.for_org(org_id)
        .filter_by(workspace_id=workspace.id)
        .order_by(WorkspaceDataset.created_at.desc())
        .all()
    )

    metrics = {
        "iugu_types": iugu_types_page["total"],
        "demands": clinker_demands_page["total"],
        "capacities": clinker_capacities_page["total"],
        "logistics": logistics_records_page["total"],
        "constraints": constraint_records_page["total"],
        "openings": iugu_opening_stocks_page["total"] + hub_opening_stocks_page["total"],
        "closings": closing_stocks_page["total"],
    }

    notifications = (
        Notification.for_org(org_id)
        .order_by(Notification.created_at.desc())
        .limit(8)
        .all()
    )

    latest_result = (
        OptimizationResult.for_org(org_id)
        .filter_by(scenario_id=scenario_id)
        .order_by(OptimizationResult.created_at.desc())
        .first()
    )

    return render_template(
        "operations/network.html",
        export_form=export_form,
        pdf_form=pdf_form,
        notification_filter_form=notification_filter_form,
        optimization_form=optimization_form,
        scenario_form=scenario_form,
        iugu_type_form=iugu_type_form,
        clinker_demand_form=clinker_demand_form,
        clinker_capacity_form=clinker_capacity_form,
        production_cost_form=production_cost_form,
        logistics_form=logistics_form,
        constraint_form=constraint_form,
        iugu_opening_form=iugu_opening_form,
        hub_opening_form=hub_opening_form,
        closing_form=closing_form,
        iugu_type_csv_form=iugu_type_csv_form,
        clinker_demand_csv_form=clinker_demand_csv_form,
        clinker_capacity_csv_form=clinker_capacity_csv_form,
        production_cost_csv_form=production_cost_csv_form,
        logistics_csv_form=logistics_csv_form,
        constraint_csv_form=constraint_csv_form,
        iugu_opening_csv_form=iugu_opening_csv_form,
        hub_opening_csv_form=hub_opening_csv_form,
        closing_csv_form=closing_csv_form,
        iugu_types=iugu_types,
        clinker_demands=clinker_demands,
        clinker_capacities=clinker_capacities,
        production_costs=production_costs,
        logistics_records=logistics_records,
        constraint_records=constraint_records,
        iugu_opening_stocks=iugu_opening_stocks,
        hub_opening_stocks=hub_opening_stocks,
        closing_stocks=closing_stocks,
        iugu_types_page=iugu_types_page,
        clinker_demands_page=clinker_demands_page,
        clinker_capacities_page=clinker_capacities_page,
        production_costs_page=production_costs_page,
        logistics_records_page=logistics_records_page,
        constraint_records_page=constraint_records_page,
        iugu_opening_stocks_page=iugu_opening_stocks_page,
        hub_opening_stocks_page=hub_opening_stocks_page,
        closing_stocks_page=closing_stocks_page,
        scenarios=scenarios,
        recent_jobs=recent_jobs,
        metrics=metrics,
        notifications=notifications,
        latest_result=latest_result,
        workspace=workspace,
        dataset=dataset,
        datasets=workspace_datasets,
        workspace_form=workspace_form,
        optimization_export_form=optimization_export_form,
    )


@ops_bp.route("/iugu-types", methods=["POST"])
@login_required
@tenant_required
@admin_required
def create_iugu_type():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = IUGUTypeForm()
    if form.validate_on_submit():
        record = IUGUType(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            code=form.code.data.strip(),
            plant_type=form.plant_type.data,
            sources_count=form.sources_count.data,
        )
        db.session.add(record)
        try:
            db.session.commit()
            flash("IUGU type saved.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("IUGU code already exists for this organization/scenario.", "warning")
    else:
        flash("Please correct the IUGU type inputs.", "danger")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor="iugu-types"))


@ops_bp.route("/clinker-demand", methods=["POST"])
@login_required
@tenant_required
@admin_required
def create_clinker_demand():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = ClinkerDemandInputForm()
    if form.validate_on_submit():
        record = ClinkerDemand(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            plant_code=form.plant_code.data.strip(),
            time_period=form.time_period.data,
            demand_tons=form.demand_tons.data,
            min_fulfillment_pct=form.min_fulfillment_pct.data,
        )
        db.session.add(record)
        try:
            db.session.commit()
            flash("Clinker demand saved.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("Demand already exists for this IUGU and period.", "warning")
    else:
        flash("Please correct the demand inputs.", "danger")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor="clinker-demand"))


@ops_bp.route("/clinker-capacity", methods=["POST"])
@login_required
@tenant_required
@admin_required
def create_clinker_capacity():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = ClinkerCapacityForm()
    if form.validate_on_submit():
        record = ClinkerCapacity(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            plant_code=form.plant_code.data.strip(),
            time_period=form.time_period.data,
            capacity_tons=form.capacity_tons.data,
        )
        db.session.add(record)
        try:
            db.session.commit()
            flash("Capacity saved.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("Capacity already exists for this IUGU and period.", "warning")
    else:
        flash("Please correct the capacity inputs.", "danger")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor="clinker-capacity"))


@ops_bp.route("/production-cost", methods=["POST"])
@login_required
@tenant_required
@admin_required
def create_production_cost():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = ProductionCostForm()
    if form.validate_on_submit():
        record = ProductionCost(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            plant_code=form.plant_code.data.strip(),
            time_period=form.time_period.data,
            cost_per_ton=form.cost_per_ton.data,
        )
        db.session.add(record)
        try:
            db.session.commit()
            flash("Production cost saved.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("Cost already exists for this IUGU and period.", "warning")
    else:
        flash("Please correct the production cost inputs.", "danger")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor="production-cost"))


@ops_bp.route("/logistics-iugu", methods=["POST"])
@login_required
@tenant_required
@admin_required
def create_logistics_iugu():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = LogisticsIUGUForm()
    if form.validate_on_submit():
        record = LogisticsIUGU(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            from_code=form.from_code.data.strip(),
            to_code=form.to_code.data.strip(),
            transport_code=form.transport_code.data.strip(),
            time_period=form.time_period.data,
            freight_cost=form.freight_cost.data,
            handling_cost=form.handling_cost.data or 0,
            quantity_multiplier=form.quantity_multiplier.data,
        )
        db.session.add(record)
        try:
            db.session.commit()
            flash("Logistics lane saved.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("A logistics record already exists for this from/to/period.", "warning")
    else:
        flash("Please correct the logistics inputs.", "danger")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor="logistics-iugu"))


@ops_bp.route("/iugu-constraint", methods=["POST"])
@login_required
@tenant_required
@admin_required
def create_iugu_constraint():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = IUGUConstraintForm()
    if form.validate_on_submit():
        record = IUGUConstraint(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            from_code=form.from_code.data.strip(),
            transport_code=form.transport_code.data.strip() if form.transport_code.data else None,
            to_code=form.to_code.data.strip() if form.to_code.data else None,
            time_period=form.time_period.data,
            constraint_type=form.constraint_type.data,
            value_type=form.value_type.data,
            value=form.value.data,
        )
        db.session.add(record)
        try:
            db.session.commit()
            flash("Constraint saved.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("A constraint already exists for this key.", "warning")
    else:
        flash("Please correct the constraint inputs.", "danger")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor="iugu-constraint"))


@ops_bp.route("/iugu-opening-stock", methods=["POST"])
@login_required
@tenant_required
@admin_required
def create_iugu_opening_stock():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = IUGUOpeningStockForm()
    if form.validate_on_submit():
        record = IUGUOpeningStock(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            plant_code=form.plant_code.data.strip(),
            opening_stock=form.opening_stock.data,
        )
        db.session.add(record)
        try:
            db.session.commit()
            flash("Opening stock saved.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("Opening stock already exists for this IUGU.", "warning")
    else:
        flash("Please correct the opening stock inputs.", "danger")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor="iugu-opening-stock"))


@ops_bp.route("/hub-opening-stock", methods=["POST"])
@login_required
@tenant_required
@admin_required
def create_hub_opening_stock():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = HubOpeningStockForm()
    if form.validate_on_submit():
        record = HubOpeningStock(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            from_code=form.from_code.data.strip(),
            to_code=form.to_code.data.strip(),
            opening_stock=form.opening_stock.data,
        )
        db.session.add(record)
        try:
            db.session.commit()
            flash("Hub opening stock saved.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("Hub opening stock already exists for this lane.", "warning")
    else:
        flash("Please correct the hub stock inputs.", "danger")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor="hub-opening-stock"))


@ops_bp.route("/iugu-closing-stock", methods=["POST"])
@login_required
@tenant_required
@admin_required
def create_iugu_closing_stock():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = IUGUClosingStockForm()
    if form.validate_on_submit():
        record = IUGUClosingStock(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            plant_code=form.plant_code.data.strip(),
            time_period=form.time_period.data,
            min_close_stock=form.min_close_stock.data,
            max_close_stock=form.max_close_stock.data,
        )
        db.session.add(record)
        try:
            db.session.commit()
            flash("Closing stock saved.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("Closing stock already exists for this IUGU and period.", "warning")
    else:
        flash("Please correct the closing stock inputs.", "danger")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor="iugu-closing-stock"))


@ops_bp.route("/iugu-types/upload", methods=["POST"])
@login_required
@tenant_required
@admin_required
def upload_iugu_type_csv():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = CsvUploadForm(prefix="iugu_type_csv")
    anchor = "iugu-types"

    if not form.validate_on_submit():
        flash("Upload a CSV file for IUGU types.", "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    try:
        rows = _read_csv_rows(form.file.data, CSV_SCHEMAS["iugu_type"])
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    created, skipped = 0, 0
    for line_no, row in rows:
        code = (row.get("IUGU CODE") or "").strip()
        plant_type = (row.get("PLANT TYPE") or "").strip()
        sources_raw = row.get("# Source")

        if not code or not plant_type:
            db.session.rollback()
            flash(f"Row {line_no}: IUGU code and plant type are required.", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        if plant_type not in {"IU", "GU"}:
            db.session.rollback()
            flash(f"Row {line_no}: Plant type must be IU or GU.", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        existing = (
            IUGUType.for_org(org_id)
            .filter_by(planning_scenario_id=dataset.planning_scenario_id, code=code)
            .first()
        )
        if existing:
            skipped += 1
            continue

        try:
            sources_count = _to_int(sources_raw, "# Source") if sources_raw not in (None, "") else None
        except ValueError as exc:
            db.session.rollback()
            flash(f"Row {line_no}: {exc}", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        record = IUGUType(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            code=code,
            plant_type=plant_type,
            sources_count=sources_count,
        )
        db.session.add(record)
        created += 1

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Import failed due to duplicate or invalid IUGU type rows.", "warning")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    flash(f"Imported {created} IUGU types (skipped {skipped} duplicates).", "success")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))


@ops_bp.route("/clinker-demand/upload", methods=["POST"])
@login_required
@tenant_required
@admin_required
def upload_clinker_demand_csv():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = CsvUploadForm(prefix="clinker_demand_csv")
    anchor = "clinker-demand"

    if not form.validate_on_submit():
        flash("Upload a CSV file for clinker demand.", "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    try:
        rows = _read_csv_rows(form.file.data, CSV_SCHEMAS["clinker_demand"])
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    created, skipped = 0, 0
    for line_no, row in rows:
        plant_code = (row.get("IUGU CODE") or "").strip()
        if not plant_code:
            db.session.rollback()
            flash(f"Row {line_no}: IUGU code is required.", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        try:
            period = _to_int(row.get("TIME PERIOD"), "TIME PERIOD")
            demand_tons = _to_decimal(row.get("DEMAND"), "DEMAND")
            min_pct = _to_decimal(row.get("MIN FULFILLMENT (%)"), "MIN FULFILLMENT (%)", allow_empty=True)
        except ValueError as exc:
            db.session.rollback()
            flash(f"Row {line_no}: {exc}", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        existing = (
            ClinkerDemand.for_org(org_id)
            .filter_by(planning_scenario_id=dataset.planning_scenario_id, plant_code=plant_code, time_period=period)
            .first()
        )
        if existing:
            skipped += 1
            continue

        record = ClinkerDemand(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            plant_code=plant_code,
            time_period=period,
            demand_tons=demand_tons,
            min_fulfillment_pct=min_pct if min_pct is not None else 100,
        )
        db.session.add(record)
        created += 1

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Import failed due to duplicate clinker demand rows.", "warning")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    flash(f"Imported {created} clinker demand rows (skipped {skipped} duplicates).", "success")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))


@ops_bp.route("/clinker-capacity/upload", methods=["POST"])
@login_required
@tenant_required
@admin_required
def upload_clinker_capacity_csv():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = CsvUploadForm(prefix="clinker_capacity_csv")
    anchor = "clinker-capacity"

    if not form.validate_on_submit():
        flash("Upload a CSV file for clinker capacity.", "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    try:
        rows = _read_csv_rows(form.file.data, CSV_SCHEMAS["clinker_capacity"])
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    created, skipped = 0, 0
    for line_no, row in rows:
        plant_code = (row.get("IU CODE") or "").strip()
        if not plant_code:
            db.session.rollback()
            flash(f"Row {line_no}: IU code is required.", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        try:
            period = _to_int(row.get("TIME PERIOD"), "TIME PERIOD")
            capacity_tons = _to_decimal(row.get("CAPACITY"), "CAPACITY")
        except ValueError as exc:
            db.session.rollback()
            flash(f"Row {line_no}: {exc}", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        existing = (
            ClinkerCapacity.for_org(org_id)
            .filter_by(planning_scenario_id=dataset.planning_scenario_id, plant_code=plant_code, time_period=period)
            .first()
        )
        if existing:
            skipped += 1
            continue

        record = ClinkerCapacity(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            plant_code=plant_code,
            time_period=period,
            capacity_tons=capacity_tons,
        )
        db.session.add(record)
        created += 1

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Import failed due to duplicate clinker capacity rows.", "warning")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    flash(f"Imported {created} clinker capacity rows (skipped {skipped} duplicates).", "success")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))


@ops_bp.route("/production-cost/upload", methods=["POST"])
@login_required
@tenant_required
@admin_required
def upload_production_cost_csv():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = CsvUploadForm(prefix="production_cost_csv")
    anchor = "production-cost"

    if not form.validate_on_submit():
        flash("Upload a CSV file for production cost.", "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    try:
        rows = _read_csv_rows(form.file.data, CSV_SCHEMAS["production_cost"])
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    created, skipped = 0, 0
    for line_no, row in rows:
        plant_code = (row.get("IU CODE") or "").strip()
        if not plant_code:
            db.session.rollback()
            flash(f"Row {line_no}: IU code is required.", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        try:
            period = _to_int(row.get("TIME PERIOD"), "TIME PERIOD")
            cost_per_ton = _to_decimal(row.get("PRODUCTION COST"), "PRODUCTION COST")
        except ValueError as exc:
            db.session.rollback()
            flash(f"Row {line_no}: {exc}", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        existing = (
            ProductionCost.for_org(org_id)
            .filter_by(planning_scenario_id=dataset.planning_scenario_id, plant_code=plant_code, time_period=period)
            .first()
        )
        if existing:
            skipped += 1
            continue

        record = ProductionCost(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            plant_code=plant_code,
            time_period=period,
            cost_per_ton=cost_per_ton,
        )
        db.session.add(record)
        created += 1

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Import failed due to duplicate production cost rows.", "warning")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    flash(f"Imported {created} production cost rows (skipped {skipped} duplicates).", "success")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))


@ops_bp.route("/logistics-iugu/upload", methods=["POST"])
@login_required
@tenant_required
@admin_required
def upload_logistics_iugu_csv():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = CsvUploadForm(prefix="logistics_csv")
    anchor = "logistics-iugu"

    if not form.validate_on_submit():
        flash("Upload a CSV file for logistics lanes.", "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    try:
        rows = _read_csv_rows(form.file.data, CSV_SCHEMAS["logistics_iugu"])
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    created, skipped = 0, 0
    for line_no, row in rows:
        from_code = (row.get("FROM IU CODE") or "").strip()
        to_code = (row.get("TO IUGU CODE") or "").strip()
        transport_code = (row.get("TRANSPORT CODE") or "").strip()
        if not from_code or not to_code or not transport_code:
            db.session.rollback()
            flash(f"Row {line_no}: From, To, and Transport code are required.", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        try:
            period = _to_int(row.get("TIME PERIOD"), "TIME PERIOD")
            freight_cost = _to_decimal(row.get("FREIGHT COST"), "FREIGHT COST")
            handling_cost = _to_decimal(row.get("HANDLING COST"), "HANDLING COST", allow_empty=True) or 0
            multiplier = _to_decimal(row.get("QUANTITY MULTIPLIER"), "QUANTITY MULTIPLIER")
        except ValueError as exc:
            db.session.rollback()
            flash(f"Row {line_no}: {exc}", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        existing = (
            LogisticsIUGU.for_org(org_id)
            .filter_by(
                planning_scenario_id=dataset.planning_scenario_id,
                from_code=from_code,
                to_code=to_code,
                transport_code=transport_code,
                time_period=period,
            )
            .first()
        )
        if existing:
            skipped += 1
            continue

        record = LogisticsIUGU(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            from_code=from_code,
            to_code=to_code,
            transport_code=transport_code,
            time_period=period,
            freight_cost=freight_cost,
            handling_cost=handling_cost,
            quantity_multiplier=multiplier,
        )
        db.session.add(record)
        created += 1

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Import failed due to duplicate logistics rows.", "warning")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    flash(f"Imported {created} logistics rows (skipped {skipped} duplicates).", "success")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))


@ops_bp.route("/iugu-constraint/upload", methods=["POST"])
@login_required
@tenant_required
@admin_required
def upload_iugu_constraint_csv():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = CsvUploadForm(prefix="constraint_csv")
    anchor = "iugu-constraint"

    if not form.validate_on_submit():
        flash("Upload a CSV file for constraints.", "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    try:
        rows = _read_csv_rows(form.file.data, CSV_SCHEMAS["iugu_constraint"])
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    created, skipped = 0, 0
    for line_no, row in rows:
        from_code = (row.get("IU CODE") or "").strip()
        transport_code = (row.get("TRANSPORT CODE") or "").strip() or None
        to_code = (row.get("IUGU CODE") or "").strip() or None
        if not from_code:
            db.session.rollback()
            flash(f"Row {line_no}: IU code is required.", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        try:
            period = _to_int(row.get("TIME PERIOD"), "TIME PERIOD")
            constraint_type = (row.get("BOUND TYPEID") or "").strip().upper()
            value_type = (row.get("VALUE TYPEID") or "").strip().upper()
            value = _to_decimal(row.get("Value"), "Value")
        except ValueError as exc:
            db.session.rollback()
            flash(f"Row {line_no}: {exc}", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        if constraint_type not in {"L", "G", "E"}:
            db.session.rollback()
            flash(f"Row {line_no}: BOUND TYPEID must be L, G, or E.", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))
        if value_type not in {"C"}:
            db.session.rollback()
            flash(f"Row {line_no}: VALUE TYPEID must be C.", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        existing = (
            IUGUConstraint.for_org(org_id)
            .filter_by(
                planning_scenario_id=dataset.planning_scenario_id,
                from_code=from_code,
                transport_code=transport_code,
                to_code=to_code,
                time_period=period,
                constraint_type=constraint_type,
            )
            .first()
        )
        if existing:
            skipped += 1
            continue

        record = IUGUConstraint(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            from_code=from_code,
            transport_code=transport_code,
            to_code=to_code,
            time_period=period,
            constraint_type=constraint_type,
            value_type=value_type,
            value=value,
        )
        db.session.add(record)
        created += 1

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Import failed due to duplicate constraints.", "warning")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    flash(f"Imported {created} constraints (skipped {skipped} duplicates).", "success")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))


@ops_bp.route("/iugu-opening-stock/upload", methods=["POST"])
@login_required
@tenant_required
@admin_required
def upload_iugu_opening_stock_csv():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = CsvUploadForm(prefix="iugu_opening_csv")
    anchor = "iugu-opening-stock"

    if not form.validate_on_submit():
        flash("Upload a CSV file for IUGU opening stock.", "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    try:
        rows = _read_csv_rows(form.file.data, CSV_SCHEMAS["iugu_opening"])
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    created, skipped = 0, 0
    for line_no, row in rows:
        plant_code = (row.get("IUGU CODE") or "").strip()
        if not plant_code:
            db.session.rollback()
            flash(f"Row {line_no}: IUGU code is required.", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        try:
            opening_stock = _to_decimal(row.get("OPENING STOCK"), "OPENING STOCK")
        except ValueError as exc:
            db.session.rollback()
            flash(f"Row {line_no}: {exc}", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        existing = (
            IUGUOpeningStock.for_org(org_id)
            .filter_by(planning_scenario_id=dataset.planning_scenario_id, plant_code=plant_code)
            .first()
        )
        if existing:
            skipped += 1
            continue

        record = IUGUOpeningStock(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            plant_code=plant_code,
            opening_stock=opening_stock,
        )
        db.session.add(record)
        created += 1

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Import failed due to duplicate opening stock rows.", "warning")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    flash(f"Imported {created} IUGU opening stock rows (skipped {skipped} duplicates).", "success")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))


@ops_bp.route("/hub-opening-stock/upload", methods=["POST"])
@login_required
@tenant_required
@admin_required
def upload_hub_opening_stock_csv():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = CsvUploadForm(prefix="hub_opening_csv")
    anchor = "hub-opening-stock"

    if not form.validate_on_submit():
        flash("Upload a CSV file for hub opening stock.", "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    try:
        rows = _read_csv_rows(form.file.data, CSV_SCHEMAS["hub_opening"])
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    created, skipped = 0, 0
    for line_no, row in rows:
        from_code = (row.get("IU") or "").strip()
        to_code = (row.get("IUGU") or "").strip()
        if not from_code or not to_code:
            db.session.rollback()
            flash(f"Row {line_no}: IU and IUGU codes are required.", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        try:
            opening_stock = _to_decimal(row.get("Opening Stock"), "Opening Stock")
        except ValueError as exc:
            db.session.rollback()
            flash(f"Row {line_no}: {exc}", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        existing = (
            HubOpeningStock.for_org(org_id)
            .filter_by(
                planning_scenario_id=dataset.planning_scenario_id,
                from_code=from_code,
                to_code=to_code,
            )
            .first()
        )
        if existing:
            skipped += 1
            continue

        record = HubOpeningStock(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            from_code=from_code,
            to_code=to_code,
            opening_stock=opening_stock,
        )
        db.session.add(record)
        created += 1

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Import failed due to duplicate hub opening stock rows.", "warning")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    flash(f"Imported {created} hub opening stock rows (skipped {skipped} duplicates).", "success")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))


@ops_bp.route("/iugu-closing-stock/upload", methods=["POST"])
@login_required
@tenant_required
@admin_required
def upload_iugu_closing_stock_csv():
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = CsvUploadForm(prefix="closing_csv")
    anchor = "iugu-closing-stock"

    if not form.validate_on_submit():
        flash("Upload a CSV file for IUGU closing stock targets.", "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    try:
        rows = _read_csv_rows(form.file.data, CSV_SCHEMAS["iugu_closing"])
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    created, skipped = 0, 0
    for line_no, row in rows:
        plant_code = (row.get("IUGU CODE") or "").strip()
        if not plant_code:
            db.session.rollback()
            flash(f"Row {line_no}: IUGU code is required.", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        try:
            period = _to_int(row.get("TIME PERIOD"), "TIME PERIOD")
            min_close = _to_decimal(row.get("MIN CLOSE STOCK"), "MIN CLOSE STOCK")
            max_close = _to_decimal(row.get("MAX CLOSE STOCK"), "MAX CLOSE STOCK", allow_empty=True)
        except ValueError as exc:
            db.session.rollback()
            flash(f"Row {line_no}: {exc}", "danger")
            return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

        existing = (
            IUGUClosingStock.for_org(org_id)
            .filter_by(planning_scenario_id=dataset.planning_scenario_id, plant_code=plant_code, time_period=period)
            .first()
        )
        if existing:
            skipped += 1
            continue

        record = IUGUClosingStock(
            organization_id=org_id,
            planning_scenario_id=dataset.planning_scenario_id,
            plant_code=plant_code,
            time_period=period,
            min_close_stock=min_close,
            max_close_stock=max_close,
        )
        db.session.add(record)
        created += 1

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Import failed due to duplicate closing stock rows.", "warning")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))

    flash(f"Imported {created} closing stock rows (skipped {skipped} duplicates).", "success")
    return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor=anchor))


@ops_bp.route("/plant-demands", methods=["POST"])
@login_required
@tenant_required
@admin_required
def create_plant_demand():
    org_id = _org_id()
    form = PlantDemandForm()
    form.plant_id.choices = _plant_choices(org_id)

    if form.validate_on_submit():
        plant = Plant.for_org(org_id).filter_by(id=form.plant_id.data).first()
        if not plant:
            abort(404)

        existing = (
            PlantDemand.for_org(org_id)
            .filter_by(plant_id=plant.id, time_period=form.time_period.data)
            .first()
        )
        if existing:
            flash("A demand entry for this plant and period already exists.", "warning")
            return redirect(url_for("ops.network", _anchor="demand"))

        record = PlantDemand(
            organization_id=org_id,
            plant_id=plant.id,
            time_period=form.time_period.data,
            demand=form.demand.data,
            min_fulfillment_pct=form.min_fulfillment_pct.data if form.min_fulfillment_pct.data is not None else 100,
        )
        db.session.add(record)
        db.session.flush()
        _log_activity(
            org_id,
            current_user.id,
            "demand_created",
            f"Demand for {plant.plant_name} period {record.time_period} created",
            entity_type="demand",
            entity_id=record.id,
            details={"demand": float(record.demand), "min_fulfillment_pct": float(record.min_fulfillment_pct or 0)},
        )
        db.session.commit()
        flash("Demand saved.", "success")
    else:
        flash("Please correct the demand inputs and try again.", "danger")

    return redirect(url_for("ops.network", _anchor="demand"))


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
    workspace, dataset = _dataset_context_from_request()
    org_id = workspace.organization_id
    form = OptimizationRunForm()
    form.scenario_id.choices = _scenario_choices(org_id, workspace.id)

    if not form.validate_on_submit():
        flash("Please review the optimization inputs.", "danger")
        return redirect(url_for("ops.network", workspace_id=workspace.id, dataset_id=dataset.id, _anchor="optimization"))

    scenario = get_tenant_record_or_404(PlanningScenario, form.scenario_id.data)

    # Enforce that the selected scenario belongs to the active workspace AND dataset
    scenario_ds = WorkspaceDataset.for_org(org_id).filter_by(planning_scenario_id=scenario.id).first()
    if (
        scenario_ds is None
        or scenario_ds.workspace_id != workspace.id
        or scenario_ds.id != dataset.id
    ):
        flash("Select the scenario for this dataset only.", "warning")
        return redirect(
            url_for(
                "ops.network",
                workspace_id=workspace.id,
                dataset_id=dataset.id,
                _anchor="optimization",
            )
        )

    dataset = scenario_ds
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
            kpis={
                **solution.kpis,
                "trips_plan": trips_plan,
                "shortage_plan": shortage_plan,
                "ui_views": solution.ui_views,
                "solver_diagnostics": solution.diagnostics,
                "runtime_seconds": solution.runtime_seconds,
            },
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

    return redirect(
        url_for(
            "ops.network",
            workspace_id=workspace.id,
            dataset_id=dataset.id,
            _anchor="optimization",
        )
    )


@ops_bp.route("/optimization/latest", methods=["GET"])
@login_required
@tenant_required
def latest_optimization_result():
    org_id = _org_id()
    scenario_id = request.args.get("scenario_id", type=int)

    query = OptimizationResult.for_org(org_id)
    if scenario_id:
        query = query.filter_by(scenario_id=scenario_id)
    result = query.order_by(OptimizationResult.created_at.desc()).first()
    if result is None:
        return {"error": "no_results"}, 404

    def _coerce_num(val):
        try:
            return float(val)
        except (TypeError, ValueError):
            return val

    payload = {
        "id": result.id,
        "scenario_id": result.scenario_id,
        "job_id": result.job_id,
        "total_cost": _coerce_num(result.total_cost),
        "cost_breakdown": result.cost_breakdown or {},
        "kpis": result.kpis or {},
        "created_at": result.created_at.isoformat() if result.created_at else None,
        "solver_status": getattr(result.job, "solver_status", None),
        "status": getattr(result.job, "status", None),
    }
    return payload


@ops_bp.route("/exports/optimization/csv", methods=["POST"])
@login_required
@tenant_required
def export_optimization_csv():
    org_id = _org_id()
    org = Organization.query.get(org_id)
    form = OptimizationExportForm()
    form.scenario_id.choices = _scenario_choices(org_id)

    if not form.validate_on_submit():
        flash("Select a scenario and at least one section to export.", "danger")
        return redirect(url_for("ops.network", _anchor="optimization"))

    result = (
        OptimizationResult.for_org(org_id)
        .filter_by(scenario_id=form.scenario_id.data)
        .order_by(OptimizationResult.created_at.desc())
        .first()
    )
    if result is None:
        flash("No optimization output available for that scenario yet.", "warning")
        return redirect(url_for("ops.network", _anchor="optimization"))

    ui = (result.kpis or {}).get("ui_views", {}) or {}
    summary = ui.get("summary", {}) or {}
    sections = set(form.sections.data or [])

    buf = StringIO()
    writer = csv.writer(buf)
    scenario_label = result.scenario.scenario_name if result.scenario else form.scenario_id.data
    writer.writerow(["Scenario", scenario_label])
    writer.writerow(["Generated", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")])
    writer.writerow([])

    def section_heading(title: str):
        writer.writerow([title])

    def blank_line():
        writer.writerow([])

    if "summary" in sections:
        section_heading("Summary")
        writer.writerow(["Metric", "Value"])
        costs = summary.get("costs", {}) or {}
        writer.writerow(["Objective", summary.get("objective", result.total_cost)])
        writer.writerow(["Service level %", summary.get("service_level_pct")])
        writer.writerow(["Runtime (s)", summary.get("runtime_seconds", summary.get("runtime_seconds", 0))])
        writer.writerow(["Production cost", costs.get("production")])
        writer.writerow(["Transport cost", costs.get("transport")])
        writer.writerow(["Holding cost", costs.get("holding")])
        writer.writerow(["Total cost", costs.get("total", result.total_cost)])
        blank_line()

    if "dispatch" in sections:
        dispatch_rows = ui.get("dispatch", []) or []
        section_heading("Dispatch plan")
        if dispatch_rows:
            writer.writerow(["Period", "From", "To", "Mode", "Trips", "Qty", "Trip cost", "Route cost"])
            for row in dispatch_rows:
                writer.writerow(
                    [
                        row.get("period"),
                        row.get("from"),
                        row.get("to"),
                        row.get("mode"),
                        round(row.get("trips", 0), 2),
                        round(row.get("qty", 0), 2),
                        round(row.get("unit_trip_cost", 0), 2),
                        round(row.get("route_cost", 0), 2),
                    ]
                )
        else:
            writer.writerow(["No dispatch plan available."])
        blank_line()

    if "production" in sections:
        production_rows = ui.get("production", []) or []
        section_heading("Production plan")
        if production_rows:
            writer.writerow(["IU", "Period", "Produced", "Capacity", "Utilization %", "Cost/ton", "Total cost"])
            for row in production_rows:
                writer.writerow(
                    [
                        row.get("code"),
                        row.get("period"),
                        round(row.get("produced", 0), 2),
                        round(row.get("capacity", 0), 2),
                        round(row.get("utilization_pct", 0), 2),
                        round(row.get("cost_per_ton", 0), 2),
                        round(row.get("cost", 0), 2),
                    ]
                )
        else:
            writer.writerow(["No production plan available."])
        blank_line()

    if "inventory" in sections:
        inventory_rows = ui.get("inventory", []) or []
        section_heading("Inventory ledger")
        if inventory_rows:
            writer.writerow(
                [
                    "Plant",
                    "Period",
                    "Opening",
                    "Inbound",
                    "Shipments out",
                    "Demand",
                    "Outbound total",
                    "Closing",
                    "Min",
                    "Max",
                ]
            )
            for row in inventory_rows:
                writer.writerow(
                    [
                        row.get("code"),
                        row.get("period"),
                        round(row.get("opening", 0), 2),
                        round(row.get("inbound", 0), 2),
                        round(row.get("outbound_ship", 0), 2),
                        round(row.get("demand", 0), 2),
                        round(row.get("outbound_total", 0), 2),
                        round(row.get("closing", 0), 2),
                        round(row.get("min_close", 0), 2),
                        row.get("max_close") if row.get("max_close") is not None else "—",
                    ]
                )
        else:
            writer.writerow(["No inventory ledger available."])
        blank_line()

    filename = f"optimization-{_safe_org_slug(org)}-scenario-{form.scenario_id.data}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.csv"
    mem = BytesIO(buf.getvalue().encode("utf-8"))
    mem.seek(0)
    _log_activity(
        org_id,
        current_user.id,
        "export_optimization_csv",
        f"Optimization export for scenario {form.scenario_id.data}",
        entity_type="optimization_result",
        entity_id=result.id,
    )
    db.session.commit()
    current_app.logger.info("Optimization CSV export for %s by %s", form.scenario_id.data, current_user.email)
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=filename)


@ops_bp.route("/exports/optimization/pdf", methods=["POST"])
@login_required
@tenant_required
def export_optimization_pdf():
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas

    org_id = _org_id()
    org = Organization.query.get(org_id)
    form = OptimizationExportForm()
    form.scenario_id.choices = _scenario_choices(org_id)

    if not form.validate_on_submit():
        flash("Select a scenario and at least one section to export.", "danger")
        return redirect(url_for("ops.network", _anchor="optimization"))

    result = (
        OptimizationResult.for_org(org_id)
        .filter_by(scenario_id=form.scenario_id.data)
        .order_by(OptimizationResult.created_at.desc())
        .first()
    )
    if result is None:
        flash("No optimization output available for that scenario yet.", "warning")
        return redirect(url_for("ops.network", _anchor="optimization"))

    ui = (result.kpis or {}).get("ui_views", {}) or {}
    summary = ui.get("summary", {}) or {}
    sections = set(form.sections.data or [])

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    margin = 0.6 * inch

    theme = {
        "bg": colors.HexColor("#0b1220"),
        "panel": colors.HexColor("#0f172a"),
        "accent": colors.HexColor("#22c55e"),
        "accent_two": colors.HexColor("#06b6d4"),
        "muted": colors.HexColor("#94a3b8"),
        "border": colors.HexColor("#1f2937"),
        "danger": colors.HexColor("#f97316"),
    }

    def draw_bg():
        c.setFillColor(theme["bg"])
        c.rect(0, 0, width, height, stroke=0, fill=1)

    def header(title: str, subtitle: str) -> float:
        bar_h = 74
        c.setFillColor(theme["panel"])
        c.roundRect(margin, height - margin - bar_h, width - 2 * margin, bar_h, 16, stroke=0, fill=1)
        c.setStrokeColor(theme["border"])
        c.setLineWidth(1.1)
        c.roundRect(margin, height - margin - bar_h, width - 2 * margin, bar_h, 16, stroke=1, fill=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(margin + 16, height - margin - 26, title)
        c.setFont("Helvetica", 10)
        c.setFillColor(theme["muted"])
        c.drawString(margin + 16, height - margin - 42, subtitle)
        c.setFont("Helvetica", 9)
        c.setFillColor(colors.white)
        c.drawString(margin + 16, height - margin - 58, f"Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
        return height - margin - bar_h - 12

    def stat_card(x: float, y: float, w: float, h: float, label: str, value: str, accent) -> None:
        c.setFillColor(theme["panel"])
        c.roundRect(x, y - h, w, h, 12, stroke=0, fill=1)
        c.setStrokeColor(accent)
        c.setLineWidth(1)
        c.roundRect(x, y - h, w, h, 12, stroke=1, fill=0)
        c.setFillColor(theme["muted"])
        c.setFont("Helvetica", 9)
        c.drawString(x + 12, y - 16, label)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(x + 12, y - 36, value)

    def section_title(y_pos: float, label: str, accent) -> float:
        c.setFillColor(accent)
        c.rect(margin, y_pos - 4, 6, 18, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(margin + 14, y_pos + 10, label)
        return y_pos - 18

    def draw_table(y_pos: float, headers: list[str], rows: list[list], accent) -> float:
        c.setFillColor(theme["panel"])
        box_h = 18 + 14 * min(len(rows), 14)
        c.roundRect(margin, y_pos - box_h, width - 2 * margin, box_h, 10, stroke=0, fill=1)
        c.setStrokeColor(theme["border"])
        c.setLineWidth(0.8)
        c.roundRect(margin, y_pos - box_h, width - 2 * margin, box_h, 10, stroke=1, fill=0)

        c.setFillColor(accent)
        c.setFont("Helvetica-Bold", 9)
        col_w = (width - 2 * margin - 12) / len(headers)
        for idx, header_label in enumerate(headers):
            c.drawString(margin + 6 + idx * col_w, y_pos - 12, header_label)

        c.setFont("Helvetica", 8)
        c.setFillColor(colors.white)
        y_row = y_pos - 26
        max_rows = 12
        for row in rows[:max_rows]:
            for idx, cell in enumerate(row):
                c.drawString(margin + 6 + idx * col_w, y_row, str(cell))
            y_row -= 12
            if y_row < margin + 40:
                c.showPage()
                draw_bg()
                y_row = header("Optimization Report", "") - 14
        return y_row - 6

    draw_bg()
    scenario_label = result.scenario.scenario_name if result.scenario else f"Scenario {form.scenario_id.data}"
    status_label = getattr(result.job, "solver_status", "completed") if result.job else "completed"
    y = header(f"Optimization Report — {scenario_label}", f"Status {status_label}")

    card_w = (width - 2 * margin - 12) / 3
    card_h = 64
    objective_val = summary.get("objective", result.total_cost)
    service_level = summary.get("service_level_pct", 0)
    runtime_val = summary.get("runtime_seconds", 0)
    stat_card(margin, y, card_w, card_h, "Objective", f"₹{objective_val:.2f}", theme["accent"])
    stat_card(margin + card_w + 6, y, card_w, card_h, "Service level", f"{service_level}%", theme["accent_two"])
    stat_card(margin + 2 * (card_w + 6), y, card_w, card_h, "Runtime (s)", f"{runtime_val:.2f}", theme["danger"])
    y -= card_h + 16

    if "summary" in sections:
        costs = summary.get("costs", {}) or {}
        y = section_title(y, "Cost breakdown", theme["accent_two"])
        table_rows = [
            ["Production", f"₹{(costs.get('production') or 0):.2f}"],
            ["Transport", f"₹{(costs.get('transport') or 0):.2f}"],
            ["Holding", f"₹{(costs.get('holding') or 0):.2f}"],
            ["Total", f"₹{(costs.get('total', objective_val) or 0):.2f}"],
        ]
        y = draw_table(y, ["Component", "Value"], table_rows, theme["accent_two"]) - 8

    if "dispatch" in sections:
        dispatch_rows = ui.get("dispatch", []) or []
        y = section_title(y, "Dispatch plan", theme["accent"])
        if dispatch_rows:
            table_rows = [
                [
                    row.get("period"),
                    row.get("from"),
                    row.get("to"),
                    row.get("mode"),
                    round(row.get("trips", 0), 2),
                    round(row.get("qty", 0), 2),
                ]
                for row in dispatch_rows
            ]
            y = draw_table(y, ["P", "From", "To", "Mode", "Trips", "Qty"], table_rows, theme["accent"])
        else:
            y = draw_table(y, ["Info"], [["No dispatch plan available"]], theme["accent"])
        y -= 6

    if "production" in sections:
        production_rows = ui.get("production", []) or []
        y = section_title(y, "Production plan", theme["accent_two"])
        if production_rows:
            table_rows = [
                [
                    row.get("code"),
                    row.get("period"),
                    round(row.get("produced", 0), 2),
                    round(row.get("capacity", 0), 2),
                    round(row.get("utilization_pct", 0), 2),
                ]
                for row in production_rows
            ]
            y = draw_table(y, ["IU", "P", "Produced", "Capacity", "Util %"], table_rows, theme["accent_two"])
        else:
            y = draw_table(y, ["Info"], [["No production plan available"]], theme["accent_two"])
        y -= 6

    if "inventory" in sections:
        inventory_rows = ui.get("inventory", []) or []
        y = section_title(y, "Inventory ledger", theme["danger"])
        if inventory_rows:
            table_rows = [
                [
                    row.get("code"),
                    row.get("period"),
                    round(row.get("opening", 0), 2),
                    round(row.get("closing", 0), 2),
                    round(row.get("min_close", 0), 2),
                    row.get("max_close") if row.get("max_close") is not None else "—",
                ]
                for row in inventory_rows
            ]
            y = draw_table(y, ["Plant", "P", "Open", "Close", "Min", "Max"], table_rows, theme["danger"])
        else:
            y = draw_table(y, ["Info"], [["No inventory ledger available"]], theme["danger"])

    c.showPage()
    c.save()
    buf.seek(0)

    filename = f"optimization-{_safe_org_slug(org)}-scenario-{form.scenario_id.data}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.pdf"
    _log_activity(
        org_id,
        current_user.id,
        "export_optimization_pdf",
        f"Optimization PDF for scenario {form.scenario_id.data}",
        entity_type="optimization_result",
        entity_id=result.id,
    )
    db.session.commit()
    current_app.logger.info("Optimization PDF export for %s by %s", form.scenario_id.data, current_user.email)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=filename)


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


