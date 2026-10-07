"""plugin_state: хранилище состояния платных модулей

Revision ID: c6f8b0d2e4a5
Revises: b4e6a8c0d2f3
Create Date: 2026-10-01 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c6f8b0d2e4a5'
down_revision: Union[str, None] = 'b4e6a8c0d2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('plugin_state',
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('value', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('key'))


def downgrade() -> None:
    op.drop_table('plugin_state')
