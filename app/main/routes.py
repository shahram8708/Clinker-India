"""Main blueprint for dashboard, landing, analytics, and in-app AI chat."""
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean
from sqlalchemy import text

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from flask_mail import Message

from ..extensions import db, mail
from ..models import (
    ActivityLog,
    ContactRequest,
    Inventory,
    Notification,
    Organization,
    OrganizationSubscription,
    OptimizationJob,
    PlanningScenario,
    Plant,
    TransportRoute,
    User,
    UserInvitation,
)
from ..tenant.utils import tenant_required
from .forms import SUPPORT_CATEGORIES, ContactSupportForm
from .chat_service import MissingAPIKey, generate_chat_reply

main_bp = Blueprint("main", __name__, template_folder="../templates")


@main_bp.route("/")
def index():
    return redirect(url_for("main.dashboard"))


@main_bp.route("/dashboard")
@login_required
@tenant_required
def dashboard():
    org_id = current_user.organization_id
    organization = Organization.query.get(org_id)

    pending_invites = _pending_invites_for_email(current_user.email)

    metrics = {
        "active_users": User.query.filter_by(organization_id=org_id, is_active=True).count(),
        "pending_invites": UserInvitation.query.filter_by(organization_id=org_id, status="pending").count(),
        "total_users": User.query.filter_by(organization_id=org_id).count(),
        "org_status": organization.status if organization else "unknown",
        "total_plants": Plant.for_org(org_id).count(),
        "iu_count": Plant.for_org(org_id).filter_by(plant_type="IU").count(),
        "gu_count": Plant.for_org(org_id).filter_by(plant_type="GU").count(),
        "active_routes": TransportRoute.for_org(org_id).filter_by(status="active").count(),
        "scenario_count": PlanningScenario.for_org(org_id).count(),
    }

    admin_snapshot = {
        "pending_invites": metrics["pending_invites"],
        "org_status": metrics["org_status"],
    }

    inventory_records = (
        Inventory.for_org(org_id)
        .join(Plant)
        .order_by(Plant.plant_name)
        .all()
    )
    inventory_alerts = [rec for rec in inventory_records if rec.below_safety]

    routes = (
        TransportRoute.for_org(org_id)
        .order_by(TransportRoute.created_at.desc())
        .limit(5)
        .all()
    )

    plants = (
        Plant.for_org(org_id)
        .order_by(Plant.created_at.desc())
        .limit(5)
        .all()
    )

    scenarios = (
        PlanningScenario.for_org(org_id)
        .order_by(PlanningScenario.created_at.desc())
        .limit(3)
        .all()
    )

    greeting = {
        "user_name": current_user.full_name or current_user.email,
        "org_name": organization.name if organization else "Your Organization",
        "role": current_user.role,
        "timestamp": datetime.utcnow(),
    }

    return render_template(
        "dashboard/index.html",
        metrics=metrics,
        admin_snapshot=admin_snapshot,
        greeting=greeting,
        pending_invites=pending_invites,
        inventory_alerts=inventory_alerts,
        routes=routes,
        plants=plants,
        scenarios=scenarios,
    )


@main_bp.route("/analytics")
@login_required
@tenant_required
def analytics():
    org_id = current_user.organization_id
    analytics_payload = _build_analytics_payload(org_id)
    return render_template(
        "dashboard/analytics.html",
        analytics=analytics_payload,
        now=datetime.utcnow(),
    )


def _pending_invites_for_email(email: str):
    """Fetch pending invitations for the logged-in user's email across orgs."""
    sql = text(
        """
        SELECT ui.id, ui.organization_id, ui.full_name, ui.role, ui.expires_at, ui.status,
               org.name AS org_name, org.status AS org_status
        FROM user_invitations ui
        JOIN organizations org ON org.id = ui.organization_id
        WHERE ui.email = :email
          AND ui.status = 'pending'
          AND ui.expires_at >= :now
        ORDER BY ui.created_at DESC
        LIMIT 5
        """
    )
    rows = db.session.execute(sql, {"email": email.lower(), "now": datetime.utcnow()}).mappings().all()

    invites = []
    for row in rows:
        expires_raw = row["expires_at"]
        expires_dt = None
        if hasattr(expires_raw, "strftime"):
            expires_dt = expires_raw
        else:
            try:
                expires_dt = datetime.fromisoformat(str(expires_raw))
            except Exception:
                expires_dt = None

        invites.append({
            "id": row["id"],
            "organization_id": row["organization_id"],
            "full_name": row["full_name"],
            "role": row["role"],
            "status": row["status"],
            "org_name": row["org_name"],
            "org_status": row["org_status"],
            "expires_at": expires_dt,
            "expires_label": expires_dt.strftime("%Y-%m-%d") if expires_dt else str(expires_raw),
        })

    return invites


def _safe_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bucket_weekly(records, attr: str, window_start: datetime) -> dict:
    buckets: dict[str, int] = defaultdict(int)
    for item in records:
        dt = getattr(item, attr, None)
        if not dt or not hasattr(dt, "strftime"):
            continue
        if dt < window_start:
            continue
        label = dt.strftime("%Y-W%U")
        buckets[label] += 1
    labels = sorted(buckets.keys())
    return {"labels": labels, "values": [buckets[label] for label in labels]}


def _bucket_daily(records, attr: str, days: int = 14) -> dict:
    now = datetime.utcnow()
    start = now - timedelta(days=days - 1)
    labels = [(start + timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(days)]
    counts = {label: 0 for label in labels}
    for item in records:
        dt = getattr(item, attr, None)
        if not dt or not hasattr(dt, "strftime"):
            continue
        label = dt.strftime("%Y-%m-%d")
        if label in counts:
            counts[label] += 1
    return {"labels": labels, "values": [counts[label] for label in labels]}


def _build_analytics_payload(org_id: int, lookback_days: int = 90) -> dict:
    now = datetime.utcnow()
    window_start = now - timedelta(days=lookback_days)

    users = User.for_org(org_id).all()
    invites = UserInvitation.for_org(org_id).all()
    plants = Plant.for_org(org_id).all()
    routes = TransportRoute.for_org(org_id).all()
    inventories = Inventory.for_org(org_id).all()
    scenarios = PlanningScenario.for_org(org_id).order_by(PlanningScenario.created_at.desc()).all()
    jobs = (
        OptimizationJob.for_org(org_id)
        .order_by(OptimizationJob.id.desc())
        .limit(30)
        .all()
    )
    activities = (
        ActivityLog.for_org(org_id)
        .filter(ActivityLog.created_at >= now - timedelta(days=30))
        .all()
    )
    notifications = (
        Notification.for_org(org_id)
        .filter(Notification.created_at >= now - timedelta(days=30))
        .all()
    )
    subscription = OrganizationSubscription.query.filter_by(organization_id=org_id).first()

    role_counts: dict[str, int] = defaultdict(int)
    user_lifecycle: dict[str, int] = defaultdict(int)
    for user in users:
        role_counts[user.role] += 1
        user_lifecycle[user.lifecycle_state] += 1

    user_growth = _bucket_weekly(users, "created_at", window_start)
    invite_velocity = _bucket_weekly(invites, "created_at", window_start)

    inventory_alerts = [inv for inv in inventories if inv.below_safety]
    inventory_safe = [inv for inv in inventories if not inv.below_safety]
    inventory_top_risk = sorted(
        inventories,
        key=lambda inv: _safe_float(inv.current_inventory) - _safe_float(getattr(inv.plant, "safety_stock_level", 0)),
    )[:6]

    routes_by_mode: dict[str, int] = defaultdict(int)
    for route in routes:
        routes_by_mode[route.mode] += 1
    routes_active = sum(1 for route in routes if route.status == "active")
    avg_route_cost = mean([_safe_float(route.cost_per_trip) for route in routes]) if routes else 0.0

    scenarios_by_status: dict[str, int] = defaultdict(int)
    scenario_costs = []
    for scenario in scenarios:
        scenarios_by_status[scenario.status] += 1
        if scenario.result_cost is not None and scenario.status in {"executed", "completed"}:
            scenario_costs.append(
                {
                    "label": scenario.scenario_name,
                    "value": _safe_float(scenario.result_cost),
                    "status": scenario.status,
                }
            )

    job_status_counts: dict[str, int] = defaultdict(int)
    job_runtime = []
    for job in jobs:
        job_status_counts[job.status] += 1
        started = job.started_at or job.completed_at or now
        runtime = _safe_float(job.runtime_seconds)
        if runtime <= 0 and job.started_at and job.completed_at:
            runtime = _safe_float((job.completed_at - job.started_at).total_seconds())
        job_runtime.append(
            {
                "label": started.strftime("%m-%d"),
                "runtime": runtime,
                "status": job.status,
            }
        )

    activity_by_severity: dict[str, int] = defaultdict(int)
    for log in activities:
        activity_by_severity[log.severity] += 1

    notifications_by_severity: dict[str, int] = defaultdict(int)
    for note in notifications:
        notifications_by_severity[note.severity] += 1

    seat_limit = subscription.seat_limit if subscription else 0
    remaining_seats = subscription.remaining_seats if subscription else 0
    used_seats = seat_limit - remaining_seats if subscription else 0

    return {
        "kpis": {
            "active_users": user_lifecycle.get("active", 0),
            "pending_users": user_lifecycle.get("pending", 0),
            "pending_invites": sum(1 for invite in invites if invite.status == "pending"),
            "plants": len(plants),
            "routes": len(routes),
            "inventory_alerts": len(inventory_alerts),
            "scenarios": len(scenarios),
            "optimization_jobs": len(jobs),
        },
        "users": {
            "roles": dict(role_counts),
            "lifecycle": dict(user_lifecycle),
            "growth": user_growth,
        },
        "invites": {
            "velocity": invite_velocity,
            "status": {status: sum(1 for inv in invites if inv.status == status) for status in ["pending", "accepted", "expired", "revoked"]},
        },
        "operations": {
            "plants": {
                "iu": sum(1 for plant in plants if plant.plant_type == "IU"),
                "gu": sum(1 for plant in plants if plant.plant_type == "GU"),
            },
            "routes": {
                "by_mode": dict(routes_by_mode),
                "active": routes_active,
                "avg_cost": avg_route_cost,
            },
            "inventory": {
                "safe": len(inventory_safe),
                "alerts": len(inventory_alerts),
                "average_utilization": mean([inv.utilization_pct for inv in inventories]) if inventories else 0,
                "top_risk": [
                    {
                        "label": inv.plant.plant_name if inv.plant else inv.plant_id,
                        "current": _safe_float(inv.current_inventory),
                        "safety": _safe_float(getattr(inv.plant, "safety_stock_level", 0)),
                        "utilization": inv.utilization_pct,
                    }
                    for inv in inventory_top_risk
                ],
            },
            "scenarios": {
                "by_status": dict(scenarios_by_status),
                "costs": scenario_costs[:10],
            },
            "optimization": {
                "statuses": dict(job_status_counts),
                "runtime": job_runtime,
            },
        },
        "activity": {
            "by_severity": dict(activity_by_severity),
            "trend": _bucket_daily(activities, "created_at", days=14),
        },
        "notifications": dict(notifications_by_severity),
        "seats": {
            "limit": seat_limit,
            "remaining": remaining_seats,
            "used": used_seats,
            "status": subscription.status if subscription else "unknown",
        },
        "generated_at": now.isoformat(),
        "window_start": window_start.date().isoformat(),
    }


@main_bp.route("/invitations/<int:invitation_id>/accept-self", methods=["POST"])
@login_required
def accept_self_invitation(invitation_id: int):
    """Allow a logged-in user to accept an invite from their dashboard."""
    now = datetime.utcnow()
    invite_row = db.session.execute(
        text(
            """
            SELECT ui.id, ui.organization_id, ui.email, ui.role, ui.expires_at, ui.status,
                   org.status AS org_status
            FROM user_invitations ui
            JOIN organizations org ON org.id = ui.organization_id
            WHERE ui.id = :invite_id
              AND ui.email = :email
              AND ui.status = 'pending'
              AND ui.expires_at >= :now
            """
        ),
        {"invite_id": invitation_id, "email": current_user.email.lower(), "now": now},
    ).mappings().first()

    if invite_row is None:
        flash("This invitation is no longer available.", "warning")
        return redirect(url_for("main.dashboard"))

    if invite_row["org_status"] != "active":
        flash("The organization that invited you is not active.", "danger")
        return redirect(url_for("main.dashboard"))

    target_org_id = invite_row["organization_id"]

    db.session.execute(
        text("UPDATE user_invitations SET status='accepted', accepted_at=:now WHERE id=:invite_id"),
        {"now": now, "invite_id": invitation_id},
    )

    if current_user.organization_id != target_org_id:
        db.session.execute(
            text(
                """
                UPDATE users
                SET organization_id = :org_id,
                    role = :role,
                    lifecycle_state = 'active',
                    is_active = 1
                WHERE id = :user_id
                """
            ),
            {"org_id": target_org_id, "role": invite_row["role"], "user_id": current_user.id},
        )
        session["org_id"] = target_org_id
    else:
        db.session.execute(
            text(
                """
                UPDATE users
                SET role = :role,
                    lifecycle_state = 'active',
                    is_active = 1
                WHERE id = :user_id
                """
            ),
            {"role": invite_row["role"], "user_id": current_user.id},
        )

    db.session.commit()
    db.session.expire(current_user)

    flash("You joined the organization successfully.", "success")
    return redirect(url_for("main.dashboard"))


def _build_ai_context(org_id: int) -> str:
    """Assemble a short domain context for the AI with plant capacities."""
    plants = (
        Plant.for_org(org_id)
        .order_by(Plant.plant_name)
        .all()
    )

    if not plants:
        return ""

    total_production = sum(_safe_float(plant.production_capacity) for plant in plants)
    total_consumption = sum(_safe_float(plant.consumption_capacity) for plant in plants)
    total_inventory = sum(_safe_float(plant.max_inventory_capacity) for plant in plants)

    lines = [
        f"Plant summary: count={len(plants)}, total production capacity={total_production:.2f}, total consumption capacity={total_consumption:.2f}, total max inventory={total_inventory:.2f}.",
        "Plant details (trimmed):",
    ]

    max_listed = 12
    for plant in plants[:max_listed]:
        location = plant.location or "N/A"
        lines.append(
            f"{plant.plant_name} [{plant.plant_type}] in {location}: production={_safe_float(plant.production_capacity):.2f}, consumption={_safe_float(plant.consumption_capacity):.2f}, max inventory={_safe_float(plant.max_inventory_capacity):.2f}.",
        )

    if len(plants) > max_listed:
        lines.append(f"...{len(plants) - max_listed} additional plants not listed to keep context short.")

    return "\n".join(lines)


@main_bp.route("/api/chat", methods=["POST"])
@login_required
@tenant_required
def ai_chat():
    payload = request.get_json(silent=True) or {}
    messages = payload.get("messages") or []
    page_context = (payload.get("pageContext") or "").strip()
    org_context = _build_ai_context(current_user.organization_id)
    combined_context = "\n\n".join(part for part in (page_context, org_context) if part)

    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "messages array is required"}), 400
    if messages[-1].get("role") != "user":
        return jsonify({"error": "last message must come from the user"}), 400

    try:
        reply = generate_chat_reply(messages, combined_context)
    except MissingAPIKey as exc:
        current_app.logger.warning("Gemini key missing: %s", exc)
        return jsonify({"error": "AI is not configured yet."}), 503
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        current_app.logger.exception("AI chat failed")
        return jsonify({"error": "Unable to generate a reply right now."}), 500

    return jsonify({"reply": reply})


@main_bp.route("/about")
def about():
    return render_template("pages/about.html")


@main_bp.route("/terms")
def terms():
    return render_template("pages/terms.html")


@main_bp.route("/privacy")
def privacy():
    return render_template("pages/privacy.html")


@main_bp.route("/contact-support", methods=["GET", "POST"])
def contact_support():
    form = ContactSupportForm()
    if form.validate_on_submit():
        if form.is_spam():
            flash("Submission blocked. Please try again.", "warning")
            return redirect(url_for("main.contact_support"))

        contact = ContactRequest(
            full_name=form.full_name.data.strip(),
            email=form.email.data.strip(),
            subject=form.subject.data.strip(),
            category=form.category.data,
            message=form.message.data.strip(),
            request_ip=request.remote_addr,
            user_agent=(request.headers.get("User-Agent") or "")[:256],
        )

        if current_user.is_authenticated:
            contact.user_id = current_user.id
            contact.organization_id = current_user.organization_id

        db.session.add(contact)
        db.session.commit()

        try:
            admin_email = current_app.config.get("SUPPORT_ADMIN_EMAIL") or current_app.config.get("MAIL_DEFAULT_SENDER")
            if not admin_email:
                raise RuntimeError("Support admin email is not configured")

            msg = Message(
                subject=f"[Clinker Support] {dict(SUPPORT_CATEGORIES).get(contact.category, 'General')} · {contact.subject}",
                recipients=[admin_email],
                reply_to=contact.email,
            )
            msg.html = render_template("emails/support_ticket.html", contact=contact, categories=dict(SUPPORT_CATEGORIES))
            mail.send(msg)
        except Exception:
            current_app.logger.exception("Failed to send support email")
            flash("We logged your request, but email delivery failed. Our team will still review it.", "warning")
        else:
            flash("Thanks! Your request was submitted to Clinker India's support team.", "success")

        return redirect(url_for("main.contact_support"))

    return render_template("pages/support.html", form=form, categories=SUPPORT_CATEGORIES)
