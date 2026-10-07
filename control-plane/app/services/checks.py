"""Периодические проверки: сроки секретов и целостность журнала аудита.

Результат пишется как события аудита (secret_expiring, secret_expired, secret_rotation_due, audit_chain_broken),
а уведомления рассылаются обычным путём. Повторы подавляются: одно событие на секрет и состояние в неделю."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.db.base import SessionLocal
from app.db.models import AuditEvent, Project, Secret
from app.services import audit
from app.services import secrets as secrets_svc

logger = logging.getLogger(__name__)

STATUS_ACTION = {"expiring": "secret_expiring", "expired": "secret_expired", "rotation_due": "secret_rotation_due"}
REPEAT_AFTER = timedelta(days=7)


def _seen(db: Session, action: str, project_id, key: str, scope: str, since: datetime) -> bool:
    q = db.query(AuditEvent).filter(AuditEvent.action == action, AuditEvent.project_id == project_id,
                                    AuditEvent.created_at >= since)
    return any((e.detail or {}).get("key") == key and (e.detail or {}).get("scope") == scope for e in q.all())


def run_once(db: Session) -> dict:
    now = datetime.now(timezone.utc)
    raised = 0
    slugs = {p.id: p.slug for p in db.query(Project).all()}
    for sec in db.query(Secret).all():
        status = secrets_svc.secret_status(sec, now)
        action = STATUS_ACTION.get(status)
        if not action or _seen(db, action, sec.project_id, sec.key, sec.scope, now - REPEAT_AFTER):
            continue
        exp = secrets_svc._utc(sec.expires_at)
        audit.log_event(db, "system", action, sec.project_id, None,
                        {"key": sec.key, "scope": sec.scope, "project_slug": slugs.get(sec.project_id),
                         "expires_at": exp.isoformat() if exp else None})
        raised += 1

    chain = audit.verify_chain(db)
    if not chain["ok"]:
        problem = chain["first_problem"]
        last = db.query(AuditEvent).filter(AuditEvent.action == "audit_chain_broken").order_by(AuditEvent.created_at.desc()).first()
        if not last or (last.detail or {}).get("seq") != problem["seq"] or (last.detail or {}).get("reason") != problem["reason"]:
            audit.log_event(db, "system", "audit_chain_broken", None, None, dict(problem))
            raised += 1
    return {"raised": raised, "chain_ok": chain["ok"]}


def _run_in_thread() -> None:
    db = SessionLocal()
    try:
        run_once(db)
    except Exception:  # noqa: BLE001
        logger.exception("periodic checks failed")
    finally:
        db.close()


async def periodic() -> None:
    interval = settings.checks_interval_minutes
    if interval <= 0:
        return
    await asyncio.sleep(30)          # дать приложению подняться
    while True:
        await asyncio.to_thread(_run_in_thread)
        await asyncio.sleep(interval * 60)
