"""Super admin blueprint for platform-wide control."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
from statistics import mean

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from ..extensions import db
from ..models import (
    ActivityLog,
    ContactRequest,
    Inventory,
    Notification,
    OptimizationJob,
    Organization,
    OrganizationSubscription,
    PlanningScenario,
    Plant,
    SeatPurchase,
    TransportRoute,
    User,
    UserInvitation,
)

superadmin_bp = Blueprint("superadmin", __name__, template_folder="../templates/superadmin")


def super_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)
        if not getattr(current_user, "is_super_admin", False):
            abort(403)
        return view(*args, **kwargs)

    return login_required(wrapped)


@superadmin_bp.before_request
def _guard_super_admin():
    if not current_user.is_authenticated:
        abort(401)
    if not getattr(current_user, "is_super_admin", False):
        abort(403)


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


def _bucket_monthly_sum(records, attr: str, value_attr: str | None = None, months: int = 6) -> dict:
    now = datetime.utcnow()
    first_day_this_month = datetime(now.year, now.month, 1)
    start_month = first_day_this_month - timedelta(days=30 * (months - 1))
    labels = []
    sums: dict[str, float] = {}
    cursor = start_month
    for _ in range(months):
        label = cursor.strftime("%Y-%m")
        labels.append(label)
        sums[label] = 0.0
        next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        cursor = next_month

    for item in records:
        dt = getattr(item, attr, None)
        if not dt or not hasattr(dt, "strftime"):
            continue
        label = dt.strftime("%Y-%m")
        if label in sums:
            if value_attr:
                sums[label] += _safe_float(getattr(item, value_attr, 0))
            else:
                sums[label] += 1

    return {"labels": labels, "values": [round(sums[label], 2) for label in labels]}


def _build_superadmin_analytics(lookback_days: int = 120) -> dict:
    now = datetime.utcnow()
    window_start = now - timedelta(days=lookback_days)
    recent_window = now - timedelta(days=30)

    orgs = Organization.query.all()
    users = User.query.all()
    invites = UserInvitation.query.all()
    subscriptions = OrganizationSubscription.query.all()
    purchases = SeatPurchase.query.order_by(SeatPurchase.created_at.desc()).limit(50).all()
    activities = ActivityLog.query.filter(ActivityLog.created_at >= recent_window).all()
    notifications = Notification.query.filter(Notification.created_at >= recent_window).all()
    plants = Plant.query.all()
    routes = TransportRoute.query.all()
    inventories = Inventory.query.all()
    scenarios = PlanningScenario.query.all()
    jobs = OptimizationJob.query.order_by(OptimizationJob.id.desc()).limit(80).all()
    tickets = ContactRequest.query.order_by(ContactRequest.created_at.desc()).limit(30).all()

    org_status = defaultdict(int)
    onboarding_pending = 0
    org_growth = _bucket_weekly(orgs, "created_at", window_start)
    org_recent = 0
    for org in orgs:
        org_status[org.status] += 1
        if not org.onboarding_completed_at:
            onboarding_pending += 1
        if org.created_at and org.created_at >= now - timedelta(days=30):
            org_recent += 1

    role_counts = defaultdict(int)
    lifecycle_counts = defaultdict(int)
    user_growth = _bucket_weekly(users, "created_at", window_start)
    for user in users:
        role_counts[user.role] += 1
        lifecycle_counts[user.lifecycle_state] += 1

    activities_by_org = defaultdict(int)
    activity_by_severity = defaultdict(int)
    for log in activities:
        activities_by_org[log.organization_id] += 1
        activity_by_severity[log.severity] += 1
    top_active_orgs = sorted(
        (
            {
                "org_id": org_id,
                "label": next((o.name for o in orgs if o.id == org_id), f"Org {org_id}"),
                "value": count,
            }
            for org_id, count in activities_by_org.items()
        ),
        key=lambda item: item["value"],
        reverse=True,
    )[:8]

    notifications_by_severity = defaultdict(int)
    for note in notifications:
        notifications_by_severity[note.severity] += 1

    subscription_status = defaultdict(int)
    plan_mix = defaultdict(int)
    seat_capacity = 0
    seat_remaining = 0
    seat_utilization_by_org = []
    for sub in subscriptions:
        subscription_status[sub.status] += 1
        seat_capacity += sub.seat_limit
        seat_remaining += sub.remaining_seats
        used = sub.seat_limit - sub.remaining_seats
        seat_utilization_by_org.append(
            {
                "label": next((o.name for o in orgs if o.id == sub.organization_id), f"Org {sub.organization_id}"),
                "used": used,
                "remaining": sub.remaining_seats,
            }
        )
        plan_label = sub.plan.name if sub.plan else "Trial"
        plan_mix[plan_label] += 1

    seat_used = seat_capacity - seat_remaining if seat_capacity else 0
    seat_utilization_by_org = sorted(seat_utilization_by_org, key=lambda row: row["used"], reverse=True)[:8]

    revenue_total = _safe_float(
        db.session.query(func.coalesce(func.sum(SeatPurchase.amount_total), 0)).filter_by(status="paid").scalar()
    )
    revenue_trend = _bucket_monthly_sum(purchases, "created_at", "amount_total", months=6)

    purchase_events = [
        {
            "label": next((o.name for o in orgs if o.id == p.organization_id), f"Org {p.organization_id}"),
            "amount": _safe_float(p.amount_total),
            "status": p.status,
            "paid_at": p.paid_at.isoformat() if p.paid_at else None,
            "currency": p.currency,
        }
        for p in purchases[:10]
    ]

    inventory_alerts = [inv for inv in inventories if inv.below_safety]
    inventory_safe = [inv for inv in inventories if not inv.below_safety]
    routes_by_mode = defaultdict(int)
    for route in routes:
        routes_by_mode[route.mode] += 1

    scenarios_by_status = defaultdict(int)
    scenario_costs = []
    for scenario in scenarios:
        scenarios_by_status[scenario.status] += 1
        if scenario.result_cost is not None:
            scenario_costs.append({"label": scenario.scenario_name, "value": _safe_float(scenario.result_cost)})

    job_status_counts = defaultdict(int)
    job_runtime = []
    for job in jobs:
        job_status_counts[job.status] += 1
        started = job.started_at or job.completed_at or now
        runtime = _safe_float(job.runtime_seconds)
        if runtime <= 0 and job.started_at and job.completed_at:
            runtime = _safe_float((job.completed_at - job.started_at).total_seconds())
        job_runtime.append({"label": started.strftime("%m-%d"), "runtime": runtime})

    open_tickets = sum(1 for t in tickets if t.status == "open")
    ack_tickets = sum(1 for t in tickets if t.status == "acknowledged")

    return {
        "generated_at": now.isoformat(),
        "window_start": window_start.date().isoformat(),
        "kpis": {
            "org_total": len(orgs),
            "org_active": org_status.get("active", 0),
            "org_suspended": org_status.get("suspended", 0),
            "org_new_30": org_recent,
            "onboarding_pending": onboarding_pending,
            "user_total": len(users),
            "user_active": lifecycle_counts.get("active", 0),
            "seat_capacity": seat_capacity,
            "seat_used": seat_used,
            "revenue_total": revenue_total,
            "inventory_alerts": len(inventory_alerts),
            "open_tickets": open_tickets,
            "jobs_running": job_status_counts.get("running", 0),
            "avg_runtime": round(mean([row["runtime"] for row in job_runtime]) if job_runtime else 0, 2),
        },
        "orgs": {
            "growth": org_growth,
            "status": dict(org_status),
            "top_activity": top_active_orgs,
            "onboarding_pending": onboarding_pending,
        },
        "users": {
            "growth": user_growth,
            "roles": dict(role_counts),
            "lifecycle": dict(lifecycle_counts),
            "activity_by_org": top_active_orgs,
        },
        "seats": {
            "status": dict(subscription_status),
            "plan_mix": dict(plan_mix),
            "utilization_by_org": seat_utilization_by_org,
            "seat_capacity": seat_capacity,
            "seat_used": seat_used,
            "seat_remaining": seat_remaining,
        },
        "billing": {
            "revenue_trend": revenue_trend,
            "purchases": purchase_events,
            "total_revenue": revenue_total,
        },
        "invites": {
            "status": {status: sum(1 for inv in invites if inv.status == status) for status in ["pending", "accepted", "expired", "revoked"]},
        },
        "operations": {
            "plants": {"total": len(plants), "iu": sum(1 for p in plants if p.plant_type == "IU"), "gu": sum(1 for p in plants if p.plant_type == "GU")},
            "routes": {"by_mode": dict(routes_by_mode), "active": sum(1 for r in routes if r.status == "active"), "total": len(routes)},
            "inventory": {"alerts": len(inventory_alerts), "safe": len(inventory_safe)},
            "scenarios": {"by_status": dict(scenarios_by_status), "costs": scenario_costs[:12]},
            "optimization": {"statuses": dict(job_status_counts), "runtime": job_runtime[:15]},
        },
        "security": {
            "activity": dict(activity_by_severity),
            "notifications": dict(notifications_by_severity),
        },
        "support": {
            "open": open_tickets,
            "acknowledged": ack_tickets,
            "recent": [
                {
                    "label": t.subject,
                    "status": t.status,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                }
                for t in tickets[:6]
            ],
        },
    }


@superadmin_bp.route("/superadmin")
@super_admin_required
def dashboard():
    orgs = Organization.query.order_by(Organization.created_at.desc()).all()
    users = User.query.order_by(User.created_at.desc()).limit(12).all()
    tickets = ContactRequest.query.order_by(ContactRequest.created_at.desc()).limit(10).all()
    activities = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(15).all()

    metrics = {
        "org_total": len(orgs),
        "org_active": len([o for o in orgs if o.status == "active"]),
        "org_suspended": len([o for o in orgs if o.status == "suspended"]),
        "user_total": User.query.count(),
        "open_tickets": ContactRequest.query.filter_by(status="open").count(),
        "paid_orgs": OrganizationSubscription.query.filter(OrganizationSubscription.paid_seats > 0).count(),
    }

    subscriptions = {
        org.id: OrganizationSubscription.query.filter_by(organization_id=org.id).first()
        for org in orgs
    }

    recent_payments = SeatPurchase.query.order_by(SeatPurchase.created_at.desc()).limit(8).all()

    return render_template(
        "superadmin/dashboard.html",
        orgs=orgs,
        users=users,
        tickets=tickets,
        metrics=metrics,
        subscriptions=subscriptions,
        activities=activities,
        recent_payments=recent_payments,
    )


@superadmin_bp.route("/superadmin/analytics")
@super_admin_required
def analytics():
    analytics_payload = _build_superadmin_analytics()
    return render_template(
        "superadmin/analytics.html",
        analytics=analytics_payload,
        now=datetime.utcnow(),
    )


@superadmin_bp.route("/superadmin/organizations/<int:org_id>/status", methods=["POST"])
@super_admin_required
def update_org_status(org_id: int):
    desired = request.form.get("status")
    organization = Organization.query.get_or_404(org_id)
    if desired == "suspended":
        organization.suspend()
        flash("Organization suspended.", "warning")
    elif desired == "active":
        organization.activate()
        flash("Organization reactivated.", "success")
    else:
        flash("Invalid status change requested.", "danger")
        return redirect(url_for("superadmin.dashboard"))

    db.session.commit()
    return redirect(url_for("superadmin.dashboard"))


@superadmin_bp.route("/superadmin/users/<int:user_id>/force-verify", methods=["POST"])
@super_admin_required
def force_verify_user(user_id: int):
    user = User.query.get_or_404(user_id)
    user.mark_email_verified()
    user.activate()
    db.session.commit()
    flash("User verified and activated.", "success")
    return redirect(url_for("superadmin.dashboard"))


@superadmin_bp.route("/superadmin/support/<int:ticket_id>/ack", methods=["POST"])
@super_admin_required
def acknowledge_ticket(ticket_id: int):
    ticket = ContactRequest.query.get_or_404(ticket_id)
    ticket.mark_acknowledged()
    db.session.commit()
    flash("Ticket acknowledged.", "success")
    return redirect(url_for("superadmin.dashboard"))


@superadmin_bp.route("/superadmin/support/<int:ticket_id>/close", methods=["POST"])
@super_admin_required
def close_ticket(ticket_id: int):
    ticket = ContactRequest.query.get_or_404(ticket_id)
    ticket.close()
    db.session.commit()
    flash("Ticket closed.", "info")
    return redirect(url_for("superadmin.dashboard"))
