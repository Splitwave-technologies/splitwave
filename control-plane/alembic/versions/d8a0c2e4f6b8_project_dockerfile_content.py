"""projects.dockerfile_content: Dockerfile, который платформа хранит у себя (предложен мастером и отредактирован пользователем)

Revision ID: d8a0c2e4f6b8
Revises: c6e8a0b2d4f1
Create Date: 2026-10-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd8a0c2e4f6b8'
down_revision: Union[str, None] = 'c6e8a0b2d4f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('dockerfile_content', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('projects', 'dockerfile_content')
