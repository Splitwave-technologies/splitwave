"""environments.app_port: порт приложения (для мастера подключения: Service, Ingress, повторная выдача манифеста)

Revision ID: c6e8a0b2d4f1
Revises: b5d7f9a1c3e4
Create Date: 2026-10-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c6e8a0b2d4f1'
down_revision: Union[str, None] = 'b5d7f9a1c3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('environments', sa.Column('app_port', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('environments', 'app_port')
