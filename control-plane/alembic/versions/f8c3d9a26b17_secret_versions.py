"""secret_versions: история значений секретов, срок действия и период ротации

Revision ID: f8c3d9a26b17
Revises: e5b7c2d81f06
Create Date: 2026-09-30 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f8c3d9a26b17'
down_revision: Union[str, None] = 'e5b7c2d81f06'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('secrets', sa.Column('version', sa.Integer(), nullable=False, server_default='1'))
    op.add_column('secrets', sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('secrets', sa.Column('rotation_days', sa.Integer(), nullable=True))
    op.add_column('secrets', sa.Column('updated_by', sa.String(), nullable=True))
    op.create_table('secret_versions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('scope', sa.String(), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('encrypted_value', sa.LargeBinary(), nullable=False),
        sa.Column('created_by', sa.String(), nullable=True),
        sa.Column('reason', sa.String(), nullable=False, server_default='set'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', 'scope', 'key', 'version', name='uq_secret_version'),
    )
    op.create_index('ix_secret_versions_project_id', 'secret_versions', ['project_id'])
    # Текущие значения становятся версией 1.
    op.execute("INSERT INTO secret_versions (id, project_id, scope, key, version, encrypted_value, created_by, reason, created_at) "
               "SELECT gen_random_uuid(), project_id, scope, key, 1, encrypted_value, 'migration', 'set', COALESCE(updated_at, now()) FROM secrets")


def downgrade() -> None:
    op.drop_index('ix_secret_versions_project_id', table_name='secret_versions')
    op.drop_table('secret_versions')
    for c in ('updated_by', 'rotation_days', 'expires_at', 'version'):
        op.drop_column('secrets', c)
