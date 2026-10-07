"""notification_channels: каналы уведомлений (webhook, Slack, Telegram)

Revision ID: a2d4f6b8c0e1
Revises: f8c3d9a26b17
Create Date: 2026-09-30 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a2d4f6b8c0e1'
down_revision: Union[str, None] = 'f8c3d9a26b17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('notification_channels',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('config_encrypted', sa.LargeBinary(), nullable=False),
        sa.Column('target_hint', sa.String(), nullable=False, server_default=''),
        sa.Column('events', sa.JSON(), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_by', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('last_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_status', sa.String(), nullable=True),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )


def downgrade() -> None:
    op.drop_table('notification_channels')
