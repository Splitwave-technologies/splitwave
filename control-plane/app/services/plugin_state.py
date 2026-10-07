"""Хранилище состояния платных модулей (ключ → JSON). Изменения полей идут через merge: чтение и запись в одной транзакции."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import PluginState


def get(db: Session, key: str) -> Optional[dict]:
    row = db.get(PluginState, key)
    return dict(row.value) if row else None


def put(db: Session, key: str, value: dict) -> None:
    row = db.get(PluginState, key)
    if row:
        row.value = dict(value)
        row.updated_at = datetime.now(timezone.utc)
    else:
        db.add(PluginState(key=key, value=dict(value)))
    db.commit()


def merge(db: Session, key: str, fields: dict) -> Optional[dict]:
    """Обновляет отдельные поля документа, не затирая чужие изменения других полей."""
    row = db.query(PluginState).filter_by(key=key).with_for_update().first()
    if not row:
        db.rollback()
        return None
    row.value = {**row.value, **fields}
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return dict(row.value)


def list_prefix(db: Session, prefix: str) -> dict[str, dict]:
    rows = db.query(PluginState).filter(PluginState.key.like(prefix.replace("%", r"\%") + "%", escape="\\")).order_by(PluginState.key).all()
    return {r.key: dict(r.value) for r in rows}


def delete(db: Session, key: str) -> bool:
    n = db.query(PluginState).filter_by(key=key).delete()
    db.commit()
    return bool(n)
