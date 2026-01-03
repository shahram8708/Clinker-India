"""Add production_cost column to plants

Revision ID: 20251228_add_plant_production_cost
Revises: 20251228_invite_otp
Create Date: 2025-12-28 19:05:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "20251228_add_plant_production_cost"
down_revision = "20251228_invite_otp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("plants")}
    if "production_cost" not in columns:
        op.add_column(
            "plants",
            sa.Column("production_cost", sa.Numeric(12, 2), nullable=False, server_default="0"),
        )
        op.execute("UPDATE plants SET production_cost = 0")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("plants")}
    if "production_cost" in columns:
        with op.batch_alter_table("plants", recreate="always") as batch_op:
            batch_op.drop_column("production_cost")
