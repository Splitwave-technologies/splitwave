"""projects: build_method (buildpacks | dockerfile) и dockerfile_path

Revision ID: a3c5e7b9d1f2
Revises: f1a3c5e7b9d0
Create Date: 2026-10-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3c5e7b9d1f2'
down_revision: Union[str, None] = 'f1a3c5e7b9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('build_method', sa.String(), nullable=False, server_default='buildpacks'))
    op.add_column('projects', sa.Column('dockerfile_path', sa.String(), nullable=False, server_default='Dockerfile'))


def downgrade() -> None:
    op.drop_column('projects', 'dockerfile_path')
    op.drop_column('projects', 'build_method')
