"""Configuration classes for multiple environments."""
import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

# Ensure .env values are loaded before class attributes are evaluated.
load_dotenv()


class BaseConfig:
    BASE_DIR = Path(__file__).resolve().parent.parent
    SECRET_KEY = os.getenv("SECRET_KEY")
    if not SECRET_KEY or SECRET_KEY == "change-me-now":
        raise RuntimeError("SECRET_KEY environment variable must be set to a strong random value")
    _DEFAULT_DB_PATH = BASE_DIR / "instance" / "app.db"
    _RAW_DATABASE_URL = os.getenv("DATABASE_URL")

    if _RAW_DATABASE_URL:
        if _RAW_DATABASE_URL.startswith("sqlite:///"):
            # Normalize relative sqlite paths to absolute for reliability on Windows/OneDrive.
            relative = Path(_RAW_DATABASE_URL.removeprefix("sqlite:///"))
            db_path = relative if relative.is_absolute() else (BASE_DIR / relative)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
        else:
            SQLALCHEMY_DATABASE_URI = _RAW_DATABASE_URL
    else:
        _DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{_DEFAULT_DB_PATH}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    SESSION_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SAMESITE = "Lax"
    PREFERRED_URL_SCHEME = "https"

    # Keep users logged in for one year unless they explicitly sign out.
    REMEMBER_COOKIE_DURATION = timedelta(days=365)

    SESSION_REFRESH_EACH_REQUEST = True
    WTF_CSRF_ENABLED = True

    WTF_CSRF_TIME_LIMIT = None

    # OTP + password reset security
    OTP_LENGTH = int(os.getenv("OTP_LENGTH", "6"))
    OTP_EXPIRY_MINUTES = int(os.getenv("OTP_EXPIRY_MINUTES", "10"))
    OTP_RESEND_SECONDS = int(os.getenv("OTP_RESEND_SECONDS", "60"))
    OTP_MAX_ATTEMPTS = int(os.getenv("OTP_MAX_ATTEMPTS", "5"))
    LOGIN_OTP_COOLDOWN_SECONDS = int(os.getenv("LOGIN_OTP_COOLDOWN_SECONDS", "30"))
    PASSWORD_RESET_EXPIRY_MINUTES = int(os.getenv("PASSWORD_RESET_EXPIRY_MINUTES", "30"))
    PASSWORD_RESET_TOKEN_BYTES = int(os.getenv("PASSWORD_RESET_TOKEN_BYTES", "32"))

    # Super admin controls
    SUPER_ADMIN_EMAIL = os.getenv("SUPER_ADMIN_EMAIL")
    SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD")
    SUPER_ADMIN_PASSWORD_IS_HASHED = _as_bool(os.getenv("SUPER_ADMIN_PASSWORD_IS_HASHED", "false"), False)
    SUPER_ADMIN_OTP_LENGTH = int(os.getenv("SUPER_ADMIN_OTP_LENGTH", "6"))
    SUPER_ADMIN_OTP_EXPIRY_MINUTES = int(os.getenv("SUPER_ADMIN_OTP_EXPIRY_MINUTES", "8"))
    SUPER_ADMIN_OTP_RESEND_SECONDS = int(os.getenv("SUPER_ADMIN_OTP_RESEND_SECONDS", "60"))
    SUPER_ADMIN_OTP_MAX_ATTEMPTS = int(os.getenv("SUPER_ADMIN_OTP_MAX_ATTEMPTS", "5"))
    SUPER_ADMIN_RATE_LIMIT_PER_MINUTE = int(os.getenv("SUPER_ADMIN_RATE_LIMIT_PER_MINUTE", "5"))

    # Mail settings (TLS-first, production-ready)
    MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USE_TLS = _as_bool(os.getenv("MAIL_USE_TLS", "true"), True)
    MAIL_USE_SSL = _as_bool(os.getenv("MAIL_USE_SSL", "false"), False)
    MAIL_USERNAME = os.getenv("MAIL_USERNAME")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER", "Clinker India <no-reply@clinker.ind.in>")
    MAIL_SUPPRESS_SEND = _as_bool(os.getenv("MAIL_SUPPRESS_SEND", "false"), False)
    MAIL_MAX_EMAILS = int(os.getenv("MAIL_MAX_EMAILS", "100"))
    MAIL_ASCII_ATTACHMENTS = True

    SUPPORT_ADMIN_EMAIL = os.getenv("SUPPORT_ADMIN_EMAIL", "support@clinker.ind.in")

    # Pricing + Billing
    DEFAULT_PLAN_CODE = os.getenv("DEFAULT_PLAN_CODE", "CLINKER_PRO_INDIA")
    PRICING_BASE_AMOUNT_INR = float(os.getenv("PRICING_BASE_AMOUNT_INR", "9999"))
    PRICING_PER_SEAT_INR = float(os.getenv("PRICING_PER_SEAT_INR", "499"))
    GST_RATE = float(os.getenv("GST_RATE", "0.18"))

    RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
    RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
    RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")


class DevelopmentConfig(BaseConfig):
    DEBUG = True


class ProductionConfig(BaseConfig):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
