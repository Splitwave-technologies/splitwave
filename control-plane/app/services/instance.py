"""Идентификатор установки платформы: создаётся при первом обращении, хранится в базе и не меняется при перезапусках.
Лицензионный ключ можно привязать к нему (поле install_id): тогда один ключ нельзя раздать нескольким компаниям.
Идентификатор не секрет (он нужен клиенту, чтобы запросить ключ), но и ничего не раскрывает о кластере."""
import threading
import uuid
from typing import Optional

from app.db.base import SessionLocal
from app.services import plugin_state

_KEY = "instance"
_lock = threading.Lock()
_cached: Optional[str] = None


def get_id() -> str:
    global _cached
    if _cached:
        return _cached
    with _lock:
        if _cached:
            return _cached
        db = SessionLocal()
        try:
            state = plugin_state.get(db, _KEY)
            if not state or not state.get("id"):
                try:
                    plugin_state.put(db, _KEY, {"id": f"dsp-{uuid.uuid4().hex[:20]}"})
                except Exception:           # второй процесс создал запись одновременно: берём его значение
                    db.rollback()
                state = plugin_state.get(db, _KEY)
            _cached = state["id"]
        finally:
            db.close()
    return _cached


def reset_cache() -> None:           # для тестов: после пересоздания базы
    global _cached
    _cached = None
