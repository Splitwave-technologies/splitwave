"""environments.cluster: удалённый кластер среды (платная функция multi_cluster)

Revision ID: d7a9c1e3f5b6
Revises: c6f8b0d2e4a5
Create Date: 2026-10-01 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd7a9c1e3f5b6'
down_revision: Union[str, None] = 'c6f8b0d2e4a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('environments', sa.Column('cluster', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('environments', 'cluster')
