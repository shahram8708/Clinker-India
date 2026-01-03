"""Flask application factory and blueprint registration."""
import logging
import os

from dotenv import load_dotenv
from flask import Flask, current_app, render_template
from flask_wtf.csrf import CSRFError

from .config import DevelopmentConfig, ProductionConfig
from .extensions import csrf, db, login_manager, mail, migrate
from .auth.super_admin import SUPER_ADMIN_SESSION_ID, SuperAdminIdentity, super_admin_configured


def create_app(config_name: str | None = None) -> Flask:
    """Application factory with sensible defaults for SaaS multi-tenancy."""
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True)

    _configure_app(app, config_name)
    _configure_logging(app)
    _ensure_instance_folder(app)
    _init_extensions(app)
    _register_tenant_enforcement(app)
    _register_blueprints(app)
    _register_request_hooks(app)
    _register_error_handlers(app)

    return app


def _configure_app(app: Flask, config_name: str | None) -> None:
    config_map = {
        "development": DevelopmentConfig,
        "production": ProductionConfig,
    }
    selected = (config_name or os.getenv("FLASK_ENV") or "development").lower()
    app.config.from_object(config_map.get(selected, DevelopmentConfig))

    # Optional instance-specific overrides (ignored if missing).
    app.config.from_pyfile("settings.py", silent=True)


def _configure_logging(app: Flask) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    app.logger.setLevel(logging.INFO)


def _ensure_instance_folder(app: Flask) -> None:
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError:
        # If the instance path cannot be created, fail loudly in logs.
        app.logger.exception("Could not ensure instance folder exists")


def _init_extensions(app: Flask) -> None:
    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    mail.init_app(app)
    migrate.init_app(app, db)

    from .models import User  # noqa: WPS433 (import inside function is intentional)

    @login_manager.user_loader
    def load_user(user_id: str):
        if user_id == SUPER_ADMIN_SESSION_ID:
            if not super_admin_configured():
                return None
            return SuperAdminIdentity(email=current_app.config.get("SUPER_ADMIN_EMAIL", ""))
        if user_id.isdigit():
            return User.query.get(int(user_id))
        return None

    login_manager.login_view = "auth.login"
    login_manager.session_protection = "strong"


def _register_tenant_enforcement(app: Flask) -> None:
    from .models import TenantOwnedMixin
    from .tenant.utils import register_tenant_criteria

    with app.app_context():
        register_tenant_criteria(db.session, TenantOwnedMixin)


def _register_blueprints(app: Flask) -> None:
    from .auth.routes import auth_bp
    from .billing.routes import billing_bp
    from .main.routes import main_bp
    from .operations.routes import ops_bp
    from .tenant.routes import tenant_bp
    from .superadmin.routes import superadmin_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(billing_bp, url_prefix="/billing")
    app.register_blueprint(main_bp)
    app.register_blueprint(ops_bp, url_prefix="/ops")
    app.register_blueprint(tenant_bp, url_prefix="/org")
    app.register_blueprint(superadmin_bp)


def _register_request_hooks(app: Flask) -> None:
    from flask import abort, session
    from flask_login import current_user, logout_user

    @app.before_request
    def sync_tenant_context():
        """Persist and validate the active tenant for every request."""
        if getattr(current_user, "is_super_admin", False):
            session.pop("org_id", None)
            return None
        if current_user.is_authenticated:
            org_id = getattr(current_user, "organization_id", None)
            organization = getattr(current_user, "organization", None)

            if org_id is None or organization is None:
                logout_user()
                session.clear()
                return ("Organization context missing", 403)

            session["org_id"] = org_id

            if organization.status != "active":
                logout_user()
                session.clear()
                return ("Organization suspended", 403)
            if not getattr(current_user, "is_active", True):
                logout_user()
                session.clear()
                return ("Account disabled", 403)

            session_org_id = session.get("org_id")
            if session_org_id is not None and session_org_id != org_id:
                logout_user()
                session.clear()
                abort(403)
        else:
            session.pop("org_id", None)

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Cache-Control", "no-store")
        return response


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(401)
    def unauthorized(error):  # type: ignore[unused-argument]
        return render_template("errors/401.html"), 401

    @app.errorhandler(403)
    def forbidden(error):  # type: ignore[unused-argument]
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(error):  # type: ignore[unused-argument]
        return render_template("errors/404.html"), 404

    @app.errorhandler(CSRFError)
    def csrf_blocked(error):  # type: ignore[unused-argument]
        app.logger.warning("CSRF validation failed: %s", getattr(error, "description", "unknown"))
        return render_template("errors/403.html", message="Security validation failed. Please try again."), 403

    @app.errorhandler(500)
    def server_error(error):  # type: ignore[unused-argument]
        app.logger.exception("Unhandled server error")
        return render_template("errors/500.html"), 500
