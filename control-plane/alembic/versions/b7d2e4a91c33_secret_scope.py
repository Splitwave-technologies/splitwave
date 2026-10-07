"""secrets: область действия (все среды проекта или конкретная среда)

Revision ID: b7d2e4a91c33
Revises: a1f3c9d47b21
Create Date: 2026-09-30 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7d2e4a91c33'
down_revision: Union[str, None] = 'a1f3c9d47b21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # существующие секреты получают область "*" — поведение не меняется
    op.add_column('secrets', sa.Column('scope', sa.String(), nullable=False, server_default='*'))
    op.drop_constraint('uq_secret_project_key', 'secrets', type_='unique')
    op.create_unique_constraint('uq_secret_project_scope_key', 'secrets', ['project_id', 'scope', 'key'])


def downgrade() -> None:
    op.drop_constraint('uq_secret_project_scope_key', 'secrets', type_='unique')
    op.create_unique_constraint('uq_secret_project_key', 'secrets', ['project_id', 'key'])
    op.drop_column('secrets', 'scope')
