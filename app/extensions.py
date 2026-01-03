"""Centralized extension instances."""
from flask_login import LoginManager
from flask_mail import Mail
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

# Extensions are instantiated here and initialized in the app factory.
db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()
mail = Mail()
migrate = Migrate()

# Login manager defaults hardened for security UX.
login_manager.login_message_category = "warning"
login_manager.needs_refresh_message_category = "info"
