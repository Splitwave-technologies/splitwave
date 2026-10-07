"""environments.preview_of: временные среды под pull request (платная функция preview_envs)

Revision ID: e8b0d2f4a6c7
Revises: d7a9c1e3f5b6
Create Date: 2026-10-01 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e8b0d2f4a6c7'
down_revision: Union[str, None] = 'd7a9c1e3f5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('environments', sa.Column('preview_of', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('environments', 'preview_of')
