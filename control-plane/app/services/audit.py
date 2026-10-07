"""Журнал аудита с цепочкой целостности.

Каждое событие получает сквозной номер seq и хеш HMAC-SHA256(prev_hash + содержимое). Ключ HMAC выводится из
ключа шифрования платформы, поэтому подправить запись и пересчитать цепочку может только тот, у кого есть
ключ, а не любой, кто получил доступ к базе. Изменение, удаление или вставка события в середине ломает
цепочку и находится проверкой `verify_chain`. Обрезку хвоста можно поймать, только сверяя `head` с внешней
копией (его показывает /api/audit/verify), поэтому head стоит периодически выгружать во внешнее место.
Поля project_id/environment_id и `context` в хеш не входят: они меняются легально (удаление проекта).
"""
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import AuditEvent

logger = logging.getLogger(__name__)

GENESIS = "0" * 64


def _key() -> bytes:
    first = next((k.strip() for k in settings.secret_encryption_key.split(",") if k.strip()), "")
    if not first:
        raise RuntimeError("secret_encryption_key is not configured")
    return hashlib.sha256(b"audit-chain|" + first.encode()).digest()


def _ts(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


def compute_hash(seq: int, actor: str, action: str, detail: Optional[dict], created_at: datetime, prev_hash: str) -> str:
    payload = json.dumps({"seq": seq, "actor": actor, "action": action, "detail": detail or {},
                          "ts": _ts(created_at), "prev": prev_hash},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()


def seal(event: AuditEvent, last: Optional[AuditEvent]) -> None:
    event.seq = (last.seq + 1) if last else 1
    event.prev_hash = last.hash if last else GENESIS
    event.hash = compute_hash(event.seq, event.actor, event.action, event.detail, event.created_at, event.prev_hash)


def _last(db: Session) -> Optional[AuditEvent]:
    return db.query(AuditEvent).filter(AuditEvent.seq.isnot(None)).order_by(AuditEvent.seq.desc()).first()


def log_event(
    db: Session,
    actor: str,
    action: str,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    detail: Optional[dict] = None,
) -> None:
    # seq уникален: при гонке двух писателей проигравший получает IntegrityError и пробует снова с новым хвостом.
    for attempt in range(8):
        event = AuditEvent(project_id=project_id, environment_id=environment_id, actor=actor, action=action,
                           detail=detail or {}, created_at=datetime.now(timezone.utc))
        seal(event, _last(db))
        db.add(event)
        try:
            db.commit()
            break
        except IntegrityError:
            db.rollback()
            if attempt == 7:
                raise
    logger.info("audit: actor=%s action=%s project=%s env=%s detail=%s",
                actor, action, project_id, environment_id, detail)
    from app.services import notifications   # поздний импорт: notifications зависит от моделей и БД
    notifications.dispatch(action, actor, project_id, environment_id, detail or {}, event.created_at)


def _iter_chain(db: Session) -> Iterator[AuditEvent]:
    return iter(db.query(AuditEvent).filter(AuditEvent.seq.isnot(None)).order_by(AuditEvent.seq).yield_per(500))


def verify_chain(db: Session) -> dict:
    """Проходит всю цепочку. Возвращает число событий, head и первое найденное нарушение."""
    count, expected_seq, prev = 0, 1, GENESIS
    problem = None
    for e in _iter_chain(db):
        if e.seq != expected_seq:
            problem = {"seq": expected_seq, "reason": "missing_event"}
            break
        if e.prev_hash != prev:
            problem = {"seq": e.seq, "reason": "broken_link"}
            break
        if not hmac.compare_digest(e.hash or "", compute_hash(e.seq, e.actor, e.action, e.detail, e.created_at, e.prev_hash)):
            problem = {"seq": e.seq, "reason": "content_modified"}
            break
        prev, expected_seq, count = e.hash, e.seq + 1, count + 1
    unchained = db.query(AuditEvent).filter(AuditEvent.seq.is_(None)).count()
    return {"ok": problem is None, "events": count, "head": prev if count else None,
            "head_seq": expected_seq - 1 if count else 0, "first_problem": problem, "unchained": unchained}
