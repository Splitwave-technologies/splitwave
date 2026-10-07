"""approvals: заявки на согласование выката и отката; environments.require_approval

Revision ID: d3a8b6c14e92
Revises: c9e1a5f27d40
Create Date: 2026-09-30 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd3a8b6c14e92'
down_revision: Union[str, None] = 'c9e1a5f27d40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('environments', sa.Column('require_approval', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table('approvals',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('environment_id', sa.UUID(), nullable=False),
        sa.Column('action', sa.String(), nullable=False),
        sa.Column('params', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('requested_by', sa.String(), nullable=False),
        sa.Column('requester_id', sa.UUID(), nullable=True),
        sa.Column('requested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('decided_by', sa.String(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('note', sa.String(), nullable=True),
        sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['environment_id'], ['environments.id']),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.PrimaryKeyConstraint('id'))


def downgrade() -> None:
    op.drop_table('approvals')
    op.drop_column('environments', 'require_approval')
