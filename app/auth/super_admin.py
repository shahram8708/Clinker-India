"""Super admin identity and credential helpers."""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Optional

from flask import current_app
from flask_login import UserMixin
from werkzeug.security import check_password_hash

SUPER_ADMIN_SESSION_ID = "superadmin"


@dataclass
class SuperAdminIdentity(UserMixin):
    email: str

    @property
    def id(self) -> str:  # type: ignore[override]
        return SUPER_ADMIN_SESSION_ID

    @property
    def role(self) -> str:
        return "super_admin"

    @property
    def full_name(self) -> str:
        return "Clinker Super Admin"

    @property
    def organization_id(self):  # noqa: D401
        return None

    @property
    def organization(self):  # noqa: D401
        return None

    @property
    def is_active(self) -> bool:  # noqa: D401
        return True

    @property
    def is_super_admin(self) -> bool:  # noqa: D401
        return True

    @property
    def is_org_admin(self) -> bool:  # noqa: D401
        return True

    def get_id(self) -> str:  # noqa: D401
        return SUPER_ADMIN_SESSION_ID


def super_admin_configured() -> bool:
    cfg = current_app.config
    return bool((cfg.get("SUPER_ADMIN_EMAIL") or "").strip() and (cfg.get("SUPER_ADMIN_PASSWORD") or ""))


def super_admin_email() -> Optional[str]:
    email = (current_app.config.get("SUPER_ADMIN_EMAIL") or "").strip()
    return email.lower() if email else None


def super_admin_credentials_match(email: str, password: str) -> bool:
    """Securely compare submitted credentials against environment-backed values."""
    target_email = super_admin_email()
    target_secret = current_app.config.get("SUPER_ADMIN_PASSWORD") or ""
    if not target_email or not target_secret:
        return False
    if email.strip().lower() != target_email:
        return False

    hashed = current_app.config.get("SUPER_ADMIN_PASSWORD_IS_HASHED", False)
    if hashed:
        return check_password_hash(target_secret, password)
    return hmac.compare_digest(target_secret, password)
