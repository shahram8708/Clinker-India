"""Tenant utilities to enforce row-level isolation at the app layer."""
from functools import wraps
from typing import Any, Callable, TypeVar

from flask import abort, g, has_request_context, session
from flask_login import current_user, logout_user
from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from ..auth.super_admin import SUPER_ADMIN_SESSION_ID

Record = TypeVar("Record")


def active_org_id() -> int | None:
    """Return the current organization id from user context or session."""
    if not has_request_context():
        return None

    # Guard against recursive entry when the SQLAlchemy criteria hook
    # itself triggers user loading.
    if getattr(g, "_resolving_org_id", False):
        return session.get("org_id")

    g._resolving_org_id = True
    try:
        org_id = session.get("org_id")
        if org_id is not None:
            return org_id

        if session.get("_user_id") == SUPER_ADMIN_SESSION_ID:
            return None

        if getattr(current_user, "is_super_admin", False):
            session.pop("org_id", None)
            return None

        # Only touch the authenticated user after session checks to avoid
        # triggering the user_loader during SQLAlchemy select events.
        if getattr(current_user, "is_authenticated", False):
            org_id = getattr(current_user, "organization_id", None)
            if org_id is not None:
                session["org_id"] = org_id
            return org_id
        return None
    finally:
        g._resolving_org_id = False


def ensure_org_access(record_org_id: int | None) -> None:
    """Abort if the record does not belong to the active organization."""
    org_id = active_org_id()
    if org_id is None:
        return
    if record_org_id is not None and record_org_id != org_id:
        abort(403)


def scoped_query(model, org_id: int | None = None):
    """Apply tenant filter automatically when a model exposes organization_id."""
    resolved_org_id = org_id or active_org_id()
    if resolved_org_id is None:
        return model.query
    if hasattr(model, "organization_id"):
        return model.query.filter_by(organization_id=resolved_org_id)
    return model.query


def enforce_tenant_record(record: Record) -> Record:
    """Abort 403 when a record is outside the active tenant context."""
    if record is None:
        abort(404)
    ensure_org_access(getattr(record, "organization_id", None))
    return record


def get_tenant_record_or_404(model, object_id, org_id: int | None = None):
    """Fetch a single record by id and enforce tenant isolation."""
    record = scoped_query(model, org_id).filter_by(id=object_id).first_or_404()
    return enforce_tenant_record(record)


def tenant_resource_loader(loader: Callable[..., Record]) -> Callable[..., Record]:
    """Wrap a loader callable to enforce organization isolation automatically."""

    @wraps(loader)
    def wrapper(*args: Any, **kwargs: Any) -> Record:
        record = loader(*args, **kwargs)
        return enforce_tenant_record(record)

    return wrapper


def register_tenant_criteria(session: Session, tenant_base) -> None:
    """Globally apply row-level tenant criteria for mapped tenant-owned models."""
    if session.info.get("tenant_criteria_registered"):
        return

    @event.listens_for(session, "do_orm_execute")
    def _add_tenant_criteria(execute_state):  # type: ignore[unused-argument]
        if not execute_state.is_select:
            return
        if execute_state.is_column_load or execute_state.is_relationship_load:
            return

        org_id = active_org_id()
        if org_id is None:
            return

        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                tenant_base,
                lambda cls: cls.organization_id == org_id,
                include_aliases=True,
            )
        )

    session.info["tenant_criteria_registered"] = True


def tenant_required(view):
    """Ensure the caller belongs to an active organization and session matches."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if getattr(current_user, "is_super_admin", False):
            session.pop("org_id", None)
            return view(*args, **kwargs)
        if not current_user.is_authenticated:
            abort(401)

        org_id = getattr(current_user, "organization_id", None)
        if org_id is None:
            abort(403)

        session_org_id = session.get("org_id")
        if session_org_id is not None and session_org_id != org_id:
            logout_user()
            session.clear()
            abort(403)

        from ..models import Organization  # local import to avoid circular

        organization = Organization.query.get(org_id)
        if organization is None or organization.status != "active":
            logout_user()
            session.clear()
            abort(403)

        if not getattr(current_user, "is_active", True):
            logout_user()
            session.clear()
            abort(403)

        session["org_id"] = org_id
        return view(*args, **kwargs)

    return wrapper


def role_required(*allowed_roles):
    """Restrict a view to users having one of the allowed roles."""

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)

            if getattr(current_user, "is_super_admin", False):
                return view(*args, **kwargs)

            if getattr(current_user, "role", None) not in allowed_roles:
                abort(403)

            return view(*args, **kwargs)

        return wrapper

    return decorator


def admin_required(view):
    """Shortcut decorator for owner/admin roles only."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if getattr(current_user, "is_super_admin", False):
            return view(*args, **kwargs)
        if not current_user.is_authenticated:
            abort(401)

        allowed = {"owner", "admin"}
        if getattr(current_user, "role", None) not in allowed:
            abort(403)
        return view(*args, **kwargs)

    return wrapper
