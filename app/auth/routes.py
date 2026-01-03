"""Authentication blueprint: register, login, logout, onboarding, and admin provisioning."""
import csv
import io
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_mail import Message
from werkzeug.security import generate_password_hash

from ..extensions import db, mail
from ..models import (
    Organization,
    OrganizationSubscription,
    PricingPlan,
    User,
    EmailOTP,
    PasswordResetToken,
    UserInvitation,
    SuperAdminOTP,
)
from ..tenant.utils import admin_required, tenant_required
from .super_admin import (
    SUPER_ADMIN_SESSION_ID,
    SuperAdminIdentity,
    super_admin_configured,
    super_admin_credentials_match,
    super_admin_email,
)
from .forms import (
    AcceptInviteForm,
    ForgotPasswordForm,
    LoginForm,
    OTPVerifyForm,
    OrganizationProfileForm,
    ProvisionUserForm,
    RegisterForm,
    ResetPasswordForm,
    InviteUserForm,
    BulkInviteForm,
    BulkProvisionForm,
)

auth_bp = Blueprint("auth", __name__, template_folder="../templates/auth")

OTP_PURPOSE_REG = "registration"
OTP_PURPOSE_LOGIN = "login"
OTP_PURPOSE_INVITE = "invite"


def _send_email(subject: str, template_name: str, recipient: str, **context) -> None:
    html = render_template(template_name, **context)
    text_fallback = context.get("text_fallback") or "This message requires an HTML-capable email client."
    msg = Message(subject=subject, recipients=[recipient])
    msg.html = html
    msg.body = text_fallback
    try:
        mail.send(msg)
        current_app.logger.info("Email %s dispatched to %s", template_name, recipient)
    except Exception:  # pragma: no cover - operational safeguard
        current_app.logger.exception("Email dispatch failed for %s", recipient)


def _generate_otp_code(length: int) -> str:
    upper = 10**length
    return str(secrets.randbelow(upper)).zfill(length)


def _latest_otp(user_id: int, purpose: str) -> EmailOTP | None:
    return (
        EmailOTP.query.filter_by(user_id=user_id, purpose=purpose)
        .order_by(EmailOTP.sent_at.desc())
        .first()
    )


def _latest_super_admin_otp(email: str) -> SuperAdminOTP | None:
    return (
        SuperAdminOTP.query.filter_by(email=email)
        .order_by(SuperAdminOTP.sent_at.desc())
        .first()
    )


def _super_admin_can_resend(email: str) -> bool:
    cooldown = current_app.config.get("SUPER_ADMIN_OTP_RESEND_SECONDS", 60)
    last = _latest_super_admin_otp(email)
    if last is None:
        return True
    elapsed = (datetime.utcnow() - last.sent_at).total_seconds()
    return elapsed >= cooldown


def _super_admin_issue_otp(request_id: str) -> tuple[str, SuperAdminOTP]:
    cfg = current_app.config
    email = super_admin_email()
    if not email:
        raise RuntimeError("Super admin email is not configured")

    SuperAdminOTP.query.filter_by(email=email, status="pending").update({"status": "expired"})
    code_length = max(4, cfg.get("SUPER_ADMIN_OTP_LENGTH", 6))
    code = _generate_otp_code(code_length)
    otp = SuperAdminOTP(
        email=email,
        code_hash=generate_password_hash(code),
        expires_at=datetime.utcnow() + timedelta(minutes=cfg.get("SUPER_ADMIN_OTP_EXPIRY_MINUTES", 8)),
        max_attempts=cfg.get("SUPER_ADMIN_OTP_MAX_ATTEMPTS", 5),
        request_id=request_id,
        request_ip=request.remote_addr,
        user_agent=(request.headers.get("User-Agent") or "")[:256],
    )
    db.session.add(otp)
    db.session.flush()
    return code, otp


def _send_super_admin_login_otp_email(code: str, expires_at: datetime, recipient: str) -> None:
    _send_email(
        subject="Super Admin login verification",
        template_name="emails/login_otp.html",
        recipient=recipient,
        otp_code=code,
        expires_at=expires_at,
        user_name="Clinker Super Admin",
        brand_name="Clinker India",
    )


def _issue_otp(user: User, purpose: str, request_id: str | None = None) -> tuple[str, EmailOTP]:
    cfg = current_app.config
    EmailOTP.query.filter_by(user_id=user.id, purpose=purpose, status="pending").update({"status": "expired"})
    code = _generate_otp_code(max(4, cfg.get("OTP_LENGTH", 6)))
    otp = EmailOTP(
        user_id=user.id,
        purpose=purpose,
        code_hash=generate_password_hash(code),
        expires_at=datetime.utcnow() + timedelta(minutes=cfg.get("OTP_EXPIRY_MINUTES", 10)),
        max_attempts=cfg.get("OTP_MAX_ATTEMPTS", 5),
        request_id=request_id or secrets.token_hex(8),
    )
    db.session.add(otp)
    db.session.flush()
    return code, otp


def _can_resend_otp(user_id: int, purpose: str) -> bool:
    cooldown = current_app.config.get("OTP_RESEND_SECONDS", 60)
    last = _latest_otp(user_id, purpose)
    if last is None:
        return True
    elapsed = (datetime.utcnow() - last.sent_at).total_seconds()
    return elapsed >= cooldown


def _send_registration_otp_email(user: User, code: str, expires_at: datetime) -> None:
    _send_email(
        subject="Verify your Clinker India account",
        template_name="emails/registration_otp.html",
        recipient=user.email,
        otp_code=code,
        expires_at=expires_at,
        user_name=user.full_name or user.email,
        brand_name="Clinker India",
    )


def _send_login_otp_email(user: User, code: str, expires_at: datetime) -> None:
    _send_email(
        subject="Sign-in verification for Clinker India",
        template_name="emails/login_otp.html",
        recipient=user.email,
        otp_code=code,
        expires_at=expires_at,
        user_name=user.full_name or user.email,
        brand_name="Clinker India",
    )


def _send_invite_otp_email(user: User, organization_name: str, code: str, expires_at: datetime) -> None:
    _send_email(
        subject=f"Verify your invite to {organization_name}",
        template_name="emails/login_otp.html",
        recipient=user.email,
        otp_code=code,
        expires_at=expires_at,
        user_name=user.full_name or user.email,
        brand_name=organization_name,
    )


def _send_reset_email(user: User, reset_link: str, expires_at: datetime) -> None:
    _send_email(
        subject="Reset your Clinker India password",
        template_name="emails/reset_password.html",
        recipient=user.email,
        reset_link=reset_link,
        expires_at=expires_at,
        user_name=user.full_name or user.email,
        brand_name="Clinker India",
    )


def _default_plan() -> PricingPlan:
    code = current_app.config.get("DEFAULT_PLAN_CODE", "CLINKER_PRO_INDIA")
    plan = PricingPlan.query.filter_by(code=code).first()
    if plan:
        return plan
    plan = PricingPlan(
        code=code,
        name="Clinker Pro India",
        currency="INR",
        base_amount=current_app.config.get("PRICING_BASE_AMOUNT_INR", 9999),
        per_seat_amount=current_app.config.get("PRICING_PER_SEAT_INR", 499),
        is_active=True,
    )
    db.session.add(plan)
    db.session.commit()
    return plan


def _subscription(org_id: int) -> OrganizationSubscription:
    plan = _default_plan()
    subscription = OrganizationSubscription.bootstrap(org_id, plan)
    subscription.refresh_status()
    db.session.commit()
    return subscription


def _enforce_seat_capacity(org_id: int, seats_needed: int = 1) -> bool:
    subscription = _subscription(org_id)
    allowed, message = subscription.can_allocate(seats_needed)
    if not allowed:
        flash(message, "warning")
        return False
    return True


def _read_csv_upload(file_storage) -> tuple[list[dict[str, str]], list[str], list[str]]:
    errors: list[str] = []
    try:
        content = file_storage.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], [], ["Could not decode CSV. Please upload UTF-8 encoded CSV files."]

    if not content.strip():
        return [], [], ["The uploaded CSV file is empty."]

    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        return [], [], ["CSV requires a header row with the expected column names."]

    headers = [header.strip().lower() for header in reader.fieldnames if header]
    rows = [
        {(key or "").strip().lower(): (value or "").strip() for key, value in row.items()}
        for row in reader
    ]

    if not rows:
        errors.append("CSV contains only headers and no data rows.")

    return rows, headers, errors


def _flash_form_errors(form, prefix: str = "") -> None:
    """Surface WTForms validation errors to the UI for easier debugging."""
    messages: list[str] = []
    for field, errs in form.errors.items():
        human_field = getattr(form, field).label.text if hasattr(getattr(form, field), "label") else field
        for err in errs:
            messages.append(f"{human_field}: {err}")
    if messages:
        flash((prefix + " ").strip() + " ".join(messages), "danger")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = RegisterForm()
    if form.validate_on_submit():
        session.clear()  # avoid session fixation
        email = form.email.data.lower()
        org_name = form.organization_name.data.strip()
        industry = (form.industry.data or "").strip() or None
        admin_name = form.admin_name.data.strip()

        if Organization.query.filter_by(name=org_name).first():
            flash("An organization with that name already exists.", "warning")
            return render_template("auth/register.html", form=form)

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash("Account already exists. Please log in instead.", "warning")
            return redirect(url_for("auth.login"))

        organization = Organization(name=org_name, industry=industry)
        user = User(
            email=email,
            full_name=admin_name,
            role="owner",
            organization=organization,
        )
        user.set_password(form.password.data)
        user.set_pending()
        user.verification_status = "pending"

        db.session.add(organization)
        db.session.add(user)
        db.session.flush()

        OrganizationSubscription.bootstrap(organization.id, _default_plan())

        db.session.commit()

        code, otp = _issue_otp(user, OTP_PURPOSE_REG)
        db.session.commit()
        _send_registration_otp_email(user, code, otp.expires_at)

        current_app.logger.info("Organization %s created; verification OTP issued for %s", organization.id, user.email)

        session["pending_verification_user_id"] = user.id
        flash("We sent a verification code to your email. Enter it to activate your account.", "info")
        return redirect(url_for("auth.verify_registration"))

    return render_template("auth/register.html", form=form)


@auth_bp.route("/verify/registration", methods=["GET", "POST"])
def verify_registration():
    user_id = session.get("pending_verification_user_id")
    if not user_id:
        flash("No verification is pending. Please start again.", "warning")
        return redirect(url_for("auth.login"))

    user = User.query.get(user_id)
    if user is None:
        session.pop("pending_verification_user_id", None)
        flash("We could not find that account. Please register again.", "warning")
        return redirect(url_for("auth.register"))

    if user.verification_status == "verified":
        flash("Your email is already verified. You can sign in.", "info")
        session.pop("pending_verification_user_id", None)
        return redirect(url_for("auth.login"))

    latest = _latest_otp(user.id, OTP_PURPOSE_REG)
    form = OTPVerifyForm()

    if form.validate_on_submit():
        if latest is None:
            user.verification_status = "expired"
            db.session.commit()
            flash("Your verification code expired. Request a new one.", "danger")
            return redirect(url_for("auth.verify_registration"))

        success = latest.check_code(form.otp.data.strip())
        db.session.commit()

        if success:
            user.mark_email_verified()
            db.session.commit()
            login_user(user, remember=True)
            session.pop("pending_verification_user_id", None)
            session["org_id"] = user.organization_id
            flash("Email verified. Welcome to Clinker India!", "success")
            return redirect(url_for("main.dashboard"))

        if latest.status == "locked":
            user.mark_verification_failed()
            db.session.commit()
            flash("Too many incorrect attempts. Request a new code.", "danger")
        elif latest.is_expired or latest.status == "expired":
            user.verification_status = "expired"
            db.session.commit()
            flash("That code has expired. Request a fresh one.", "danger")
        else:
            flash(f"Incorrect code. {latest.remaining_attempts} attempts remaining.", "danger")

    return render_template(
        "auth/verify_registration.html",
        form=form,
        user=user,
        latest=latest,
    )


@auth_bp.route("/verify/registration/resend", methods=["POST"])
def resend_registration_otp():
    user_id = session.get("pending_verification_user_id")
    if not user_id:
        flash("No verification is pending.", "warning")
        return redirect(url_for("auth.login"))

    user = User.query.get(user_id)
    if user is None:
        flash("We could not find that account. Please register again.", "warning")
        return redirect(url_for("auth.register"))

    user.verification_status = "pending"
    user.set_pending()

    if not _can_resend_otp(user.id, OTP_PURPOSE_REG):
        flash("Please wait a moment before requesting another code.", "warning")
        return redirect(url_for("auth.verify_registration"))

    code, otp = _issue_otp(user, OTP_PURPOSE_REG)
    db.session.commit()
    _send_registration_otp_email(user, code, otp.expires_at)

    flash("A new verification code has been sent to your email.", "info")
    return redirect(url_for("auth.verify_registration"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        session.clear()  # force new session per authentication event
        submitted_email = form.email.data.lower()

        if super_admin_configured() and super_admin_email() == submitted_email:
            if not super_admin_credentials_match(submitted_email, form.password.data):
                flash("Invalid credentials. Please try again.", "danger")
                current_app.logger.warning("Super admin credential mismatch")
                return render_template("auth/login.html", form=form)

            if not _super_admin_can_resend(submitted_email):
                flash("A verification code was recently sent. Please check your email.", "info")
                session["super_admin_email"] = submitted_email
                latest_sa = _latest_super_admin_otp(submitted_email)
                if latest_sa:
                    session["super_admin_request_id"] = latest_sa.request_id
                return redirect(url_for("auth.login_verify"))

            request_id = secrets.token_hex(12)
            code, otp = _super_admin_issue_otp(request_id=request_id)
            db.session.commit()
            _send_super_admin_login_otp_email(code, otp.expires_at, submitted_email)

            session["super_admin_email"] = submitted_email
            session["super_admin_request_id"] = otp.request_id
            session.pop("org_id", None)

            current_app.logger.info("Super admin OTP dispatched for request %s", request_id)
            flash("Enter the super admin verification code sent to your email.", "info")
            return redirect(url_for("auth.login_verify"))

        user = User.query.filter_by(email=submitted_email).first()
        if user is None or not user.check_password(form.password.data):
            flash("Invalid credentials. Please try again.", "danger")
            current_app.logger.warning("Failed login attempt for %s", submitted_email)
            return render_template("auth/login.html", form=form)

        organization = user.organization
        if organization.status != "active":
            flash("This organization is suspended. Contact support.", "danger")
            current_app.logger.warning("Suspended org login blocked for user %s", user.email)
            return render_template("auth/login.html", form=form)

        if not user.is_active:
            flash("This account is disabled. Contact your organization admin.", "danger")
            current_app.logger.warning("Disabled account login blocked for user %s", user.email)
            return render_template("auth/login.html", form=form)

        if user.verification_status != "verified":
            session["pending_verification_user_id"] = user.id
            if _can_resend_otp(user.id, OTP_PURPOSE_REG):
                code, otp = _issue_otp(user, OTP_PURPOSE_REG)
                db.session.commit()
                _send_registration_otp_email(user, code, otp.expires_at)
            flash("Verify your email before signing in. We sent you a code.", "warning")
            return redirect(url_for("auth.verify_registration"))

        if not _can_resend_otp(user.id, OTP_PURPOSE_LOGIN):
            flash("We recently sent a code. Check your email or try again shortly.", "info")
            session["login_challenge_user_id"] = user.id
            session["login_remember_me"] = True
            session["org_id"] = user.organization_id
            return redirect(url_for("auth.login_verify"))

        code, otp = _issue_otp(user, OTP_PURPOSE_LOGIN, request_id=secrets.token_hex(8))
        db.session.commit()
        _send_login_otp_email(user, code, otp.expires_at)

        session["login_challenge_user_id"] = user.id
        session["login_remember_me"] = True
        session["org_id"] = user.organization_id
        flash("Enter the verification code sent to your email to finish signing in.", "info")
        return redirect(url_for("auth.login_verify"))

        flash("Invalid request.", "danger")
        return redirect(url_for("auth.login"))

    return render_template("auth/login.html", form=form)


@auth_bp.route("/login/verify", methods=["GET", "POST"])
def login_verify():
    sa_email = session.get("super_admin_email")
    if sa_email and super_admin_configured() and sa_email == super_admin_email():
        latest_sa = _latest_super_admin_otp(sa_email)
        form = OTPVerifyForm()

        if form.validate_on_submit():
            if latest_sa is None:
                flash("Your code expired. Start a new sign-in.", "danger")
                return redirect(url_for("auth.login"))

            success = latest_sa.check_code(form.otp.data.strip())
            db.session.commit()

            if success:
                login_user(SuperAdminIdentity(email=sa_email), remember=False)
                session.permanent = False
                session.pop("super_admin_email", None)
                session.pop("super_admin_request_id", None)
                session.pop("org_id", None)
                current_app.logger.info("Super admin completed OTP login")
                flash("Super admin signed in securely.", "success")
                return redirect(url_for("superadmin.dashboard"))

            if latest_sa.status == "locked":
                flash("Too many incorrect attempts. Restart sign-in.", "danger")
                return redirect(url_for("auth.login"))
            if latest_sa.is_expired or latest_sa.status == "expired":
                flash("That code expired. Request a new one.", "warning")
                return redirect(url_for("auth.login"))
            flash(f"Incorrect code. {latest_sa.remaining_attempts} attempts remaining.", "danger")

        return render_template(
            "auth/login_otp.html",
            form=form,
            user=SuperAdminIdentity(email=sa_email),
            latest=latest_sa,
        )

    user_id = session.get("login_challenge_user_id")
    if not user_id:
        flash("No login verification is pending.", "warning")
        return redirect(url_for("auth.login"))

    user = User.query.get(user_id)
    if user is None:
        session.pop("login_challenge_user_id", None)
        flash("We could not find that account.", "warning")
        return redirect(url_for("auth.login"))

    organization = user.organization
    if organization.status != "active" or not user.is_active:
        flash("This account is blocked. Contact your admin.", "danger")
        return redirect(url_for("auth.login"))

    latest = _latest_otp(user.id, OTP_PURPOSE_LOGIN)
    form = OTPVerifyForm()

    if form.validate_on_submit():
        if latest is None:
            flash("Your code expired. Start a new sign-in.", "danger")
            return redirect(url_for("auth.login"))

        success = latest.check_code(form.otp.data.strip())
        db.session.commit()

        if success:
            remember = bool(session.pop("login_remember_me", True))
            login_user(user, remember=remember)
            session["org_id"] = user.organization_id
            session.permanent = True
            current_app.permanent_session_lifetime = timedelta(days=365)
            user.last_login_at = datetime.utcnow()
            db.session.commit()
            session.pop("login_challenge_user_id", None)

            current_app.logger.info("User %s completed OTP login", user.email)

            if user.is_org_admin and organization.onboarding_completed_at is None:
                flash("Finish onboarding to unlock your workspace.", "info")
                return redirect(url_for("auth.onboarding"))

            flash("Signed in securely.", "success")
            return redirect(url_for("main.dashboard"))

        if latest.status == "locked":
            flash("Too many incorrect attempts. Restart sign-in.", "danger")
            return redirect(url_for("auth.login"))
        if latest.is_expired or latest.status == "expired":
            flash("That code expired. Request a new one.", "warning")
            return redirect(url_for("auth.login"))
        flash(f"Incorrect code. {latest.remaining_attempts} attempts remaining.", "danger")

    return render_template(
        "auth/login_otp.html",
        form=form,
        user=user,
        latest=latest,
    )


@auth_bp.route("/login/resend", methods=["POST"])
def resend_login_otp():
    sa_email = session.get("super_admin_email")
    if sa_email and super_admin_configured() and sa_email == super_admin_email():
        if not _super_admin_can_resend(sa_email):
            flash("Please wait a moment before requesting another code.", "warning")
            return redirect(url_for("auth.login_verify"))

        request_id = secrets.token_hex(12)
        code, otp = _super_admin_issue_otp(request_id=request_id)
        db.session.commit()
        _send_super_admin_login_otp_email(code, otp.expires_at, sa_email)
        session["super_admin_request_id"] = otp.request_id
        flash("A fresh verification code has been emailed to you.", "info")
        return redirect(url_for("auth.login_verify"))

    user_id = session.get("login_challenge_user_id")
    if not user_id:
        flash("No login verification is pending.", "warning")
        return redirect(url_for("auth.login"))

    user = User.query.get(user_id)
    if user is None:
        flash("We could not find that account.", "warning")
        return redirect(url_for("auth.login"))

    if not _can_resend_otp(user.id, OTP_PURPOSE_LOGIN):
        flash("Please wait a moment before requesting another code.", "warning")
        return redirect(url_for("auth.login_verify"))

    code, otp = _issue_otp(user, OTP_PURPOSE_LOGIN, request_id=secrets.token_hex(8))
    db.session.commit()
    _send_login_otp_email(user, code, otp.expires_at)

    flash("A fresh verification code has been emailed to you.", "info")
    return redirect(url_for("auth.login_verify"))


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    session.clear()
    current_app.logger.info("User logged out")
    flash("Signed out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/users/new/sample", methods=["GET"])
@login_required
@tenant_required
@admin_required
def provision_sample_csv():
    sample = """full_name,email,role,status,password
Alice Example,alice@example.com,member,pending,TempPass123!
Bob Admin,bob.admin@example.com,admin,active,SecurePass456!
Cara Member,cara.member@example.com,member,disabled,AnotherPass789!
"""
    return Response(
        sample,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=provision_users_sample.csv"},
    )


@auth_bp.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = ForgotPasswordForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.lower()).first()
        if user and user.is_active:
            token, record = PasswordResetToken.issue(
                user=user,
                ttl_minutes=current_app.config.get("PASSWORD_RESET_EXPIRY_MINUTES", 30),
                secret_bytes=current_app.config.get("PASSWORD_RESET_TOKEN_BYTES", 32),
                request_ip=request.remote_addr,
                user_agent=request.headers.get("User-Agent", "")[:255],
            )
            db.session.commit()
            reset_link = url_for("auth.reset_password", token=token, _external=True)
            _send_reset_email(user, reset_link, record.expires_at)
            current_app.logger.info("Password reset email prepared for %s", user.email)
        else:
            current_app.logger.warning("Password reset requested for unknown or inactive account: %s", form.email.data.lower())

        flash("If your account exists, a reset link has been prepared.", "info")
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html", form=form)


@auth_bp.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    record = PasswordResetToken.verify(token)
    if record is None:
        flash("The reset link is invalid or has expired.", "danger")
        return redirect(url_for("auth.login"))

    if record.is_expired or record.status != "pending":
        record.mark_expired()
        db.session.commit()
        flash("The reset link has expired. Request a new one.", "danger")
        return redirect(url_for("auth.login"))

    user = User.query.get(record.user_id)
    if user is None:
        flash("We could not find that account.", "danger")
        return redirect(url_for("auth.login"))

    organization = user.organization
    if organization is None or organization.status != "active":
        flash("This organization is suspended. Contact support.", "danger")
        return redirect(url_for("auth.login"))
    if not user.is_active:
        flash("This account is disabled. Contact your organization admin.", "danger")
        return redirect(url_for("auth.login"))

    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        record.mark_consumed()
        db.session.commit()
        current_app.logger.info("Password reset completed for user %s", user.email)
        flash("Password updated. You can now sign in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", form=form, token=token)


@auth_bp.route("/users/new", methods=["GET", "POST"])
@login_required
@tenant_required
@admin_required
def provision_user():
    form = ProvisionUserForm()
    bulk_form = BulkProvisionForm()
    organization_id = session.get("org_id") or current_user.organization_id

    is_bulk = (bulk_form.submit_bulk.name in request.form) or bool(
        request.files.get(bulk_form.csv_file.name)
    )

    if is_bulk and bulk_form.validate_on_submit():
        rows, headers, parse_errors = _read_csv_upload(bulk_form.csv_file.data)
        required_headers = {"full_name", "email", "role", "status", "password"}
        missing_headers = required_headers - set(headers)
        if missing_headers:
            parse_errors.append(
                f"Missing required columns: {', '.join(sorted(missing_headers))}."
            )

        if parse_errors:
            flash(" ".join(parse_errors), "danger")
            return render_template("auth/provision_user.html", form=form, bulk_form=bulk_form)

        allowed_roles = {choice[0] for choice in form.role.choices}
        allowed_statuses = {"active", "disabled", "pending"}
        seen_emails: set[str] = set()
        errors: list[str] = []
        candidates: list[dict[str, str]] = []

        for idx, row in enumerate(rows, start=2):
            email = (row.get("email") or "").lower().strip()
            full_name = (row.get("full_name") or "").strip()
            role = (row.get("role") or "").lower().strip()
            status = (row.get("status") or "pending").lower().strip() or "pending"
            password = (row.get("password") or "").strip()
            row_errors: list[str] = []

            if not email or not full_name or not password:
                row_errors.append("full_name, email, and password are required.")

            if len(password) < 8:
                row_errors.append("password must be at least 8 characters.")
            if role not in allowed_roles:
                row_errors.append(f"role must be one of {', '.join(sorted(allowed_roles))}.")
            if status not in allowed_statuses:
                row_errors.append(f"status must be one of {', '.join(sorted(allowed_statuses))}.")
            if email in seen_emails:
                row_errors.append(f"duplicate email in file: {email}.")
            seen_emails.add(email)

            existing_user = User.query.filter_by(email=email).first()
            if existing_user and existing_user.organization_id == organization_id:
                row_errors.append(f"{email} is already in this organization.")

            if row_errors:
                errors.append(f"Row {idx}: " + " ".join(row_errors))
                continue

            candidates.append(
                {
                    "email": email,
                    "full_name": full_name,
                    "role": role,
                    "status": status,
                    "password": password,
                }
            )

        if errors:
            flash(" ".join(errors), "danger")
            return render_template("auth/provision_user.html", form=form, bulk_form=bulk_form)

        if not candidates:
            flash("No valid rows found in the CSV.", "warning")
            return render_template("auth/provision_user.html", form=form, bulk_form=bulk_form)

        if not _enforce_seat_capacity(organization_id, seats_needed=len(candidates)):
            return redirect(url_for("billing.upgrade_page"))

        new_users: list[User] = []
        for candidate in candidates:
            user = User(
                email=candidate["email"],
                full_name=candidate["full_name"],
                role=candidate["role"],
                organization_id=organization_id,
            )
            user.set_password(candidate["password"])

            if candidate["status"] == "active":
                user.activate()
            elif candidate["status"] == "disabled":
                user.disable()
            else:
                user.set_pending()

            new_users.append(user)

        db.session.add_all(new_users)
        db.session.commit()

        current_app.logger.info(
            "Admin %s bulk provisioned %s users", current_user.email, len(new_users)
        )
        flash(f"{len(new_users)} users created from CSV.", "success")
        return redirect(url_for("tenant.user_management"))

    if is_bulk and request.method == "POST" and not bulk_form.validate():
        current_app.logger.warning(
            "Bulk provision validation failed", extra={"errors": bulk_form.errors, "form_keys": list(request.form.keys())}
        )
        _flash_form_errors(bulk_form, prefix="Bulk upload failed validation.")
        return render_template("auth/provision_user.html", form=form, bulk_form=bulk_form)

    if (not is_bulk) and form.validate_on_submit():
        email = form.email.data.lower()
        if User.query.filter_by(email=email).first():
            flash("A user with that email already exists.", "warning")
            return render_template("auth/provision_user.html", form=form, bulk_form=bulk_form)

        if not _enforce_seat_capacity(organization_id, seats_needed=1):
            return redirect(url_for("billing.upgrade_page"))

        user = User(
            email=email,
            full_name=form.full_name.data.strip(),
            role=form.role.data,
            organization_id=organization_id,
        )
        user.set_password(form.password.data)

        status = form.status.data
        if status == "active":
            user.activate()
        elif status == "disabled":
            user.disable()
        else:
            user.set_pending()

        db.session.add(user)
        db.session.commit()

        current_app.logger.info("Admin %s provisioned user %s", current_user.email, user.email)

        flash("User created. Share credentials securely.", "success")
        return redirect(url_for("main.dashboard"))

    if (not is_bulk) and request.method == "POST" and not form.validate():
        current_app.logger.warning(
            "Single provision validation failed", extra={"errors": form.errors, "form_keys": list(request.form.keys())}
        )
        _flash_form_errors(form, prefix="User creation failed validation.")
        return render_template("auth/provision_user.html", form=form, bulk_form=bulk_form)

    return render_template("auth/provision_user.html", form=form, bulk_form=bulk_form)


@auth_bp.route("/onboarding", methods=["GET", "POST"])
@login_required
@tenant_required
@admin_required
def onboarding():
    organization = current_user.organization
    form = OrganizationProfileForm(
        organization_name=organization.name,
        industry=organization.industry,
        timezone=organization.timezone,
        size=organization.size or "11-50",
    )

    if form.validate_on_submit():
        desired_name = form.organization_name.data.strip()
        conflict = (
            Organization.query.filter_by(name=desired_name)
            .filter(Organization.id != organization.id)
            .first()
        )
        if conflict:
            flash("Another organization already uses that name.", "warning")
            return render_template("auth/onboarding.html", form=form, organization=organization)

        organization.name = desired_name
        organization.industry = (form.industry.data or "").strip() or None
        organization.timezone = (form.timezone.data or "").strip() or None
        organization.size = form.size.data
        organization.mark_onboarding_complete()

        db.session.commit()
        current_app.logger.info("Organization %s onboarding completed by %s", organization.id, current_user.email)
        flash("Onboarding complete. Welcome to your workspace!", "success")
        return redirect(url_for("main.dashboard"))

    return render_template("auth/onboarding.html", form=form, organization=organization)


@auth_bp.route("/invite/sample", methods=["GET"])
@login_required
@tenant_required
@admin_required
def invite_sample_csv():
    sample = """full_name,email,role
Dana Member,dana.member@example.com,member
Evan Admin,evan.admin@example.com,admin
Fiona Member,fiona.member@example.com,member
"""
    return Response(
        sample,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=invite_users_sample.csv"},
    )


@auth_bp.route("/invite", methods=["GET", "POST"])
@login_required
@tenant_required
@admin_required
def invite_user():
    form = InviteUserForm()
    bulk_form = BulkInviteForm()
    organization_id = session.get("org_id") or current_user.organization_id

    is_bulk = (bulk_form.submit_bulk.name in request.form) or bool(
        request.files.get(bulk_form.csv_file.name)
    )

    if is_bulk and bulk_form.validate_on_submit():
        rows, headers, parse_errors = _read_csv_upload(bulk_form.csv_file.data)
        required_headers = {"full_name", "email", "role"}
        missing_headers = required_headers - set(headers)
        if missing_headers:
            parse_errors.append(
                f"Missing required columns: {', '.join(sorted(missing_headers))}."
            )

        if parse_errors:
            flash(" ".join(parse_errors), "danger")
            return render_template("auth/invite_user.html", form=form, bulk_form=bulk_form)

        allowed_roles = {choice[0] for choice in form.role.choices}
        seen_emails: set[str] = set()
        errors: list[str] = []
        candidates: list[dict[str, str]] = []

        for idx, row in enumerate(rows, start=2):
            email = (row.get("email") or "").lower().strip()
            full_name = (row.get("full_name") or "").strip()
            role = (row.get("role") or "").lower().strip()
            row_errors: list[str] = []

            if not email or not full_name:
                row_errors.append("full_name and email are required.")
            if role not in allowed_roles:
                row_errors.append(f"role must be one of {', '.join(sorted(allowed_roles))}.")
            if email in seen_emails:
                row_errors.append(f"duplicate email in file: {email}.")
            seen_emails.add(email)

            if User.query.filter_by(email=email).first():
                row_errors.append(f"a user with {email} already exists.")

            existing_pending = UserInvitation.query.filter_by(
                email=email,
                organization_id=organization_id,
                status="pending",
            ).first()
            if existing_pending:
                row_errors.append(f"pending invitation already exists for {email}.")

            if row_errors:
                errors.append(f"Row {idx}: " + " ".join(row_errors))
                continue

            candidates.append(
                {
                    "email": email,
                    "full_name": full_name,
                    "role": role,
                }
            )

        if errors:
            flash(" ".join(errors), "danger")
            return render_template("auth/invite_user.html", form=form, bulk_form=bulk_form)

        if not candidates:
            flash("No valid rows found in the CSV.", "warning")
            return render_template("auth/invite_user.html", form=form, bulk_form=bulk_form)

        if not _enforce_seat_capacity(organization_id, seats_needed=len(candidates)):
            return redirect(url_for("billing.upgrade_page"))

        invitations: list[UserInvitation] = []
        for candidate in candidates:
            token, expires_at = User.generate_invitation_token()
            invitation = UserInvitation(
                email=candidate["email"],
                full_name=candidate["full_name"],
                role=candidate["role"],
                organization_id=organization_id,
                token=token,
                expires_at=expires_at,
                invited_by_id=current_user.id,
            )
            invitations.append(invitation)

        db.session.add_all(invitations)
        db.session.commit()

        current_app.logger.info(
            "Admin %s bulk invited %s users", current_user.email, len(invitations)
        )
        flash(f"{len(invitations)} invitations created from CSV.", "success")
        return redirect(url_for("tenant.user_management"))

    if is_bulk and request.method == "POST" and not bulk_form.validate():
        current_app.logger.warning(
            "Bulk invite validation failed", extra={"errors": bulk_form.errors, "form_keys": list(request.form.keys())}
        )
        _flash_form_errors(bulk_form, prefix="Bulk invite failed validation.")
        return render_template("auth/invite_user.html", form=form, bulk_form=bulk_form)

    if (not is_bulk) and form.validate_on_submit():
        email = form.email.data.lower()

        existing_user = User.query.filter_by(email=email).first()
        if existing_user and existing_user.organization_id == organization_id:
            flash("That user is already part of this organization.", "info")
            return redirect(url_for("tenant.user_management"))

        existing_pending = UserInvitation.query.filter_by(
            email=email,
            organization_id=organization_id,
            status="pending",
        ).first()
        if existing_pending:
            flash("There is already a pending invite for this email.", "info")
            return redirect(url_for("auth.invite_user"))

        if not _enforce_seat_capacity(organization_id, seats_needed=1):
            return redirect(url_for("billing.upgrade_page"))

        token, expires_at = User.generate_invitation_token()
        invitation = UserInvitation(
            email=email,
            full_name=form.full_name.data.strip(),
            role=form.role.data,
            organization_id=organization_id,
            token=token,
            expires_at=expires_at,
            invited_by_id=current_user.id,
        )

        db.session.add(invitation)
        db.session.commit()

        invite_link = url_for("auth.accept_invite", token=token, _external=True)
        current_app.logger.info("Invitation link for %s: %s", email, invite_link)
        flash("Invitation created. Share the link securely.", "success")
        return redirect(url_for("tenant.user_management"))

    if (not is_bulk) and request.method == "POST" and not form.validate():
        current_app.logger.warning(
            "Single invite validation failed", extra={"errors": form.errors, "form_keys": list(request.form.keys())}
        )
        _flash_form_errors(form, prefix="Invite failed validation.")
        return render_template("auth/invite_user.html", form=form, bulk_form=bulk_form)

    return render_template("auth/invite_user.html", form=form, bulk_form=bulk_form)


@auth_bp.route("/invite/accept/<token>", methods=["GET", "POST"])
def accept_invite(token: str):
    invitation = UserInvitation.query.filter_by(token=token).first()
    if invitation is None:
        flash("This invitation is no longer valid.", "danger")
        return redirect(url_for("auth.login"))
    organization = invitation.organization
    if organization is None or organization.status != "active":
        flash("This organization is suspended. Contact support.", "danger")
        return redirect(url_for("auth.login"))
    if invitation.expires_at < datetime.utcnow():
        invitation.mark_expired()
        db.session.commit()
        flash("This invitation has expired.", "danger")
        return redirect(url_for("auth.login"))
    if not invitation.is_valid():
        flash("This invitation is no longer valid.", "danger")
        return redirect(url_for("auth.login"))

    form = AcceptInviteForm(full_name=invitation.full_name)

    if form.validate_on_submit():
        existing_user = User.query.filter_by(email=invitation.email.lower()).first()
        if existing_user:
            if existing_user.invitation_id == invitation.id and existing_user.verification_status != "verified":
                if not _can_resend_otp(existing_user.id, OTP_PURPOSE_INVITE):
                    flash("We recently sent you a code. Check your email or try again soon.", "info")
                    session["invite_verification_user_id"] = existing_user.id
                    return redirect(url_for("auth.verify_invite"))

                code, otp = _issue_otp(existing_user, OTP_PURPOSE_INVITE, request_id=secrets.token_hex(8))
                db.session.commit()
                _send_invite_otp_email(existing_user, invitation.organization.name, code, otp.expires_at)
                session["invite_verification_user_id"] = existing_user.id
                flash("A fresh verification code has been emailed.", "info")
                return redirect(url_for("auth.verify_invite"))

            flash("An account already exists for this email.", "warning")
            invitation.mark_expired()
            db.session.commit()
            return redirect(url_for("auth.login"))

        if not _enforce_seat_capacity(invitation.organization_id, seats_needed=1):
            flash("Seat limit reached. Ask your admin to purchase more seats.", "warning")
            return redirect(url_for("billing.upgrade_page"))

        user = User(
            email=invitation.email.lower(),
            full_name=form.full_name.data.strip(),
            role=invitation.role,
            organization_id=invitation.organization_id,
            invitation_id=invitation.id,
        )
        user.set_password(form.password.data)
        user.invited_at = invitation.created_at
        user.set_pending()

        db.session.add(user)
        db.session.flush()

        code, otp = _issue_otp(user, OTP_PURPOSE_INVITE, request_id=secrets.token_hex(8))
        db.session.commit()

        _send_invite_otp_email(user, invitation.organization.name, code, otp.expires_at)

        session["invite_verification_user_id"] = user.id
        flash("Enter the verification code sent to your email to finish joining.", "info")
        return redirect(url_for("auth.verify_invite"))

    return render_template("auth/accept_invite.html", form=form, invitation=invitation)


@auth_bp.route("/invite/verify", methods=["GET", "POST"])
def verify_invite():
    user_id = session.get("invite_verification_user_id")
    if not user_id:
        flash("No invitation verification is pending.", "warning")
        return redirect(url_for("auth.login"))

    user = User.query.get(user_id)
    if user is None:
        session.pop("invite_verification_user_id", None)
        flash("We could not find that account.", "warning")
        return redirect(url_for("auth.login"))

    invitation = UserInvitation.query.get(user.invitation_id) if user.invitation_id else None
    if invitation is None or not invitation.is_valid():
        flash("This invitation is no longer valid.", "danger")
        session.pop("invite_verification_user_id", None)
        return redirect(url_for("auth.login"))

    latest = _latest_otp(user.id, OTP_PURPOSE_INVITE)
    form = OTPVerifyForm()

    if form.validate_on_submit():
        if latest is None:
            flash("Your code expired. Request a new invitation.", "danger")
            return redirect(url_for("auth.login"))

        success = latest.check_code(form.otp.data.strip())
        db.session.commit()

        if success:
            invitation.mark_accepted()
            user.mark_email_verified()
            db.session.commit()

            login_user(user, remember=True)
            session["org_id"] = user.organization_id
            session.pop("invite_verification_user_id", None)
            flash("Invitation verified. Welcome aboard!", "success")
            return redirect(url_for("main.dashboard"))

        if latest.status == "locked":
            flash("Too many incorrect attempts. Restart with a new invitation.", "danger")
            return redirect(url_for("auth.login"))
        if latest.is_expired or latest.status == "expired":
            flash("That code expired. Request a fresh invite.", "warning")
            return redirect(url_for("auth.login"))
        flash(f"Incorrect code. {latest.remaining_attempts} attempts remaining.", "danger")

    return render_template("auth/login_otp.html", form=form, user=user, latest=latest)
