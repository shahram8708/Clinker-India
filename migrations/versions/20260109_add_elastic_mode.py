"""allow elastic mode in optimization_jobs"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260109_add_elastic_mode"
down_revision = "c1f7b6a9d3e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("optimization_jobs", schema=None) as batch_op:
        batch_op.drop_constraint("ck_opt_job_mode", type_="check")
        batch_op.create_check_constraint(
            "ck_opt_job_mode",
            "mode IN ('elastic','deterministic','stochastic','robust')",
        )


def downgrade() -> None:
    with op.batch_alter_table("optimization_jobs", schema=None) as batch_op:
        batch_op.drop_constraint("ck_opt_job_mode", type_="check")
        batch_op.create_check_constraint(
            "ck_opt_job_mode",
            "mode IN ('deterministic','stochastic','robust')",
        )
