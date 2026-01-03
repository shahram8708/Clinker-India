"""Tenant admin surfaces for user management and governance."""
import csv
import io
from flask import Blueprint, Response, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_

from ..auth.forms import UserRoleUpdateForm, UserStatusUpdateForm
from ..extensions import db
from ..models import OrganizationSubscription, PricingPlan, User, UserInvitation
from ..tenant.utils import admin_required, tenant_required


def _active_admin_count(org_id: int) -> int:
    return (
        User.query.filter_by(organization_id=org_id, is_active=True)
        .filter(User.role.in_(["owner", "admin"]))
        .count()
    )


def _users_base_query(org_id: int):
    return User.query.filter_by(organization_id=org_id)


def _build_users_query(org_id: int, query: str, role_filter: str, status_filter: str):
    """Apply the same filters used by the listing UI for reuse in exports."""
    users_query = _users_base_query(org_id)
    if query:
        like = f"%{query.lower()}%"
        users_query = users_query.filter(
            or_(User.email.ilike(like), User.full_name.ilike(like))
        )
    if role_filter:
        users_query = users_query.filter_by(role=role_filter)
    if status_filter:
        if status_filter == "disabled":
            users_query = users_query.filter(User.is_active.is_(False))
        elif status_filter == "active":
            users_query = users_query.filter(User.is_active.is_(True))
        elif status_filter == "pending":
            users_query = users_query.filter(User.lifecycle_state == "pending")
    return users_query


def _redirect_to_users():
    return redirect(url_for("tenant.user_management"))


tenant_bp = Blueprint("tenant", __name__, template_folder="../templates/tenant")


@tenant_bp.route("/users")
@login_required
@tenant_required
@admin_required
def user_management():
    org_id = session.get("org_id") or current_user.organization_id
    query = (request.args.get("q") or "").strip()
    role_filter = request.args.get("role") or ""
    status_filter = request.args.get("status") or ""

    users_query = _build_users_query(org_id, query, role_filter, status_filter)
    users = users_query.order_by(User.created_at.desc()).all()
    invitations = (
        UserInvitation.query.filter_by(organization_id=org_id, status="pending")
        .order_by(UserInvitation.created_at.desc())
        .all()
    )

    plan = PricingPlan.query.filter_by(is_active=True).first()
    subscription = OrganizationSubscription.bootstrap(org_id, plan)
    subscription.refresh_status()
    seat_summary = {
        "status": subscription.status,
        "seat_limit": subscription.seat_limit,
        "remaining_seats": subscription.remaining_seats,
        "paid_seats": subscription.paid_seats,
    }
    db.session.commit()

    role_form = UserRoleUpdateForm()
    status_form = UserStatusUpdateForm()

    return render_template(
        "tenant/user_management.html",
        users=users,
        invitations=invitations,
        filters={"q": query, "role": role_filter, "status": status_filter},
        role_form=role_form,
        status_form=status_form,
        seat_summary=seat_summary,
    )


@tenant_bp.route("/users/export", methods=["GET"])
@login_required
@tenant_required
@admin_required
def export_users_csv():
    org_id = session.get("org_id") or current_user.organization_id
    query = (request.args.get("q") or "").strip()
    role_filter = request.args.get("role") or ""
    status_filter = request.args.get("status") or ""

    users_query = _build_users_query(org_id, query, role_filter, status_filter)
    users = users_query.order_by(User.created_at.desc()).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Name", "Email", "Role", "Status", "Created"])
    for user in users:
        writer.writerow(
            [
                user.full_name or "",
                user.email,
                user.role,
                getattr(user, "status_label", ""),
                user.created_at.strftime("%Y-%m-%d %H:%M:%S")
                if getattr(user, "created_at", None)
                else "",
            ]
        )

    csv_bytes = buffer.getvalue()
    buffer.close()

    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=tenant-users.csv",
            "Cache-Control": "no-cache",
        },
    )


@tenant_bp.route("/invitations/export", methods=["GET"])
@login_required
@tenant_required
@admin_required
def export_invitations_csv():
    org_id = session.get("org_id") or current_user.organization_id
    invitations = (
        UserInvitation.query.filter_by(organization_id=org_id, status="pending")
        .order_by(UserInvitation.created_at.desc())
        .all()
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Email", "Role", "Expires", "Status", "Accept link"])
    for invite in invitations:
        writer.writerow(
            [
                invite.email,
                invite.role,
                invite.expires_at.strftime("%Y-%m-%d %H:%M:%S")
                if getattr(invite, "expires_at", None)
                else "",
                invite.status,
                url_for("auth.accept_invite", token=invite.token, _external=True),
            ]
        )

    csv_bytes = buffer.getvalue()
    buffer.close()

    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=tenant-invitations.csv",
            "Cache-Control": "no-cache",
        },
    )


@tenant_bp.route("/users/<int:user_id>/role", methods=["POST"])
@login_required
@tenant_required
@admin_required
def update_user_role(user_id: int):
    org_id = session.get("org_id") or current_user.organization_id
    form = UserRoleUpdateForm()
    if not form.validate_on_submit():
        flash("Invalid role update request.", "warning")
        return _redirect_to_users()

    target = _users_base_query(org_id).filter_by(id=user_id).first_or_404()

    if target.id == current_user.id and target.role == "owner":
        flash("You cannot change your own owner role.", "warning")
        return _redirect_to_users()

    new_role = form.role.data
    if new_role == "owner" and current_user.role != "owner":
        flash("Only owners can assign the owner role.", "danger")
        return _redirect_to_users()

    admin_roles = {"owner", "admin"}
    admin_count = _active_admin_count(org_id)

    if target.role in admin_roles and new_role not in admin_roles and admin_count <= 1:
        flash("At least one admin must remain in the organization.", "danger")
        return _redirect_to_users()

    target.role = new_role
    db.session.commit()
    flash("Role updated.", "success")
    return _redirect_to_users()


@tenant_bp.route("/users/<int:user_id>/status", methods=["POST"])
@login_required
@tenant_required
@admin_required
def update_user_status(user_id: int):
    org_id = session.get("org_id") or current_user.organization_id
    form = UserStatusUpdateForm()
    if not form.validate_on_submit():
        flash("Invalid status update request.", "warning")
        return _redirect_to_users()

    target = _users_base_query(org_id).filter_by(id=user_id).first_or_404()
    if target.id == current_user.id:
        flash("You cannot disable your own account.", "warning")
        return _redirect_to_users()

    desired_status = form.status.data
    admin_roles = {"owner", "admin"}
    admin_count = _active_admin_count(org_id)

    if target.role in admin_roles and desired_status == "disabled" and admin_count <= 1:
        flash("At least one admin must remain active in the organization.", "danger")
        return _redirect_to_users()

    if desired_status == "active":
        target.activate()
    else:
        target.disable()

    db.session.commit()
    flash("User status updated.", "success")
    return _redirect_to_users()


@tenant_bp.route("/invitations/<int:invitation_id>/revoke", methods=["POST"])
@login_required
@tenant_required
@admin_required
def revoke_invitation(invitation_id: int):
    org_id = session.get("org_id") or current_user.organization_id
    invitation = (
        UserInvitation.query.filter_by(id=invitation_id, organization_id=org_id)
        .first_or_404()
    )

    if invitation.status != "pending":
        flash("Invitation is already finalized.", "info")
        return _redirect_to_users()

    invitation.revoke()
    db.session.commit()
    flash("Invitation revoked.", "success")
    return _redirect_to_users()
