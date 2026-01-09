"""bind optimization data to workspaces

Revision ID: 20260109_workspace_isolation
Revises: 20260109_add_elastic_mode
Create Date: 2026-01-09
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260109_workspace_isolation"
down_revision = "20260109_add_elastic_mode"
branch_labels = None
depends_on = None


PLANNING_UNIQUE_OLD = "uq_scenario_name_per_org"
PLANNING_UNIQUE_NEW = "uq_scenario_name_per_workspace"


def upgrade() -> None:
    # Clean up any leftover temp tables from prior failed batch operations (SQLite safety)
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_planning_scenarios")

    # planning_scenarios: add workspace_id + scoped uniqueness
    with op.batch_alter_table("planning_scenarios", schema=None) as batch_op:
        batch_op.add_column(sa.Column("workspace_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_planning_scenarios_workspace_id", ["workspace_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_planning_scenarios_workspace_id",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.drop_constraint(PLANNING_UNIQUE_OLD, type_="unique")

    # optimization_jobs: add workspace_id
    with op.batch_alter_table("optimization_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("workspace_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_optimization_jobs_workspace_id", ["workspace_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_optimization_jobs_workspace_id",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # optimization_results: add workspace_id
    with op.batch_alter_table("optimization_results", schema=None) as batch_op:
        batch_op.add_column(sa.Column("workspace_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_optimization_results_workspace_id", ["workspace_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_optimization_results_workspace_id",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # backfill workspace_id values after columns exist (SQLite requires this ordering)
    op.execute(
        """
        UPDATE planning_scenarios
        SET workspace_id = (
                SELECT wsd.workspace_id
                FROM workspace_datasets AS wsd
                WHERE wsd.planning_scenario_id = planning_scenarios.id
                LIMIT 1
        )
        WHERE workspace_id IS NULL
            AND EXISTS (
                SELECT 1 FROM workspace_datasets wsd WHERE wsd.planning_scenario_id = planning_scenarios.id
            )
        """
    )

    # fallback: for any remaining scenarios, pick the first workspace in the same org
    op.execute(
        """
        UPDATE planning_scenarios
        SET workspace_id = (
                SELECT MIN(id) FROM workspaces w WHERE w.organization_id = planning_scenarios.organization_id
        )
        WHERE workspace_id IS NULL
            AND organization_id IS NOT NULL
        """
    )

    # last resort: if any scenarios still lack org linkage, assign the earliest workspace overall
    op.execute(
        """
        UPDATE planning_scenarios
        SET workspace_id = (SELECT MIN(id) FROM workspaces)
        WHERE workspace_id IS NULL
        """
    )

    # backfill jobs from scenarios
    op.execute(
        """
        UPDATE optimization_jobs
        SET workspace_id = (
                SELECT ps.workspace_id FROM planning_scenarios ps WHERE ps.id = optimization_jobs.scenario_id
        )
        WHERE workspace_id IS NULL
            AND EXISTS (SELECT 1 FROM planning_scenarios ps WHERE ps.id = optimization_jobs.scenario_id)
        """
    )

    # fallback jobs: if any remain null, pick earliest workspace overall
    op.execute(
        """
        UPDATE optimization_jobs
        SET workspace_id = (SELECT MIN(id) FROM workspaces)
        WHERE workspace_id IS NULL
        """
    )

    # backfill results from jobs (primary) or scenarios (secondary)
    op.execute(
        """
        UPDATE optimization_results
        SET workspace_id = (
                SELECT oj.workspace_id FROM optimization_jobs oj WHERE oj.id = optimization_results.job_id
        )
        WHERE workspace_id IS NULL
            AND EXISTS (SELECT 1 FROM optimization_jobs oj WHERE oj.id = optimization_results.job_id)
        """
    )
    op.execute(
        """
        UPDATE optimization_results
        SET workspace_id = (
                SELECT ps.workspace_id FROM planning_scenarios ps WHERE ps.id = optimization_results.scenario_id
        )
        WHERE workspace_id IS NULL
            AND EXISTS (SELECT 1 FROM planning_scenarios ps WHERE ps.id = optimization_results.scenario_id)
        """
    )

    # fallback results: if any remain null, pick earliest workspace overall
    op.execute(
        """
        UPDATE optimization_results
        SET workspace_id = (SELECT MIN(id) FROM workspaces)
        WHERE workspace_id IS NULL
        """
    )

    # enforce non-null after backfill
    with op.batch_alter_table("planning_scenarios", schema=None) as batch_op:
        batch_op.alter_column("workspace_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_unique_constraint(
            PLANNING_UNIQUE_NEW,
            ["organization_id", "workspace_id", "scenario_name"],
        )

    with op.batch_alter_table("optimization_jobs", schema=None) as batch_op:
        batch_op.alter_column("workspace_id", existing_type=sa.Integer(), nullable=False)

    with op.batch_alter_table("optimization_results", schema=None) as batch_op:
        batch_op.alter_column("workspace_id", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    # drop new constraints/indexes and columns in reverse order
    with op.batch_alter_table("optimization_results", schema=None) as batch_op:
        batch_op.drop_constraint("fk_optimization_results_workspace_id", type_="foreignkey")
        batch_op.drop_index("ix_optimization_results_workspace_id")
        batch_op.drop_column("workspace_id")

    with op.batch_alter_table("optimization_jobs", schema=None) as batch_op:
        batch_op.drop_constraint("fk_optimization_jobs_workspace_id", type_="foreignkey")
        batch_op.drop_index("ix_optimization_jobs_workspace_id")
        batch_op.drop_column("workspace_id")

    with op.batch_alter_table("planning_scenarios", schema=None) as batch_op:
        batch_op.drop_constraint(PLANNING_UNIQUE_NEW, type_="unique")
        batch_op.alter_column("workspace_id", existing_type=sa.Integer(), nullable=True)
        batch_op.drop_constraint("fk_planning_scenarios_workspace_id", type_="foreignkey")
        batch_op.drop_index("ix_planning_scenarios_workspace_id")
        batch_op.drop_column("workspace_id")
        batch_op.create_unique_constraint(PLANNING_UNIQUE_OLD, ["organization_id", "scenario_name"])
