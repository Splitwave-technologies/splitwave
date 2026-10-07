"""users: auth_source и external_id (вход через внешнего провайдера SSO)

Revision ID: b4e6a8c0d2f3
Revises: a2d4f6b8c0e1
Create Date: 2026-10-01 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b4e6a8c0d2f3'
down_revision: Union[str, None] = 'a2d4f6b8c0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('auth_source', sa.String(), nullable=False, server_default='local'))
    op.add_column('users', sa.Column('external_id', sa.String(), nullable=True))
    op.create_unique_constraint('uq_users_external', 'users', ['auth_source', 'external_id'])


def downgrade() -> None:
    op.drop_constraint('uq_users_external', 'users', type_='unique')
    op.drop_column('users', 'external_id')
    op.drop_column('users', 'auth_source')
