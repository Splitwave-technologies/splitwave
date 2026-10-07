"""users, user_sessions, project_members, login_attempts; api_tokens.project_id

Revision ID: c9e1a5f27d40
Revises: b7d2e4a91c33
Create Date: 2026-09-30 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9e1a5f27d40'
down_revision: Union[str, None] = 'b7d2e4a91c33'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('users',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('username', sa.String(), nullable=False),
        sa.Column('password_hash', sa.String(), nullable=False),
        sa.Column('role', sa.String(), nullable=False),
        sa.Column('disabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('must_change_password', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('totp_secret', sa.LargeBinary(), nullable=True),
        sa.Column('totp_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('totp_last_counter', sa.BigInteger(), nullable=True),
        sa.Column('recovery_codes', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('username'))
    op.create_table('user_sessions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('token_hash', sa.String(), nullable=False),
        sa.Column('csrf_token', sa.String(), nullable=False),
        sa.Column('restricted', sa.String(), nullable=True),
        sa.Column('ip', sa.String(), nullable=True),
        sa.Column('user_agent', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'))
    op.create_index(op.f('ix_user_sessions_token_hash'), 'user_sessions', ['token_hash'], unique=True)
    op.create_table('project_members',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('role', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('user_id', 'project_id', name='uq_member_user_project'))
    op.create_table('login_attempts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('success', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'))
    op.create_index(op.f('ix_login_attempts_key'), 'login_attempts', ['key'], unique=False)
    op.create_index(op.f('ix_login_attempts_created_at'), 'login_attempts', ['created_at'], unique=False)
    op.add_column('api_tokens', sa.Column('project_id', sa.UUID(), nullable=True))
    op.create_foreign_key('fk_api_tokens_project', 'api_tokens', 'projects', ['project_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('fk_api_tokens_project', 'api_tokens', type_='foreignkey')
    op.drop_column('api_tokens', 'project_id')
    op.drop_table('login_attempts')
    op.drop_table('project_members')
    op.drop_table('user_sessions')
    op.drop_table('users')
