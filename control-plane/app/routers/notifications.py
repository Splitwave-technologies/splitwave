import secrets as pysecrets
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import Principal, require
from app.db.base import SessionLocal
from app.db.models import NotificationChannel
from app.services import audit, checks, notifications as svc, project_repo

router = APIRouter(prefix="/api/notifications", tags=["notifications"])
PERM = "notifications:manage"


class ChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kind: str
    url: Optional[str] = None            # webhook, slack
    bot_token: Optional[str] = None      # telegram
    chat_id: Optional[str] = None        # telegram
    events: Optional[list[str]] = None   # по умолчанию — рекомендуемый набор; ["*"] — все события
    project: Optional[str] = None        # slug: только события этого проекта
    enabled: bool = True


class ChannelPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    events: Optional[list[str]] = None
    enabled: Optional[bool] = None


def _view(ch: NotificationChannel, slug: Optional[str]) -> dict:
    return {"id": str(ch.id), "name": ch.name, "kind": ch.kind, "target": ch.target_hint, "events": ch.events,
            "project": slug, "enabled": ch.enabled, "created_by": ch.created_by,
            "last_sent_at": ch.last_sent_at.isoformat() if ch.last_sent_at else None,
            "last_status": ch.last_status, "last_error": ch.last_error}


def _check_events(events: list[str]) -> list[str]:
    if "*" in events:
        return ["*"]
    unknown = [e for e in events if e not in svc.CATALOG]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown events: {', '.join(unknown)}")
    return list(dict.fromkeys(events))


def _get(db, channel_id: str) -> NotificationChannel:
    try:
        ch = db.get(NotificationChannel, uuid.UUID(channel_id))
    except ValueError:  # не UUID
        ch = None
    if not ch:
        raise HTTPException(status_code=404, detail="unknown channel")
    return ch


@router.get("/events")
async def events(principal: Principal = Depends(require(PERM))):
    return [{"action": k, "title_ru": v[0], "title_en": v[1], "default": v[2]} for k, v in svc.CATALOG.items()]


@router.get("/channels")
async def list_channels(principal: Principal = Depends(require(PERM))):
    db = SessionLocal()
    try:
        from app.db.models import Project
        slugs = {p.id: p.slug for p in db.query(Project).all()}
        return [_view(c, slugs.get(c.project_id)) for c in db.query(NotificationChannel).order_by(NotificationChannel.name).all()]
    finally:
        db.close()


@router.post("/channels", status_code=201)
async def create_channel(body: ChannelIn, principal: Principal = Depends(require(PERM))):
    if body.kind not in svc.KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {', '.join(svc.KINDS)}")
    db = SessionLocal()
    try:
        if db.query(NotificationChannel).filter_by(name=body.name).first():
            raise HTTPException(status_code=409, detail="channel name already exists")
        project = None
        if body.project:
            project = project_repo.get_project_by_slug(db, body.project)
            if not project:
                raise HTTPException(status_code=404, detail=f"unknown project {body.project}")
        cfg = {"url": body.url, "bot_token": body.bot_token, "chat_id": body.chat_id}
        signing_secret = None
        if body.kind == "webhook":
            signing_secret = pysecrets.token_urlsafe(32)
            cfg["secret"] = signing_secret
        try:
            clean, hint = svc.validate_config(body.kind, cfg)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        ch = NotificationChannel(name=body.name, kind=body.kind, config_encrypted=svc.encode_config(clean), target_hint=hint,
                                 events=_check_events(body.events if body.events is not None else svc.DEFAULT_EVENTS),
                                 project_id=project.id if project else None, enabled=body.enabled, created_by=principal.name)
        db.add(ch)
        db.commit()
        audit.log_event(db, principal.name, "notification_channel_create", project.id if project else None,
                        detail={"name": body.name, "kind": body.kind, "target": hint})
        out = _view(ch, body.project)
        if signing_secret:
            out["signing_secret"] = signing_secret     # показывается один раз
        return out
    finally:
        db.close()


@router.patch("/channels/{channel_id}")
async def update_channel(channel_id: str, body: ChannelPatch, principal: Principal = Depends(require(PERM))):
    db = SessionLocal()
    try:
        ch = _get(db, channel_id)
        changes = {}
        if body.name is not None and body.name != ch.name:
            if db.query(NotificationChannel).filter_by(name=body.name).first():
                raise HTTPException(status_code=409, detail="channel name already exists")
            ch.name = body.name; changes["name"] = body.name
        if body.events is not None:
            ch.events = _check_events(body.events); changes["events"] = ch.events
        if body.enabled is not None:
            ch.enabled = body.enabled; changes["enabled"] = body.enabled
        db.commit()
        audit.log_event(db, principal.name, "notification_channel_update", ch.project_id, detail={"name": ch.name, **changes})
        return _view(ch, None)
    finally:
        db.close()


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str, principal: Principal = Depends(require(PERM))):
    db = SessionLocal()
    try:
        ch = _get(db, channel_id)
        name, pid = ch.name, ch.project_id
        db.delete(ch)
        db.commit()
        audit.log_event(db, principal.name, "notification_channel_delete", pid, detail={"name": name})
        return {"deleted": name}
    finally:
        db.close()


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: str, principal: Principal = Depends(require(PERM))):
    """Отправляет тестовое сообщение сразу (в этом же запросе) и возвращает результат доставки."""
    db = SessionLocal()
    try:
        ch = _get(db, channel_id)
        msg = svc.build_message("approval_requested", principal.name, None, None, {"test": True}, datetime.now().astimezone())
        msg["text"] = ("[SplitWave] " + ("Проверка канала уведомлений" if svc.settings.notify_lang == "ru" else "Notification channel test")
                       + f"\n{ch.name} ← {principal.name}")
        msg["event"] = "test"
        cid, kind, cfg = str(ch.id), ch.kind, svc.decode_config(ch)
    finally:
        db.close()
    ok, error = svc.deliver(cid, kind, cfg, msg)
    return {"ok": ok, "error": error}


@router.post("/check")
async def run_checks(principal: Principal = Depends(require(PERM))):
    """Запускает периодические проверки (сроки секретов, целостность аудита) немедленно."""
    db = SessionLocal()
    try:
        return checks.run_once(db)
    finally:
        db.close()
