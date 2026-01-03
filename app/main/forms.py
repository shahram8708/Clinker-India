"""Marketing and support-facing forms."""
from flask_wtf import FlaskForm
from wtforms import EmailField, HiddenField, SelectField, StringField, TextAreaField
from wtforms.validators import Email, InputRequired, Length


SUPPORT_CATEGORIES = [
    ("technical_issue", "Technical Issue"),
    ("billing_payment", "Billing / Payment Support"),
    ("subscription", "Organization / Subscription Help"),
    ("feature_request", "Feature Request"),
    ("bug_report", "Bug Report"),
    ("general", "General Inquiry"),
    ("partnership", "Business / Partnership"),
    ("other", "Other"),
]


class ContactSupportForm(FlaskForm):
    full_name = StringField(
        "Full Name",
        validators=[InputRequired(), Length(min=2, max=120)],
    )
    email = EmailField("Email Address", validators=[InputRequired(), Email(), Length(max=255)])
    subject = StringField("Subject", validators=[InputRequired(), Length(min=4, max=180)])
    category = SelectField(
        "Support Category",
        choices=SUPPORT_CATEGORIES,
        validators=[InputRequired()],
        default="technical_issue",
    )
    message = TextAreaField(
        "Message",
        validators=[InputRequired(), Length(min=20, max=4000)],
    )
    honeypot = HiddenField("Leave blank")

    def is_spam(self) -> bool:
        return bool((self.honeypot.data or "").strip())
