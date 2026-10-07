"""Проверка обновлений: раз в сутки читает публичный файл с последним выпуском (https://split-wave.com/releases.json).
Ничего не отправляет о вас: обычный GET без ключа, адреса и идентификаторов установки. Выключается UPDATE_CHECK=false."""
import json
import logging
import re
import threading
import time
import urllib.request
from typing import Optional

from app.config import settings
from app.version import VERSION

logger = logging.getLogger(__name__)
TTL = 24 * 3600
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": None}


def _parse(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v.split("-")[0])[:3])


def is_newer(latest: str, current: str) -> bool:
    try:
        return _parse(latest) > _parse(current)
    except ValueError:
        return False


def _fetch() -> Optional[dict]:
    url = settings.update_url
    if not url.startswith("https://"):
        return None
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "splitwave-update-check"})
        with urllib.request.urlopen(req, timeout=5) as r:                            # noqa: S310 (схема проверена выше)
            data = json.loads(r.read(65536))
        latest = str(data["version"])
        if not _parse(latest):
            raise ValueError("bad version")
        return {"version": latest, "url": str(data.get("url", "")), "security": bool(data.get("security", False))}
    except Exception as exc:                                                         # сеть недоступна, файл не тот — тихо молчим
        logger.info("update check failed: %s", exc)
        return None


def status(now: Optional[float] = None) -> dict:
    """{enabled, current, latest, available, security, url}. Не бросает исключений; не чаще одного запроса в сутки."""
    out = {"enabled": settings.update_check, "current": VERSION, "latest": None, "available": False, "security": False, "url": ""}
    if not settings.update_check:
        return out
    now = time.time() if now is None else now
    with _lock:
        if now - _cache["at"] > TTL:
            _cache.update(at=now, data=_fetch())
        data = _cache["data"]
    if data:
        out.update(latest=data["version"], url=data["url"], security=data["security"], available=is_newer(data["version"], VERSION))
    return out


def reset() -> None:
    _cache.update(at=0.0, data=None)
