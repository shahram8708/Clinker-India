"""Database models for multi-tenant SaaS."""
from datetime import datetime, timedelta
import secrets

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.orm import declared_attr, validates
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


TRIAL_SEAT_LIMIT = 5
DEFAULT_TRIAL_DAYS = 21


class TenantOwnedMixin:
    """Mixin for tables that must always belong to a single organization."""

    __abstract__ = True
    __tenant_owned__ = True

    @declared_attr
    def organization_id(cls):  # noqa: D401 - declarative attribute
        return db.Column(
            db.Integer,
            db.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )

    @classmethod
    def for_org(cls, organization_id: int):
        return cls.query.filter_by(organization_id=organization_id)

    @validates("organization_id")
    def _lock_org(self, key, organization_id):  # noqa: WPS442 - required signature
        if getattr(self, key, None) not in (None, organization_id):
            raise ValueError("organization_id is immutable for tenant-owned records")
        return organization_id

    def enforce_same_org(self, organization_id: int) -> None:
        if self.organization_id != organization_id:
            raise PermissionError("Cross-organization access is not allowed")


class Organization(db.Model):
    __tablename__ = "organizations"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    industry = db.Column(db.String(120))
    timezone = db.Column(db.String(64))
    size = db.Column(db.String(32))
    preferences = db.Column(db.JSON, default=dict)
    status = db.Column(db.String(20), default="active", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    suspended_at = db.Column(db.DateTime)
    onboarding_completed_at = db.Column(db.DateTime)

    users = db.relationship(
        "User",
        backref="organization",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    subscription = db.relationship(
        "OrganizationSubscription",
        backref=db.backref("organization", uselist=False),
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    seat_purchases = db.relationship(
        "SeatPurchase",
        backref="organization",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        db.CheckConstraint(status.in_(["active", "suspended"]), name="ck_org_status"),
    )

    def suspend(self) -> None:
        self.status = "suspended"
        self.suspended_at = datetime.utcnow()

    def activate(self) -> None:
        self.status = "active"
        self.suspended_at = None

    def admin_count(self) -> int:
        return sum(1 for user in self.users if user.role in {"owner", "admin"} and user.is_active)

    def mark_onboarding_complete(self) -> None:
        self.onboarding_completed_at = datetime.utcnow()

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Organization {self.name}>"


class Workspace(TenantOwnedMixin, db.Model):
    __tablename__ = "workspaces"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(255))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_workspace_name_per_org"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Workspace {self.name}>"


class User(UserMixin, TenantOwnedMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(255))
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(32), default="member", nullable=False)
    lifecycle_state = db.Column(db.String(20), default="pending", nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    verification_status = db.Column(db.String(20), default="pending", nullable=False)
    email_verified_at = db.Column(db.DateTime)

    last_login_at = db.Column(db.DateTime)
    invited_at = db.Column(db.DateTime)
    invitation_id = db.Column(
        db.Integer,
        db.ForeignKey("user_invitations.id", ondelete="SET NULL"),
    )

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.CheckConstraint(role.in_(["owner", "admin", "member"]), name="ck_user_role"),
        db.CheckConstraint(
            lifecycle_state.in_(["active", "pending", "disabled"]),
            name="ck_user_lifecycle_state",
        ),
        db.CheckConstraint(
            verification_status.in_(
                ["pending", "verified", "expired", "failed"],
            ),
            name="ck_user_verification_status",
        ),
    )

    @property
    def is_org_admin(self) -> bool:
        return self.role in {"owner", "admin"}

    @property
    def status_label(self) -> str:
        return self.lifecycle_state

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def activate(self) -> None:
        self.is_active = True
        self.lifecycle_state = "active"

    def disable(self) -> None:
        self.is_active = False
        self.lifecycle_state = "disabled"

    def mark_email_verified(self) -> None:
        self.verification_status = "verified"
        self.email_verified_at = datetime.utcnow()
        self.activate()

    def mark_verification_failed(self) -> None:
        self.verification_status = "failed"
        self.is_active = False
        self.lifecycle_state = "pending"

    def set_pending(self) -> None:
        self.is_active = False
        self.lifecycle_state = "pending"

    def generate_reset_token(self, expires_in: int = 3600) -> str:
        ttl_minutes = max(1, int(expires_in / 60))
        token, _ = PasswordResetToken.issue(
            user=self,
            ttl_minutes=ttl_minutes,
            secret_bytes=16,
        )
        return token

    @staticmethod
    def verify_reset_token(token: str, max_age: int = 3600) -> "User | None":
        record = PasswordResetToken.verify(token)
        if record is None:
            return None
        if record.expires_at < datetime.utcnow() or record.status != "pending":
            record.mark_expired()
            return None
        return User.query.get(record.user_id)

    @staticmethod
    def generate_invitation_token(expiry_minutes: int = 60 * 24) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(32)
        return token, datetime.utcnow() + timedelta(minutes=expiry_minutes)

    def get_id(self) -> str:
        return str(self.id)

    @property
    def is_email_verified(self) -> bool:
        return self.verification_status == "verified" and self.email_verified_at is not None


class EmailOTP(db.Model):
    __tablename__ = "email_otps"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    purpose = db.Column(db.String(32), nullable=False)
    code_hash = db.Column(db.String(255), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    consumed_at = db.Column(db.DateTime)
    attempt_count = db.Column(db.Integer, default=0, nullable=False)
    max_attempts = db.Column(db.Integer, default=5, nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    request_id = db.Column(db.String(64), index=True)
    status = db.Column(db.String(20), default="pending", nullable=False)

    __table_args__ = (
           db.CheckConstraint(purpose.in_(["registration", "login", "invite"]), name="ck_otp_purpose"),
        db.CheckConstraint(status.in_(["pending", "consumed", "expired", "locked"]), name="ck_otp_status"),
    )

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at

    @property
    def remaining_attempts(self) -> int:
        return max(self.max_attempts - self.attempt_count, 0)

    def mark_expired(self) -> None:
        self.status = "expired"
        self.consumed_at = self.consumed_at or datetime.utcnow()

    def mark_consumed(self) -> None:
        self.status = "consumed"
        self.consumed_at = datetime.utcnow()

    def increment_attempt(self) -> None:
        self.attempt_count += 1
        if self.attempt_count >= self.max_attempts:
            self.status = "locked"

    def check_code(self, code: str) -> bool:
        if self.status != "pending":
            return False
        if self.is_expired:
            self.mark_expired()
            return False
        self.increment_attempt()
        if check_password_hash(self.code_hash, code):
            self.mark_consumed()
            return True
        if self.remaining_attempts <= 0:
            self.status = "locked"
        return False


class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    identifier = db.Column(db.String(32), unique=True, nullable=False)
    secret_hash = db.Column(db.String(255), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    consumed_at = db.Column(db.DateTime)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    request_ip = db.Column(db.String(64))
    user_agent = db.Column(db.String(256))
    status = db.Column(db.String(20), default="pending", nullable=False)

    __table_args__ = (
        db.CheckConstraint(status.in_(["pending", "consumed", "expired"]), name="ck_reset_token_status"),
    )

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at

    def mark_expired(self) -> None:
        self.status = "expired"
        self.consumed_at = self.consumed_at or datetime.utcnow()

    def mark_consumed(self) -> None:
        self.status = "consumed"
        self.consumed_at = datetime.utcnow()

    @classmethod
    def issue(
        cls,
        user: User,
        ttl_minutes: int,
        secret_bytes: int,
        request_ip: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[str, "PasswordResetToken"]:
        identifier = secrets.token_urlsafe(8)
        secret = secrets.token_urlsafe(secret_bytes)
        secret_hash = generate_password_hash(secret)
        expires_at = datetime.utcnow() + timedelta(minutes=ttl_minutes)
        token = f"{identifier}.{secret}"

        # Invalidate older pending tokens for this user to enforce single-use links.
        cls.query.filter_by(user_id=user.id, status="pending").update({"status": "expired"})

        record = cls(
            user_id=user.id,
            identifier=identifier,
            secret_hash=secret_hash,
            expires_at=expires_at,
            request_ip=request_ip,
            user_agent=user_agent,
        )
        db.session.add(record)
        db.session.flush()
        return token, record

    @classmethod
    def verify(cls, raw_token: str) -> "PasswordResetToken | None":
        if not raw_token or "." not in raw_token:
            return None
        identifier, secret = raw_token.split(".", 1)
        record = cls.query.filter_by(identifier=identifier).first()
        if record is None or record.status != "pending":
            return None
        if record.is_expired:
            record.mark_expired()
            return None
        if not check_password_hash(record.secret_hash, secret):
            return None
        return record

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<User {self.email}>"


class SuperAdminOTP(db.Model):
    __tablename__ = "super_admin_otps"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    code_hash = db.Column(db.String(255), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    consumed_at = db.Column(db.DateTime)
    attempt_count = db.Column(db.Integer, default=0, nullable=False)
    max_attempts = db.Column(db.Integer, default=5, nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    request_id = db.Column(db.String(64), index=True, nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False)
    request_ip = db.Column(db.String(64))
    user_agent = db.Column(db.String(256))

    __table_args__ = (
        db.CheckConstraint(status.in_(["pending", "consumed", "expired", "locked"]), name="ck_sa_otp_status"),
        db.CheckConstraint(max_attempts > 0, name="ck_sa_otp_attempts_positive"),
    )

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at

    @property
    def remaining_attempts(self) -> int:
        return max(self.max_attempts - self.attempt_count, 0)

    def mark_expired(self) -> None:
        self.status = "expired"
        self.consumed_at = self.consumed_at or datetime.utcnow()

    def mark_consumed(self) -> None:
        self.status = "consumed"
        self.consumed_at = datetime.utcnow()

    def increment_attempt(self) -> None:
        self.attempt_count += 1
        if self.attempt_count >= self.max_attempts:
            self.status = "locked"

    def check_code(self, code: str) -> bool:
        if self.status != "pending":
            return False
        if self.is_expired:
            self.mark_expired()
            return False
        self.increment_attempt()
        if check_password_hash(self.code_hash, code):
            self.mark_consumed()
            return True
        if self.remaining_attempts <= 0:
            self.status = "locked"
        return False

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<SuperAdminOTP {self.email} {self.status}>"


class UserInvitation(TenantOwnedMixin, db.Model):
    __tablename__ = "user_invitations"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    full_name = db.Column(db.String(255))
    role = db.Column(db.String(32), default="member", nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False)
    token = db.Column(db.String(96), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    invited_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    accepted_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    organization = db.relationship(
        "Organization",
        backref=db.backref(
            "invitations",
            lazy=True,
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
    )

    __table_args__ = (
        db.CheckConstraint(status.in_(["pending", "accepted", "expired", "revoked"]), name="ck_invite_status"),
        db.CheckConstraint(role.in_(["owner", "admin", "member"]), name="ck_invite_role"),
        db.UniqueConstraint(
            "organization_id",
            "email",
            "status",
            name="uq_invite_email_per_org_status",
        ),
    )

    def mark_accepted(self) -> None:
        self.status = "accepted"
        self.accepted_at = datetime.utcnow()

    def mark_expired(self) -> None:
        self.status = "expired"

    def revoke(self) -> None:
        self.status = "revoked"

    def is_valid(self) -> bool:
        if self.status != "pending":
            return False
        if self.expires_at < datetime.utcnow():
            return False
        return True

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Invitation {self.email} ({self.status})>"


class PricingPlan(db.Model):
    __tablename__ = "pricing_plans"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    currency = db.Column(db.String(8), default="INR", nullable=False)
    base_amount = db.Column(db.Numeric(10, 2), nullable=False)
    per_seat_amount = db.Column(db.Numeric(10, 2), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<PricingPlan {self.code}>"


class OrganizationSubscription(TenantOwnedMixin, db.Model):
    __tablename__ = "organization_subscriptions"

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("pricing_plans.id", ondelete="SET NULL"))
    status = db.Column(db.String(32), default="trial_active", nullable=False)
    trial_started_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    trial_ends_at = db.Column(
        db.DateTime,
        default=lambda: datetime.utcnow() + timedelta(days=DEFAULT_TRIAL_DAYS),
        nullable=False,
    )
    trial_seat_allocation = db.Column(db.Integer, default=TRIAL_SEAT_LIMIT, nullable=False)
    paid_seats = db.Column(db.Integer, default=0, nullable=False)
    last_payment_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    plan = db.relationship("PricingPlan")

    __table_args__ = (
        db.UniqueConstraint("organization_id", name="uq_subscription_per_org"),
        db.CheckConstraint(status.in_(["trial_active", "subscription_required", "paid_active", "seat_limit_reached"]), name="ck_subscription_status"),
        db.CheckConstraint(trial_seat_allocation >= 0, name="ck_trial_seat_allocation_positive"),
        db.CheckConstraint(paid_seats >= 0, name="ck_paid_seats_nonnegative"),
    )

    @property
    def seat_limit(self) -> int:
        return int(self.trial_seat_allocation + self.paid_seats)

    def _used_seats(self) -> int:
        active_users = (
            User.query.filter_by(organization_id=self.organization_id)
            .filter(User.lifecycle_state.in_(["active", "pending"]))
            .count()
        )
        pending_invites = UserInvitation.query.filter_by(
            organization_id=self.organization_id,
            status="pending",
        ).count()
        return active_users + pending_invites

    @property
    def remaining_seats(self) -> int:
        remaining = self.seat_limit - self._used_seats()
        return remaining if remaining > 0 else 0

    def refresh_status(self) -> None:
        now = datetime.utcnow()
        if self.paid_seats > 0:
            self.status = "paid_active"
        elif now > self.trial_ends_at:
            self.status = "subscription_required"
        else:
            self.status = "trial_active"

        if self.remaining_seats <= 0 and self.paid_seats > 0:
            self.status = "seat_limit_reached"

    def can_allocate(self, seats_needed: int = 1) -> tuple[bool, str]:
        self.refresh_status()
        if self.status == "subscription_required":
            return False, "Upgrade required to add more than trial seats."
        if seats_needed <= 0:
            return False, "Seat request must be positive."
        if self.remaining_seats < seats_needed:
            return False, "Seat limit reached. Purchase additional seats."
        return True, "OK"

    def apply_payment(self, purchased_seats: int) -> None:
        if purchased_seats <= 0:
            raise ValueError("Purchased seats must be positive")
        self.paid_seats += purchased_seats
        self.last_payment_at = datetime.utcnow()
        self.refresh_status()

    @classmethod
    def bootstrap(cls, organization_id: int, plan: PricingPlan | None = None) -> "OrganizationSubscription":
        existing = cls.query.filter_by(organization_id=organization_id).first()
        if existing:
            return existing
        subscription = cls(
            organization_id=organization_id,
            plan=plan,
        )
        db.session.add(subscription)
        db.session.flush()
        return subscription

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Subscription org={self.organization_id} status={self.status}>"


class SeatPurchase(TenantOwnedMixin, db.Model):
    __tablename__ = "seat_purchases"

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("pricing_plans.id", ondelete="SET NULL"))
    seats_purchased = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(8), default="INR", nullable=False)
    base_amount = db.Column(db.Numeric(10, 2), nullable=False)
    per_seat_amount = db.Column(db.Numeric(10, 2), nullable=False)
    amount_subtotal = db.Column(db.Numeric(12, 2), nullable=False)
    tax_amount = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    amount_total = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(20), default="created", nullable=False)
    razorpay_order_id = db.Column(db.String(100))
    razorpay_payment_id = db.Column(db.String(100))
    razorpay_signature = db.Column(db.String(256))
    provider_payload = db.Column(db.JSON, default=dict)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    paid_at = db.Column(db.DateTime)

    plan = db.relationship("PricingPlan")

    __table_args__ = (
        db.CheckConstraint(status.in_(["created", "pending", "paid", "failed", "cancelled"]), name="ck_purchase_status"),
        db.CheckConstraint(seats_purchased > 0, name="ck_purchase_seats_positive"),
        db.CheckConstraint(amount_total >= 0, name="ck_amount_total_nonnegative"),
    )

    def mark_paid(self, payment_id: str, signature: str | None = None, payload: dict | None = None) -> None:
        self.status = "paid"
        self.razorpay_payment_id = payment_id
        if signature:
            self.razorpay_signature = signature
        if payload:
            self.provider_payload = payload
        self.paid_at = datetime.utcnow()

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<SeatPurchase org={self.organization_id} seats={self.seats_purchased} status={self.status}>"


class Plant(TenantOwnedMixin, db.Model):
    __tablename__ = "plants"

    id = db.Column(db.Integer, primary_key=True)
    plant_name = db.Column(db.String(255), nullable=False)
    plant_type = db.Column(db.String(2), nullable=False)  # IU or GU
    location = db.Column(db.String(255), nullable=False)
    production_capacity = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    production_cost = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    consumption_capacity = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    holding_cost = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    max_inventory_capacity = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    safety_stock_level = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    status = db.Column(db.String(20), default="active", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    source_routes = db.relationship(
        "TransportRoute",
        foreign_keys="TransportRoute.source_plant_id",
        backref="source_plant",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    destination_routes = db.relationship(
        "TransportRoute",
        foreign_keys="TransportRoute.destination_plant_id",
        backref="destination_plant",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    inventory = db.relationship(
        "Inventory",
        backref="plant",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "plant_name", name="uq_plant_name_per_org"),
        CheckConstraint(plant_type.in_(["IU", "GU"]), name="ck_plant_type"),
        CheckConstraint(production_capacity >= 0, name="ck_plant_prod_capacity_positive"),
        CheckConstraint(production_cost >= 0, name="ck_plant_prod_cost_positive"),
        CheckConstraint(consumption_capacity >= 0, name="ck_plant_cons_capacity_positive"),
        CheckConstraint(holding_cost >= 0, name="ck_plant_hold_cost_positive"),
        CheckConstraint(max_inventory_capacity >= 0, name="ck_max_inventory_positive"),
        CheckConstraint(safety_stock_level >= 0, name="ck_safety_stock_positive"),
        CheckConstraint(safety_stock_level <= max_inventory_capacity, name="ck_safety_below_max"),
        CheckConstraint(status.in_(["active", "disabled"]), name="ck_plant_status"),
    )

    @property
    def is_integrated(self) -> bool:
        return self.plant_type == "IU"

    @property
    def is_grinding(self) -> bool:
        return self.plant_type == "GU"

    def can_store(self, quantity: float) -> bool:
        return quantity <= float(self.max_inventory_capacity)

    def within_safety(self, quantity: float) -> bool:
        return quantity >= float(self.safety_stock_level)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Plant {self.plant_name} ({self.plant_type})>"


class TransportRoute(TenantOwnedMixin, db.Model):
    __tablename__ = "transport_routes"

    id = db.Column(db.Integer, primary_key=True)
    source_plant_id = db.Column(
        db.Integer,
        db.ForeignKey("plants.id", ondelete="CASCADE"),
        nullable=False,
    )
    destination_plant_id = db.Column(
        db.Integer,
        db.ForeignKey("plants.id", ondelete="CASCADE"),
        nullable=False,
    )
    mode = db.Column(db.String(10), nullable=False)
    trip_capacity = db.Column(db.Numeric(12, 2), nullable=False)
    min_batch_quantity = db.Column(db.Numeric(12, 2), nullable=False)
    max_trips_per_period = db.Column(db.Integer, nullable=False, default=0)
    cost_per_trip = db.Column(db.Numeric(12, 2), nullable=False)
    cost_per_ton = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    lead_time = db.Column(db.Integer)
    status = db.Column(db.String(20), default="active", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "source_plant_id",
            "destination_plant_id",
            "mode",
            name="uq_route_per_org_and_mode",
        ),
        CheckConstraint(mode.in_(["Road", "Rail", "Sea"]), name="ck_route_mode"),
        CheckConstraint(trip_capacity > 0, name="ck_trip_capacity_positive"),
        CheckConstraint(min_batch_quantity >= 0, name="ck_min_batch_nonnegative"),
        CheckConstraint(cost_per_trip >= 0, name="ck_cost_nonnegative"),
        CheckConstraint(cost_per_ton >= 0, name="ck_cost_per_ton_nonnegative"),
        CheckConstraint(max_trips_per_period >= 0, name="ck_max_trips_nonnegative"),
        CheckConstraint(status.in_(["active", "disabled"]), name="ck_route_status"),
    )

    def validate_org_alignment(self) -> None:
        if self.source_plant and self.source_plant.organization_id != self.organization_id:
            raise ValueError("Source plant must belong to the same organization")
        if self.destination_plant and self.destination_plant.organization_id != self.organization_id:
            raise ValueError("Destination plant must belong to the same organization")
        if self.trip_capacity <= self.min_batch_quantity:
            raise ValueError("Trip capacity must exceed minimum batch quantity")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Route {self.mode} {self.source_plant_id}->{self.destination_plant_id}>"


class Inventory(TenantOwnedMixin, db.Model):
    __tablename__ = "inventories"

    id = db.Column(db.Integer, primary_key=True)
    plant_id = db.Column(db.Integer, db.ForeignKey("plants.id", ondelete="CASCADE"), nullable=False)
    current_inventory = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "plant_id", name="uq_inventory_per_plant"),
        CheckConstraint(current_inventory >= 0, name="ck_inventory_nonnegative"),
    )

    def apply_bounds(self, plant: Plant) -> None:
        if self.current_inventory < 0:
            raise ValueError("Inventory cannot be negative")
        if self.current_inventory > plant.max_inventory_capacity:
            raise ValueError("Inventory exceeds plant max capacity")

    @property
    def below_safety(self) -> bool:
        if not self.plant:
            return False
        return float(self.current_inventory) < float(self.plant.safety_stock_level)

    @property
    def utilization_pct(self) -> float:
        if not self.plant or float(self.plant.max_inventory_capacity) <= 0:
            return 0.0
        return round(float(self.current_inventory) / float(self.plant.max_inventory_capacity) * 100, 2)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Inventory plant={self.plant_id} qty={self.current_inventory}>"


class PlantDemand(TenantOwnedMixin, db.Model):
    __tablename__ = "plant_demands"

    id = db.Column(db.Integer, primary_key=True)
    plant_id = db.Column(db.Integer, db.ForeignKey("plants.id", ondelete="CASCADE"), nullable=False)
    time_period = db.Column(db.Integer, nullable=False)
    demand = db.Column(db.Numeric(12, 2), nullable=False)
    min_fulfillment_pct = db.Column(db.Numeric(5, 2), nullable=False, default=100)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    plant = db.relationship("Plant")

    __table_args__ = (
        UniqueConstraint("organization_id", "plant_id", "time_period", name="uq_demand_per_plant_period"),
        CheckConstraint(time_period >= 1, name="ck_demand_period_positive"),
        CheckConstraint(demand > 0, name="ck_demand_positive"),
        CheckConstraint(min_fulfillment_pct >= 0, name="ck_min_fulfillment_nonnegative"),
        CheckConstraint(min_fulfillment_pct <= 100, name="ck_min_fulfillment_max"),
    )

    @property
    def min_fraction(self) -> float:
        pct = float(self.min_fulfillment_pct or 0)
        return max(min(pct / 100.0, 1.0), 0.0)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<PlantDemand plant={self.plant_id} period={self.time_period} demand={self.demand}>"


# New schema tables for multi-sheet clinker inputs


class IUGUType(TenantOwnedMixin, db.Model):
    __tablename__ = "iugu_types"

    id = db.Column(db.Integer, primary_key=True)
    planning_scenario_id = db.Column(
        db.Integer,
        db.ForeignKey("planning_scenarios.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    code = db.Column(db.String(16), nullable=False)
    plant_type = db.Column(db.String(5), nullable=False)
    sources_count = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "code", "planning_scenario_id", name="uq_iugu_code_per_org_scenario"),
        CheckConstraint(plant_type.in_(["IU", "GU"]), name="ck_iugu_type"),
    )


class ClinkerDemand(TenantOwnedMixin, db.Model):
    __tablename__ = "clinker_demands"

    id = db.Column(db.Integer, primary_key=True)
    planning_scenario_id = db.Column(
        db.Integer,
        db.ForeignKey("planning_scenarios.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    plant_code = db.Column(db.String(16), nullable=False)
    time_period = db.Column(db.Integer, nullable=False)
    demand_tons = db.Column(db.Numeric(12, 2), nullable=False)
    min_fulfillment_pct = db.Column(db.Numeric(5, 2), nullable=False, default=100)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "planning_scenario_id", "plant_code", "time_period", name="uq_demand_per_org_scenario_period"
        ),
        CheckConstraint(time_period >= 1, name="ck_clinker_demand_period_min"),
        CheckConstraint(time_period <= 3, name="ck_clinker_demand_period_max"),
        CheckConstraint(demand_tons > 0, name="ck_clinker_demand_positive"),
        CheckConstraint(min_fulfillment_pct >= 0, name="ck_clinker_min_fulfillment_nonnegative"),
        CheckConstraint(min_fulfillment_pct <= 100, name="ck_clinker_min_fulfillment_max"),
    )

    @property
    def min_fraction(self) -> float:
        pct = float(self.min_fulfillment_pct or 0)
        return max(min(pct / 100.0, 1.0), 0.0)


class ClinkerCapacity(TenantOwnedMixin, db.Model):
    __tablename__ = "clinker_capacities"

    id = db.Column(db.Integer, primary_key=True)
    planning_scenario_id = db.Column(
        db.Integer,
        db.ForeignKey("planning_scenarios.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    plant_code = db.Column(db.String(16), nullable=False)
    time_period = db.Column(db.Integer, nullable=False)
    capacity_tons = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "planning_scenario_id", "plant_code", "time_period", name="uq_capacity_per_org_scenario_period"
        ),
        CheckConstraint(time_period >= 1, name="ck_capacity_period_min"),
        CheckConstraint(time_period <= 3, name="ck_capacity_period_max"),
        CheckConstraint(capacity_tons >= 0, name="ck_capacity_nonnegative"),
    )


class ProductionCost(TenantOwnedMixin, db.Model):
    __tablename__ = "production_costs"

    id = db.Column(db.Integer, primary_key=True)
    planning_scenario_id = db.Column(
        db.Integer,
        db.ForeignKey("planning_scenarios.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    plant_code = db.Column(db.String(16), nullable=False)
    time_period = db.Column(db.Integer, nullable=False)
    cost_per_ton = db.Column(db.Numeric(10, 2), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "planning_scenario_id", "plant_code", "time_period", name="uq_prod_cost_per_org_scenario_period"
        ),
        CheckConstraint(time_period >= 1, name="ck_prod_cost_period_min"),
        CheckConstraint(time_period <= 3, name="ck_prod_cost_period_max"),
        CheckConstraint(cost_per_ton > 0, name="ck_prod_cost_positive"),
    )


class LogisticsIUGU(TenantOwnedMixin, db.Model):
    __tablename__ = "logistics_iugu"

    id = db.Column(db.Integer, primary_key=True)
    planning_scenario_id = db.Column(
        db.Integer,
        db.ForeignKey("planning_scenarios.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    from_code = db.Column(db.String(16), nullable=False)
    to_code = db.Column(db.String(16), nullable=False)
    transport_code = db.Column(db.String(10), nullable=False)
    time_period = db.Column(db.Integer, nullable=False)
    freight_cost = db.Column(db.Numeric(12, 2), nullable=False)
    handling_cost = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    quantity_multiplier = db.Column(db.Numeric(12, 2), nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "planning_scenario_id",
            "from_code",
            "to_code",
            "transport_code",
            "time_period",
            name="uq_logistics_route_per_period",
        ),
        CheckConstraint(time_period >= 1, name="ck_logistics_period_min"),
        CheckConstraint(time_period <= 3, name="ck_logistics_period_max"),
        CheckConstraint(freight_cost >= 0, name="ck_logistics_freight_nonnegative"),
        CheckConstraint(handling_cost >= 0, name="ck_logistics_handling_nonnegative"),
        CheckConstraint(quantity_multiplier > 0, name="ck_logistics_multiplier_positive"),
    )


class IUGUConstraint(TenantOwnedMixin, db.Model):
    __tablename__ = "iugu_constraints"

    id = db.Column(db.Integer, primary_key=True)
    planning_scenario_id = db.Column(
        db.Integer,
        db.ForeignKey("planning_scenarios.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    from_code = db.Column(db.String(16), nullable=False)
    transport_code = db.Column(db.String(10))
    to_code = db.Column(db.String(16))
    time_period = db.Column(db.Integer, nullable=False)
    constraint_type = db.Column(db.String(3), nullable=False)
    value_type = db.Column(db.String(3), nullable=False, default="C")
    value = db.Column(db.Numeric(12, 2), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "planning_scenario_id",
            "from_code",
            "transport_code",
            "to_code",
            "time_period",
            "constraint_type",
            name="uq_iugu_constraint_unique",
        ),
        CheckConstraint(time_period >= 1, name="ck_iugu_constraint_period_min"),
        CheckConstraint(time_period <= 3, name="ck_iugu_constraint_period_max"),
        CheckConstraint(value >= 0, name="ck_iugu_constraint_value_nonnegative"),
        CheckConstraint(constraint_type.in_(["L", "G", "E"]), name="ck_iugu_constraint_type"),
        CheckConstraint(value_type.in_(["C"]), name="ck_iugu_constraint_value_type"),
    )


class IUGUOpeningStock(TenantOwnedMixin, db.Model):
    __tablename__ = "iugu_opening_stocks"

    id = db.Column(db.Integer, primary_key=True)
    planning_scenario_id = db.Column(
        db.Integer,
        db.ForeignKey("planning_scenarios.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    plant_code = db.Column(db.String(16), nullable=False)
    opening_stock = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "planning_scenario_id", "plant_code", name="uq_opening_stock_per_org_scenario"),
        CheckConstraint(opening_stock >= 0, name="ck_opening_stock_nonnegative"),
    )


class HubOpeningStock(TenantOwnedMixin, db.Model):
    __tablename__ = "hub_opening_stocks"

    id = db.Column(db.Integer, primary_key=True)
    planning_scenario_id = db.Column(
        db.Integer,
        db.ForeignKey("planning_scenarios.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    from_code = db.Column(db.String(16), nullable=False)
    to_code = db.Column(db.String(16), nullable=False)
    opening_stock = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "planning_scenario_id", "from_code", "to_code", name="uq_hub_opening_per_org_scenario"),
        CheckConstraint(opening_stock >= 0, name="ck_hub_opening_nonnegative"),
    )


class IUGUClosingStock(TenantOwnedMixin, db.Model):
    __tablename__ = "iugu_closing_stocks"

    id = db.Column(db.Integer, primary_key=True)
    planning_scenario_id = db.Column(
        db.Integer,
        db.ForeignKey("planning_scenarios.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    plant_code = db.Column(db.String(16), nullable=False)
    time_period = db.Column(db.Integer, nullable=False)
    min_close_stock = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    max_close_stock = db.Column(db.Numeric(12, 2))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "planning_scenario_id", "plant_code", "time_period", name="uq_closing_stock_per_org_scenario_period"
        ),
        CheckConstraint(time_period >= 1, name="ck_closing_stock_period_min"),
        CheckConstraint(time_period <= 3, name="ck_closing_stock_period_max"),
        CheckConstraint(min_close_stock >= 0, name="ck_min_close_stock_nonnegative"),
        CheckConstraint("(max_close_stock IS NULL) OR (max_close_stock >= 0)", name="ck_max_close_stock_nonnegative"),
    )


class PlanningScenario(TenantOwnedMixin, db.Model):
    __tablename__ = "planning_scenarios"

    id = db.Column(db.Integer, primary_key=True)
    scenario_name = db.Column(db.String(255), nullable=False)
    periods = db.Column(db.Integer, nullable=False, default=1)
    status = db.Column(db.String(20), default="draft", nullable=False)
    result_cost = db.Column(db.Numeric(14, 2))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    summary = db.Column(db.JSON, default=dict)

    __table_args__ = (
        UniqueConstraint("organization_id", "scenario_name", name="uq_scenario_name_per_org"),
        CheckConstraint(status.in_(["draft", "executed", "completed"]), name="ck_scenario_status"),
        CheckConstraint(periods > 0, name="ck_periods_positive"),
    )

    def mark_executed(self, result_cost: float, summary: dict | None = None) -> None:
        self.status = "executed"
        self.result_cost = result_cost
        self.summary = summary or {}

    def mark_completed(self) -> None:
        self.status = "completed"

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Scenario {self.scenario_name} ({self.status})>"


class WorkspaceDataset(TenantOwnedMixin, db.Model):
    __tablename__ = "workspace_datasets"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    planning_scenario_id = db.Column(db.Integer, db.ForeignKey("planning_scenarios.id", ondelete="CASCADE"), nullable=False, unique=True)
    label = db.Column(db.String(120), nullable=False)
    notes = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    scenario = db.relationship("PlanningScenario")

    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "label", name="uq_dataset_label_per_workspace"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<WorkspaceDataset {self.label} workspace={self.workspace_id}>"


class OptimizationJob(TenantOwnedMixin, db.Model):
    __tablename__ = "optimization_jobs"

    id = db.Column(db.Integer, primary_key=True)
    scenario_id = db.Column(db.Integer, db.ForeignKey("planning_scenarios.id", ondelete="CASCADE"), nullable=False)
    mode = db.Column(db.String(20), nullable=False, default="deterministic")
    status = db.Column(db.String(20), nullable=False, default="pending")
    solver = db.Column(db.String(64), default="greedy")
    solver_status = db.Column(db.String(64))
    message = db.Column(db.String(512))
    request_payload = db.Column(db.JSON, default=dict)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    runtime_seconds = db.Column(db.Numeric(10, 2))

    scenario = db.relationship("PlanningScenario", backref=db.backref("optimization_jobs", lazy=True, cascade="all, delete-orphan"))

    __table_args__ = (
        CheckConstraint(status.in_(["pending", "running", "completed", "failed"]), name="ck_opt_job_status"),
        CheckConstraint(mode.in_(["deterministic", "stochastic", "robust"]), name="ck_opt_job_mode"),
    )

    def mark_running(self) -> None:
        self.status = "running"
        self.started_at = datetime.utcnow()

    def mark_failed(self, message: str) -> None:
        self.status = "failed"
        self.message = message
        self.completed_at = datetime.utcnow()

    def mark_completed(self, solver_status: str, runtime_seconds: float | None = None) -> None:
        self.status = "completed"
        self.solver_status = solver_status
        self.completed_at = datetime.utcnow()
        if runtime_seconds is not None:
            self.runtime_seconds = runtime_seconds

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<OptimizationJob {self.id} ({self.status})>"


class OptimizationResult(TenantOwnedMixin, db.Model):
    __tablename__ = "optimization_results"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("optimization_jobs.id", ondelete="CASCADE"), nullable=False, unique=True)
    scenario_id = db.Column(db.Integer, db.ForeignKey("planning_scenarios.id", ondelete="CASCADE"), nullable=False)
    total_cost = db.Column(db.Numeric(14, 2))
    production_plan = db.Column(db.JSON, default=dict)
    shipment_plan = db.Column(db.JSON, default=dict)
    inventory_plan = db.Column(db.JSON, default=dict)
    cost_breakdown = db.Column(db.JSON, default=dict)
    kpis = db.Column(db.JSON, default=dict)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    job = db.relationship("OptimizationJob", backref=db.backref("result", uselist=False, cascade="all, delete-orphan"))
    scenario = db.relationship("PlanningScenario")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<OptimizationResult job={self.job_id} cost={self.total_cost}>"


class ActivityLog(TenantOwnedMixin, db.Model):
    __tablename__ = "activity_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action_type = db.Column(db.String(64), nullable=False)
    action_description = db.Column(db.String(512), nullable=False)
    entity_type = db.Column(db.String(64))
    entity_id = db.Column(db.Integer)
    severity = db.Column(db.String(16), default="info", nullable=False)
    # Keep column name "metadata" but avoid using reserved attribute name on the model.
    details = db.Column("metadata", db.JSON, default=dict)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint(severity.in_(["info", "warning", "critical"]), name="ck_activity_severity"),
    )

    def short_label(self) -> str:
        return f"{self.action_type.title()}" if self.action_type else "Activity"

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<ActivityLog {self.action_type} {self.entity_type}:{self.entity_id}>"


class Notification(TenantOwnedMixin, db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    title = db.Column(db.String(255), nullable=False)
    body = db.Column(db.String(512))
    severity = db.Column(db.String(16), default="info", nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint(severity.in_(["info", "warning", "critical", "system"]), name="ck_notification_severity"),
    )

    def mark_read(self) -> None:
        self.is_read = True

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Notification {self.severity} {self.title}>"


class ContactRequest(db.Model):
    __tablename__ = "contact_requests"

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="SET NULL"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    full_name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(180), nullable=False)
    category = db.Column(db.String(64), nullable=False)
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(32), default="open", nullable=False)
    request_ip = db.Column(db.String(64))
    user_agent = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    acknowledged_at = db.Column(db.DateTime)

    __table_args__ = (
        CheckConstraint(status.in_(["open", "acknowledged", "closed"]), name="ck_contact_status"),
    )

    def mark_acknowledged(self) -> None:
        self.status = "acknowledged"
        self.acknowledged_at = datetime.utcnow()

    def close(self) -> None:
        self.status = "closed"

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<ContactRequest {self.category} {self.email}>"
