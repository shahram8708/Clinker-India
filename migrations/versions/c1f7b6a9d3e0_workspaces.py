"""Add workspaces and dataset mapping

Revision ID: c1f7b6a9d3e0
Revises: 23a8a1221c4c
Create Date: 2026-01-09 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1f7b6a9d3e0'
down_revision = 'ab3ac8b80ee3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'workspaces',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('organization_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id', 'name', name='uq_workspace_name_per_org')
    )
    op.create_index('ix_workspaces_org_id', 'workspaces', ['organization_id'])

    op.create_table(
        'workspace_datasets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('organization_id', sa.Integer(), nullable=False),
        sa.Column('workspace_id', sa.Integer(), nullable=False),
        sa.Column('planning_scenario_id', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=120), nullable=False),
        sa.Column('notes', sa.String(length=255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['planning_scenario_id'], ['planning_scenarios.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id', 'workspace_id', 'label', name='uq_dataset_label_per_workspace'),
        sa.UniqueConstraint('planning_scenario_id', name='uq_dataset_scenario_unique')
    )
    op.create_index('ix_workspace_datasets_workspace_id', 'workspace_datasets', ['workspace_id'])
    op.create_index('ix_workspace_datasets_org_id', 'workspace_datasets', ['organization_id'])


def downgrade():
    op.drop_index('ix_workspace_datasets_org_id', table_name='workspace_datasets')
    op.drop_index('ix_workspace_datasets_workspace_id', table_name='workspace_datasets')
    op.drop_table('workspace_datasets')
    op.drop_index('ix_workspaces_org_id', table_name='workspaces')
    op.drop_table('workspaces')
