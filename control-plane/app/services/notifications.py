"""Уведомления: события аудита уходят в webhook, Slack-совместимый вебхук или Telegram.

Отправка идёт в фоне и никогда не блокирует и не ломает основное действие. Адреса и токены каналов
хранятся зашифрованно (как секреты) и наружу не отдаются. Исходящие адреса проверяются на SSRF:
по умолчанию запрещены loopback, частные, link-local сети и http."""
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.db.base import SessionLocal
from app.db.models import Environment, NotificationChannel, Project
from app.services import crypto, safeurl

logger = logging.getLogger(__name__)

KINDS = ("webhook", "slack", "telegram")

# Каталог событий: действие аудита -> (заголовок ru, заголовок en, включено по умолчанию)
CATALOG: dict[str, tuple[str, str, bool]] = {
    "approval_requested": ("Запрошено согласование", "Approval requested", True),
    "approval_approved": ("Заявка согласована", "Approval granted", True),
    "approval_denied": ("Заявка отклонена", "Approval denied", True),
    "approval_executed": ("Согласованное действие выполнено", "Approved action executed", False),
    "approval_failed": ("Согласованное действие не выполнено", "Approved action failed", True),
    "approval_expired": ("Заявка истекла", "Approval expired", False),
    "deploy": ("Запущен выкат", "Deploy started", False),
    "deploy_failed": ("Выкат не удался", "Deploy failed", True),
    "rollback": ("Выполнен откат", "Rollback executed", True),
    "break_glass": ("Аварийный обход согласования", "Break-glass used", True),
    "access_denied": ("Отказано в доступе", "Access denied", False),
    "secret_set": ("Секрет изменён", "Secret changed", False),
    "secret_restore": ("Секрет возвращён к версии", "Secret restored", False),
    "secret_delete": ("Секрет удалён", "Secret deleted", False),
    "secret_expiring": ("Секрет скоро истечёт", "Secret expiring soon", True),
    "secret_expired": ("Срок секрета истёк", "Secret expired", True),
    "secret_rotation_due": ("Пора сменить секрет", "Secret rotation due", True),
    "audit_chain_broken": ("Нарушена целостность журнала аудита", "Audit log integrity broken", True),
    "user_create": ("Создан пользователь", "User created", False),
    "user_delete": ("Пользователь удалён", "User deleted", False),
    "project_create": ("Создан проект", "Project created", False),
    "project_delete": ("Проект удалён", "Project deleted", False),
}
DEFAULT_EVENTS = [k for k, v in CATALOG.items() if v[2]]

SYNC = False                       # тесты включают синхронную отправку
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify")
_recent: dict[str, deque] = {}     # channel_id -> времена последних отправок (ограничение частоты)
_recent_lock = threading.Lock()
RATE_LIMIT = 30                    # сообщений в минуту на канал; лишние отбрасываются
RETRIES = (0, 1.0, 3.0)            # паузы перед попытками


def _resolve(host: str) -> list[str]:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None)})


def check_url(url: str) -> str:
    """Проверяет адрес исходящего запроса; возвращает хост. ValueError — адрес недопустим."""
    try:
        return safeurl.check_url(url, settings.notify_allow_http, settings.notify_allow_private_targets, _resolve)
    except ValueError as e:
        if str(e) == "target address is not public":
            raise ValueError("target address is not public (set NOTIFY_ALLOW_PRIVATE_TARGETS to allow internal hosts)")
        raise


def validate_config(kind: str, cfg: dict) -> tuple[dict, str]:
    """Проверяет настройки канала; возвращает (очищенные настройки, безопасная подпись адресата)."""
    if kind in ("webhook", "slack"):
        host = check_url(str(cfg.get("url", "")))
        out = {"url": cfg["url"]}
        if kind == "webhook" and cfg.get("secret"):
            out["secret"] = cfg["secret"]
        return out, host
    if kind == "telegram":
        token, chat = str(cfg.get("bot_token", "")), str(cfg.get("chat_id", ""))
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("invalid Telegram bot token")
        if not re.fullmatch(r"-?\d+|@[A-Za-z0-9_]{4,}", chat):
            raise ValueError("chat_id must be a number or @channelname")
        return {"bot_token": token, "chat_id": chat}, f"telegram chat {chat}"
    raise ValueError(f"unknown kind {kind}")


def encode_config(cfg: dict) -> bytes:
    return crypto.encrypt_value(json.dumps(cfg))


def decode_config(ch: NotificationChannel) -> dict:
    return json.loads(crypto.decrypt_value(ch.config_encrypted))


def _title(action: str) -> str:
    ru, en, _ = CATALOG.get(action, (action, action, False))
    return ru if settings.notify_lang == "ru" else en


def _flat(detail: dict) -> list[str]:
    lines = []
    for k, v in list((detail or {}).items())[:8]:
        text = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        lines.append(f"{k}: {text[:120]}")
    return lines


def build_message(action: str, actor: str, project: Optional[str], environment: Optional[str], detail: dict,
                  when: datetime) -> dict:
    head = f"[SplitWave] {_title(action)}"
    where = " / ".join(x for x in (project, environment) if x)
    lines = [head] + ([where] if where else []) + [f"{'Кто' if settings.notify_lang == 'ru' else 'By'}: {actor}"] + _flat(detail)
    return {"event": action, "actor": actor, "project": project, "environment": environment,
            "time": when.astimezone(timezone.utc).isoformat(), "detail": detail or {}, "text": "\n".join(lines)}


def _post(url: str, body: bytes, headers: dict) -> int:
    r = httpx.post(url, content=body, headers=headers, timeout=settings.notify_timeout_seconds, follow_redirects=False)
    return r.status_code


def _send(kind: str, cfg: dict, msg: dict) -> None:
    """Одна попытка отправки; исключение — ошибка доставки."""
    if kind == "telegram":
        url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
        body = json.dumps({"chat_id": cfg["chat_id"], "text": msg["text"], "disable_web_page_preview": True}).encode()
        headers = {"Content-Type": "application/json"}
    else:
        check_url(cfg["url"])
        url = cfg["url"]
        headers = {"Content-Type": "application/json", "User-Agent": "splitwave"}
        if kind == "slack":
            body = json.dumps({"text": msg["text"]}).encode()
        else:
            body = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode()
            if cfg.get("secret"):
                headers["X-DSP-Signature"] = "sha256=" + hmac.new(cfg["secret"].encode(), body, hashlib.sha256).hexdigest()
    status = _post(url, body, headers)
    if not 200 <= status < 300:
        raise RuntimeError(f"HTTP {status}")


def _allowed(channel_id: str) -> bool:
    now = time.monotonic()
    with _recent_lock:
        q = _recent.setdefault(channel_id, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return False
        q.append(now)
        return True


def _record(channel_id, ok: bool, error: Optional[str]) -> None:
    db = SessionLocal()
    try:
        ch = db.get(NotificationChannel, uuid.UUID(str(channel_id)))
        if ch:
            ch.last_sent_at = datetime.now(timezone.utc)
            ch.last_status = "ok" if ok else "error"
            ch.last_error = None if ok else (error or "")[:300]
            db.commit()
    except Exception:  # noqa: BLE001 — статус канала не должен ломать доставку остальным
        logger.exception("cannot record notification status")
    finally:
        db.close()


def deliver(channel_id, kind: str, cfg: dict, msg: dict) -> tuple[bool, Optional[str]]:
    error = None
    for pause in RETRIES:
        if pause and not SYNC:
            time.sleep(pause)
        try:
            _send(kind, cfg, msg)
            _record(channel_id, True, None)
            return True, None
        except Exception as e:  # noqa: BLE001 — любая ошибка доставки только записывается
            error = f"{type(e).__name__}: {e}"
            if SYNC:
                break
    logger.warning("notification channel %s failed: %s", channel_id, error)
    _record(channel_id, False, error)
    return False, error


def _submit(fn, *args) -> None:
    if SYNC:
        fn(*args)
    else:
        _executor.submit(fn, *args)


def dispatch(action: str, actor: str, project_id, environment_id, detail: Optional[dict], when: datetime) -> None:
    """Вызывается после записи события аудита. Ошибки не пробрасываются."""
    try:
        db = SessionLocal()
        try:
            chans = [c for c in db.query(NotificationChannel).filter_by(enabled=True).all()
                     if (c.project_id is None or c.project_id == project_id) and ("*" in c.events or action in c.events)]
            if not chans:
                return
            project = db.get(Project, project_id) if project_id else None
            env = db.get(Environment, environment_id) if environment_id else None
            slug = project.slug if project else (detail or {}).get("slug") or (detail or {}).get("project_slug")
            msg = build_message(action, actor, slug, env.name if env else None, detail or {}, when)
            jobs = [(str(c.id), c.kind, decode_config(c)) for c in chans]
        finally:
            db.close()
        for cid, kind, cfg in jobs:
            if _allowed(cid):
                _submit(deliver, cid, kind, cfg, msg)
            else:
                logger.warning("notification channel %s: rate limit, message dropped", cid)
    except Exception:  # noqa: BLE001
        logger.exception("notification dispatch failed")
