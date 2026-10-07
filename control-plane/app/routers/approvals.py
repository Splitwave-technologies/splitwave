from typing import Optional

from starlette.concurrency import run_in_threadpool
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import Principal, require_scoped
from app.db.base import SessionLocal
from app.db.models import Approval, Environment, Project, Release
from app.services import approvals as svc
from app.services import audit
from app.services import deployments as deps

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class Decision(BaseModel):
    note: Optional[str] = Field(default=None, max_length=300)


def _get(db, approval_id: str) -> Approval:
    import uuid
    try:
        aid = uuid.UUID(approval_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid approval id")
    a = db.get(Approval, aid)
    if not a:
        raise HTTPException(status_code=404, detail="unknown approval")
    return a


def _visible(principal: Principal, a: Approval) -> bool:
    allowed = principal.allowed_project_ids()
    return allowed is None or str(a.project_id) in allowed


@router.get("")
async def list_approvals(status: Optional[str] = None, project: Optional[str] = None,
                         limit: int = Query(default=50, ge=1, le=200), principal: Principal = Depends(require_scoped("read"))):
    db = SessionLocal()
    try:
        q = db.query(Approval).order_by(Approval.requested_at.desc())
        if project:
            p = db.query(Project).filter_by(slug=project).first()
            q = q.filter(Approval.project_id == (p.id if p else None))
        rows = [a for a in q.limit(500).all() if _visible(principal, a)]
        for a in rows:
            svc.refresh_expiry(db, a)
        if status:
            rows = [a for a in rows if a.status == status]
        # можно ли решить: показываем интерфейсу, какие кнопки рисовать (сервер всё равно проверит)
        out = []
        for a in rows[:limit]:
            v = svc.view(a)
            v["can_decide"] = (a.status == "pending" and principal.can("rollback", a.project_id)
                               and not (a.requester_id is not None and a.requester_id == principal.id))
            out.append(v)
        return out
    finally:
        db.close()


@router.get("/{approval_id}")
async def get_approval(approval_id: str, principal: Principal = Depends(require_scoped("read"))):
    db = SessionLocal()
    try:
        a = svc.refresh_expiry(db, _get(db, approval_id))
        if not _visible(principal, a):
            raise HTTPException(status_code=404, detail="unknown approval")
        return svc.view(a)
    finally:
        db.close()


def _decide_precheck(db, principal: Principal, approval_id: str) -> Approval:
    a = svc.refresh_expiry(db, _get(db, approval_id))
    if not _visible(principal, a):
        raise HTTPException(status_code=404, detail="unknown approval")
    svc.check_can_decide(principal, a)
    if a.status != "pending":
        raise HTTPException(status_code=409, detail=f"request is already {a.status}")
    return a


@router.post("/{approval_id}/approve")
async def approve(approval_id: str, body: Decision, background_tasks: BackgroundTasks,
                  principal: Principal = Depends(require_scoped("read"))):
    from datetime import datetime, timezone
    db = SessionLocal()
    try:
        a = _decide_precheck(db, principal, approval_id)
        project, env = a.project, a.environment
        a.status, a.decided_by, a.decided_at, a.note = "approved", principal.name, datetime.now(timezone.utc), body.note
        db.commit()
        audit.log_event(db, principal.name, "approval_approved", project.id, env.id,
                        {"approval": str(a.id), "requested_by": a.requested_by, "action": a.action, "note": body.note})
        via = {"approval": str(a.id), "approved_by": principal.name}
        try:
            if a.action == "redeploy":
                p = a.params
                deps.schedule_redeploy(background_tasks, db, project, env, p.get("revision") if p.get("explicit_revision") else None,
                                       a.requested_by, git_url=p.get("git_url"), via=via,
                                       label=f"{a.requested_by} (approved by {principal.name})")
            elif a.action == "rollback":
                target = db.get(Release, __import__("uuid").UUID(a.params["to"]))
                if not target or target.environment_id != env.id or not target.image_digest:
                    raise HTTPException(status_code=409, detail="target release is no longer available")
                await run_in_threadpool(deps.execute_rollback, db, project, env, target, a.requested_by, via=via)
            else:
                raise HTTPException(status_code=500, detail=f"unknown action {a.action}")
            a.status, a.executed_at = "executed", datetime.now(timezone.utc)
            db.commit()
            audit.log_event(db, "system", "approval_executed", project.id, env.id, {"approval": str(a.id), "action": a.action})
        except HTTPException as e:
            a.status, a.error = "failed", str(e.detail)
            db.commit()
            audit.log_event(db, "system", "approval_failed", project.id, env.id, {"approval": str(a.id), "error": str(e.detail)})
            raise
        except Exception as e:  # ошибка кластера при откате и т.п.
            a.status, a.error = "failed", f"{type(e).__name__}: {e}"[:500]
            db.commit()
            audit.log_event(db, "system", "approval_failed", project.id, env.id, {"approval": str(a.id), "error": a.error})
            raise HTTPException(status_code=502, detail=f"approved but execution failed: {a.error}")
        return svc.view(a)
    finally:
        db.close()


@router.post("/{approval_id}/deny")
async def deny(approval_id: str, body: Decision, principal: Principal = Depends(require_scoped("read"))):
    from datetime import datetime, timezone
    db = SessionLocal()
    try:
        a = _decide_precheck(db, principal, approval_id)
        a.status, a.decided_by, a.decided_at, a.note = "denied", principal.name, datetime.now(timezone.utc), body.note
        db.commit()
        audit.log_event(db, principal.name, "approval_denied", a.project_id, a.environment_id,
                        {"approval": str(a.id), "requested_by": a.requested_by, "action": a.action, "note": body.note})
        return svc.view(a)
    finally:
        db.close()


@router.delete("/{approval_id}")
async def cancel(approval_id: str, principal: Principal = Depends(require_scoped("read"))):
    """Автор заявки (или администратор) отзывает её, пока решение не принято."""
    db = SessionLocal()
    try:
        a = svc.refresh_expiry(db, _get(db, approval_id))
        if not _visible(principal, a):
            raise HTTPException(status_code=404, detail="unknown approval")
        if not (a.requester_id == principal.id or principal.can("users:manage")):
            raise HTTPException(status_code=403, detail="only the requester or an administrator can cancel a request")
        if a.status != "pending":
            raise HTTPException(status_code=409, detail=f"request is already {a.status}")
        a.status = "cancelled"
        db.commit()
        audit.log_event(db, principal.name, "approval_cancelled", a.project_id, a.environment_id, {"approval": str(a.id)})
        return svc.view(a)
    finally:
        db.close()
