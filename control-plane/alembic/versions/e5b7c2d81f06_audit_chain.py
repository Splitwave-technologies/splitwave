"""audit_events: цепочка целостности (seq, prev_hash, hash) и context

Revision ID: e5b7c2d81f06
Revises: d3a8b6c14e92
Create Date: 2026-09-30 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlalchemy.orm


revision: str = 'e5b7c2d81f06'
down_revision: Union[str, None] = 'd3a8b6c14e92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('audit_events', sa.Column('seq', sa.BigInteger(), nullable=True))
    op.add_column('audit_events', sa.Column('prev_hash', sa.String(), nullable=True))
    op.add_column('audit_events', sa.Column('hash', sa.String(), nullable=True))
    op.add_column('audit_events', sa.Column('context', sa.JSON(), nullable=True))
    op.create_unique_constraint('uq_audit_events_seq', 'audit_events', ['seq'])

    # Уже накопленные события запечатываем по порядку времени (с этого момента они защищены от правок).
    from app.db.base import Base
    from app.db.models import AuditEvent
    from app.services import audit
    session = sa.orm.Session(bind=op.get_bind())
    last = None
    for ev in session.query(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.id).yield_per(500):
        audit.seal(ev, last)
        last = ev
    session.flush()


def downgrade() -> None:
    op.drop_constraint('uq_audit_events_seq', 'audit_events', type_='unique')
    for c in ('context', 'hash', 'prev_hash', 'seq'):
        op.drop_column('audit_events', c)
