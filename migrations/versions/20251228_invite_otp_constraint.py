"""add invite otp purpose

Revision ID: 20251228_invite_otp
Revises: 
Create Date: 2025-12-28 10:50:00
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251228_invite_otp"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite requires batch mode to recreate table when altering check constraints.
    with op.batch_alter_table("email_otps", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_otp_purpose", type_="check")
        batch_op.create_check_constraint(
            "ck_otp_purpose",
            "purpose IN ('registration', 'login', 'invite')",
        )


def downgrade() -> None:
    with op.batch_alter_table("email_otps", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_otp_purpose", type_="check")
        batch_op.create_check_constraint(
            "ck_otp_purpose",
            "purpose IN ('registration', 'login')",
        )
