"""projects: provider, git_url, git_username, git_token_enc; уникальность (provider, repo_full_name)

Revision ID: f1a3c5e7b9d0
Revises: e8b0d2f4a6c7
Create Date: 2026-10-02 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1a3c5e7b9d0'
down_revision: Union[str, None] = 'e8b0d2f4a6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('provider', sa.String(), nullable=False, server_default='github'))
    op.add_column('projects', sa.Column('git_url', sa.String(), nullable=True))
    op.add_column('projects', sa.Column('git_username', sa.String(), nullable=True))
    op.add_column('projects', sa.Column('git_token_enc', sa.LargeBinary(), nullable=True))
    op.drop_constraint('projects_repo_full_name_key', 'projects', type_='unique')
    op.create_unique_constraint('uq_project_provider_repo', 'projects', ['provider', 'repo_full_name'])


def downgrade() -> None:
    op.drop_constraint('uq_project_provider_repo', 'projects', type_='unique')
    op.create_unique_constraint('projects_repo_full_name_key', 'projects', ['repo_full_name'])
    for c in ('git_token_enc', 'git_username', 'git_url', 'provider'):
        op.drop_column('projects', c)
