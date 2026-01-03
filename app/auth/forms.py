"""Authentication forms using Flask-WTF."""
from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from wtforms import (
    BooleanField,
    EmailField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
)
from wtforms.validators import Email, EqualTo, InputRequired, Length


class RegisterForm(FlaskForm):
    organization_name = StringField(
        "Organization Name",
        validators=[InputRequired(), Length(min=2, max=120)],
    )
    industry = StringField("Industry", validators=[Length(max=120)])
    admin_name = StringField(
        "Admin Full Name",
        validators=[InputRequired(), Length(min=2, max=120)],
    )
    email = EmailField("Work Email", validators=[InputRequired(), Email()])
    password = PasswordField(
        "Password",
        validators=[InputRequired(), Length(min=8)],
    )
    confirm_password = PasswordField(
        "Confirm Password",
        validators=[InputRequired(), EqualTo("password", message="Passwords must match")],
    )
    submit = SubmitField("Create Account")


class LoginForm(FlaskForm):
    email = EmailField("Email", validators=[InputRequired(), Email()])
    password = PasswordField("Password", validators=[InputRequired()])
    remember_me = BooleanField("Remember me")
    submit = SubmitField("Sign In")


class OTPVerifyForm(FlaskForm):
    otp = StringField(
        "One-Time Passcode",
        validators=[InputRequired(), Length(min=6, max=8)],
    )
    submit = SubmitField("Verify")


class ForgotPasswordForm(FlaskForm):
    email = EmailField("Work Email", validators=[InputRequired(), Email()])
    submit = SubmitField("Send Reset Link")


class ResetPasswordForm(FlaskForm):
    password = PasswordField(
        "New Password",
        validators=[InputRequired(), Length(min=8)],
    )
    confirm_password = PasswordField(
        "Confirm New Password",
        validators=[InputRequired(), EqualTo("password", message="Passwords must match")],
    )
    submit = SubmitField("Update Password")


class ProvisionUserForm(FlaskForm):
    full_name = StringField("Full Name", validators=[InputRequired(), Length(min=2, max=255)])
    email = EmailField("User Email", validators=[InputRequired(), Email()])
    role = SelectField(
        "Role",
        choices=[("member", "Member"), ("admin", "Admin")],
        validators=[InputRequired()],
    )
    status = SelectField(
        "Status",
        choices=[("active", "Active"), ("disabled", "Disabled"), ("pending", "Pending")],
        validators=[InputRequired()],
    )
    password = PasswordField(
        "Temporary Password",
        validators=[InputRequired(), Length(min=8)],
    )
    confirm_password = PasswordField(
        "Confirm Password",
        validators=[InputRequired(), EqualTo("password", message="Passwords must match")],
    )
    submit = SubmitField("Create User")


class BulkProvisionForm(FlaskForm):
    csv_file = FileField(
        "Bulk upload CSV",
        validators=[FileRequired(), FileAllowed(["csv"], "CSV files only.")],
    )
    submit_bulk = SubmitField("Upload CSV")


class InviteUserForm(FlaskForm):
    full_name = StringField("Full Name", validators=[InputRequired(), Length(min=2, max=255)])
    email = EmailField("User Email", validators=[InputRequired(), Email()])
    role = SelectField(
        "Role",
        choices=[("member", "Member"), ("admin", "Admin")],
        validators=[InputRequired()],
    )
    submit = SubmitField("Send Invitation")


class BulkInviteForm(FlaskForm):
    csv_file = FileField(
        "Bulk invite CSV",
        validators=[FileRequired(), FileAllowed(["csv"], "CSV files only.")],
    )
    submit_bulk = SubmitField("Upload CSV")


class AcceptInviteForm(FlaskForm):
    full_name = StringField("Full Name", validators=[InputRequired(), Length(min=2, max=255)])
    password = PasswordField(
        "Create Password",
        validators=[InputRequired(), Length(min=8)],
    )
    confirm_password = PasswordField(
        "Confirm Password",
        validators=[InputRequired(), EqualTo("password", message="Passwords must match")],
    )
    submit = SubmitField("Join Workspace")


class OrganizationProfileForm(FlaskForm):
    organization_name = StringField("Organization Name", validators=[InputRequired(), Length(min=2, max=120)])
    industry = StringField("Industry", validators=[Length(max=120)])
    timezone = StringField("Timezone", validators=[Length(max=64)])
    size = SelectField(
        "Company Size",
        choices=[
            ("1-10", "1-10"),
            ("11-50", "11-50"),
            ("51-200", "51-200"),
            ("201-1000", "201-1000"),
            ("1000+", "1000+"),
        ],
        validators=[InputRequired()],
    )
    submit = SubmitField("Save and Continue")


class UserRoleUpdateForm(FlaskForm):
    role = SelectField(
        "Role",
        choices=[("member", "Member"), ("admin", "Admin"), ("owner", "Owner")],
        validators=[InputRequired()],
    )
    submit = SubmitField("Update Role")


class UserStatusUpdateForm(FlaskForm):
    status = SelectField(
        "Status",
        choices=[("active", "Active"), ("disabled", "Disabled")],
        validators=[InputRequired()],
    )
    submit = SubmitField("Update Status")
