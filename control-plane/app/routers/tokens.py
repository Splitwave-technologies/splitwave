import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import auth
from app.auth import Principal, require
from app.db.base import SessionLocal
from app.db.models import ApiToken, Project
from app.services import audit

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


class NewToken(BaseModel):
    name: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    role: str
    project: str | None = None      # slug проекта: токен будет работать только в нём (роль — не выше devops)


def _view(t: ApiToken) -> dict:
    return {"id": str(t.id), "name": t.name, "role": t.role, "prefix": t.token_prefix, "project": t.project_slug,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
            "revoked": t.revoked_at is not None}


@router.post("", status_code=201)
async def create_token(body: NewToken, principal: Principal = Depends(require("tokens:manage"))):
    """Токен показывается один раз — в БД остаётся только хеш."""
    if body.role not in auth.ROLES:
        raise HTTPException(status_code=422, detail=f"role must be one of {', '.join(auth.ROLES)}")
    db = SessionLocal()
    try:
        if db.query(ApiToken).filter_by(name=body.name).first():
            raise HTTPException(status_code=409, detail="token with this name already exists")
        project_id = None
        if body.project:
            if body.role not in auth.PROJECT_ROLES:
                raise HTTPException(status_code=422, detail=f"a project token role must be one of {', '.join(auth.PROJECT_ROLES)}")
            project = db.query(Project).filter_by(slug=body.project).first()
            if not project:
                raise HTTPException(status_code=404, detail=f"unknown project {body.project}")
            project_id = project.id
        raw = auth.generate_token()
        db.add(ApiToken(name=body.name, role=body.role, token_hash=auth.hash_token(raw), token_prefix=raw[:8], project_id=project_id))
        db.commit()
        audit.log_event(db, principal.name, "token_create", project_id, detail={"name": body.name, "role": body.role, "project": body.project})
        return {"name": body.name, "role": body.role, "project": body.project, "token": raw}
    finally:
        db.close()


@router.get("")
async def list_tokens(principal: Principal = Depends(require("tokens:manage"))):
    db = SessionLocal()
    try:
        return [_view(t) for t in db.query(ApiToken).order_by(ApiToken.created_at).all()]
    finally:
        db.close()


@router.delete("/{token_id}")
async def revoke_token(token_id: str, principal: Principal = Depends(require("tokens:manage"))):
    db = SessionLocal()
    try:
        try:
            tid = uuid.UUID(token_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid token id")
        token = db.get(ApiToken, tid)
        if not token:
            raise HTTPException(status_code=404, detail="unknown token")
        if token.id == principal.id:
            raise HTTPException(status_code=409, detail="cannot revoke the token you are using")
        token.revoked_at = datetime.now(timezone.utc)
        db.commit()
        audit.log_event(db, principal.name, "token_revoke", detail={"name": token.name})
        return {"revoked": token.name}
    finally:
        db.close()
