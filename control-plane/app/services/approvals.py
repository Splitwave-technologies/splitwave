"""Согласование выката и отката: заявка -> решение другого человека -> выполнение."""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.auth import Principal
from app.config import settings
from app.db.models import Approval, Environment, Project
from app.services import audit


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def create(db: Session, project: Project, env: Environment, action: str, params: dict, requested_by: str, requester_id=None) -> Approval:
    a = Approval(project_id=project.id, environment_id=env.id, action=action, params=params, requested_by=requested_by,
                 requester_id=requester_id, expires_at=_now() + timedelta(hours=settings.approval_ttl_hours))
    db.add(a)
    db.commit()
    audit.log_event(db, requested_by, "approval_requested", project.id, env.id,
                    {"approval": str(a.id), "action": action, "params": params})
    return a


def refresh_expiry(db: Session, a: Approval) -> Approval:
    if a.status == "pending" and _aware(a.expires_at) < _now():
        a.status = "expired"
        db.commit()
        audit.log_event(db, "system", "approval_expired", a.project_id, a.environment_id, {"approval": str(a.id)})
    return a


def check_can_decide(principal: Principal, a: Approval) -> None:
    """Решает тот, у кого есть право отката в проекте, и это не автор заявки (принцип четырёх глаз)."""
    if not principal.can("rollback", a.project_id):
        raise HTTPException(status_code=403, detail="you need the rollback permission in this project to decide approvals")
    if a.requester_id is not None and a.requester_id == principal.id:
        raise HTTPException(status_code=403, detail="you cannot decide your own request (four-eyes principle)")


def view(a: Approval) -> dict:
    return {"id": str(a.id), "project": a.project.slug if a.project else None,
            "environment": a.environment.name if a.environment else None, "action": a.action, "params": a.params,
            "status": a.status, "requested_by": a.requested_by,
            "requested_at": a.requested_at.isoformat() if a.requested_at else None,
            "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            "decided_by": a.decided_by, "decided_at": a.decided_at.isoformat() if a.decided_at else None,
            "note": a.note, "executed_at": a.executed_at.isoformat() if a.executed_at else None, "error": a.error}
