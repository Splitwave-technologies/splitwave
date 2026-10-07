"""servers, server_jobs, environments.server/runtime: выкат на обычные серверы через агента

Revision ID: b5d7f9a1c3e4
Revises: a3c5e7b9d1f2
Create Date: 2026-10-02 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b5d7f9a1c3e4'
down_revision: Union[str, None] = 'a3c5e7b9d1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('environments', sa.Column('server', sa.String(), nullable=True))
    op.add_column('environments', sa.Column('runtime', sa.JSON(), nullable=True))
    op.create_table('servers',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('enroll_hash', sa.String(), nullable=True),
        sa.Column('enroll_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('agent_hash', sa.String(), nullable=True),
        sa.Column('registry_auth_enc', sa.LargeBinary(), nullable=True),
        sa.Column('info', sa.JSON(), nullable=True),
        sa.Column('agent_version', sa.String(), nullable=True),
        sa.Column('enrolled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('name'))
    op.create_index('ix_servers_enroll_hash', 'servers', ['enroll_hash'])
    op.create_index('ix_servers_agent_hash', 'servers', ['agent_hash'])
    op.create_table('server_jobs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('server_id', sa.UUID(), nullable=False),
        sa.Column('environment_id', sa.UUID(), nullable=True),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('payload_enc', sa.LargeBinary(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='pending'),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('requested_by', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id']),
        sa.PrimaryKeyConstraint('id'))
    op.create_index('ix_server_jobs_server_id', 'server_jobs', ['server_id'])


def downgrade() -> None:
    op.drop_index('ix_server_jobs_server_id', table_name='server_jobs')
    op.drop_table('server_jobs')
    op.drop_index('ix_servers_agent_hash', table_name='servers')
    op.drop_index('ix_servers_enroll_hash', table_name='servers')
    op.drop_table('servers')
    op.drop_column('environments', 'runtime')
    op.drop_column('environments', 'server')
