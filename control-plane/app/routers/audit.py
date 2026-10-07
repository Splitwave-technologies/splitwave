from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.auth import Principal, require
from app.db.base import SessionLocal
from app.db.models import AuditEvent, Project
from app.services import audit as audit_svc

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/verify")
async def verify(principal: Principal = Depends(require("audit:read"))):
    """Проверка целостности всей цепочки аудита."""
    db = SessionLocal()
    try:
        return audit_svc.verify_chain(db)
    finally:
        db.close()


@router.get("")
async def list_events(project: Optional[str] = None, action: Optional[str] = None,
                      limit: int = Query(default=100, ge=1, le=500),
                      principal: Principal = Depends(require("audit:read"))):
    db = SessionLocal()
    try:
        q = db.query(AuditEvent)
        if project:
            p = db.query(Project).filter_by(slug=project).first()
            q = q.filter(AuditEvent.project_id == (p.id if p else None))
        if action:
            q = q.filter(AuditEvent.action == action)
        return [{"id": str(e.id), "actor": e.actor, "action": e.action, "detail": e.detail,
                 "context": e.context or {}, "seq": e.seq, "hash": e.hash,
                 "project_id": str(e.project_id) if e.project_id else None,
                 "created_at": e.created_at.isoformat() if e.created_at else None}
                for e in q.order_by(AuditEvent.created_at.desc()).limit(limit).all()]
    finally:
        db.close()
