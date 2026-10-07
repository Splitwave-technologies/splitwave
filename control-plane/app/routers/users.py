"""Пользователи и участники проектов (только администратор).
Ядро ведёт одну встроенную учётную запись администратора. Несколько пользователей, роли и участники проектов — платная функция teams
(модуль platform_ee подключает её через plugins.set_teams)."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app import plugins, security
from app.auth import Principal, require
from app.db.base import SessionLocal
from app.db.models import User
from app.routers.account import revoke_user_sessions
from app.services import audit

router = APIRouter(tags=["users"])


class NewUser(BaseModel):
    username: str = Field(pattern=r"^[a-z0-9][a-z0-9._@+-]{2,63}$")        # допускается адрес электронной почты

    @field_validator("username", mode="before")
    @classmethod
    def _lower(cls, v):
        return v.strip().lower() if isinstance(v, str) else v
    role: Optional[str] = None
    password: Optional[str] = Field(default=None, max_length=256)


class UserPatch(BaseModel):
    role: Optional[str] = None
    disabled: Optional[bool] = None
    reset_password: bool = False
    reset_totp: bool = False


class MemberBody(BaseModel):
    role: str


def user_view(u: User) -> dict:
    return {"id": str(u.id), "username": u.username, "role": u.role, "disabled": u.disabled, "totp_enabled": u.totp_enabled,
            "must_change_password": u.must_change_password, "auth_source": u.auth_source,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "memberships": [{"project": m.project.slug, "role": m.role} for m in u.memberships]}


def _not_licensed():
    return HTTPException(status_code=403, detail={"error": "feature_not_licensed", "feature": "teams",
                                                  "message": "Several user accounts, roles and project members are part of the paid Teams module."})


@router.get("/api/users")
async def list_users(principal: Principal = Depends(require("users:manage"))):
    if plugins.TEAMS:
        return plugins.TEAMS.list_users(principal)
    db = SessionLocal()
    try:
        return [user_view(u) for u in db.query(User).order_by(User.username).limit(1).all()]
    finally:
        db.close()


@router.post("/api/users", status_code=201)
async def create_user(body: NewUser, principal: Principal = Depends(require("users:manage"))):
    """Временный пароль показывается один раз; при первом входе пользователь обязан его сменить."""
    if plugins.TEAMS:
        return plugins.TEAMS.create_user(body, principal)
    if body.role not in (None, "admin"):
        raise _not_licensed()
    db = SessionLocal()
    try:
        if db.query(User).filter_by(username=body.username).first():
            raise HTTPException(status_code=409, detail="user already exists")
        if db.query(User).count() >= 1:
            raise _not_licensed()
        temp = body.password is None
        password = body.password or security.random_password()
        problems = security.password_problems(password, body.username)
        if problems:
            raise HTTPException(status_code=422, detail="weak password: " + "; ".join(problems))
        user = User(username=body.username, role="admin", password_hash=security.hash_password(password), must_change_password=True)
        db.add(user)
        db.commit()
        audit.log_event(db, principal.name, "user_create", detail={"username": body.username, "role": "admin"})
        out = user_view(user)
        if temp:
            out["temporary_password"] = password
        return out
    finally:
        db.close()


@router.patch("/api/users/{user_id}")
async def update_user(user_id: str, body: UserPatch, principal: Principal = Depends(require("users:manage"))):
    if plugins.TEAMS:
        return plugins.TEAMS.update_user(user_id, body, principal)
    if body.role is not None or body.disabled is not None:
        raise _not_licensed()
    db = SessionLocal()
    try:
        try:
            user = db.get(User, uuid.UUID(user_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid user id")
        if not user:
            raise HTTPException(status_code=404, detail="unknown user")
        if not (body.reset_password or body.reset_totp):
            raise HTTPException(status_code=422, detail="nothing to update")
        if user.auth_source != "local":
            raise HTTPException(status_code=422, detail="this account is managed by the identity provider")
        extra = {}
        if body.reset_password:
            temp = security.random_password()
            user.password_hash = security.hash_password(temp)
            user.must_change_password = True
            extra["temporary_password"] = temp
        if body.reset_totp:
            user.totp_enabled, user.totp_secret, user.recovery_codes, user.totp_last_counter = False, None, None, None
        db.commit()
        revoke_user_sessions(db, user.id)
        audit.log_event(db, principal.name, "user_update", detail={"username": user.username, "reset_password": body.reset_password, "reset_totp": body.reset_totp})
        return {**user_view(user), **extra}
    finally:
        db.close()


@router.delete("/api/users/{user_id}")
async def delete_user(user_id: str, principal: Principal = Depends(require("users:manage"))):
    if plugins.TEAMS:
        return plugins.TEAMS.delete_user(user_id, principal)
    raise _not_licensed()


# ───────────── участники проекта ─────────────
@router.get("/api/projects/{slug}/members")
async def list_members(slug: str, principal: Principal = Depends(require("read"))):
    if plugins.TEAMS:
        return plugins.TEAMS.list_members(slug, principal)
    return []


@router.put("/api/projects/{slug}/members/{username}")
async def set_member(slug: str, username: str, body: MemberBody, principal: Principal = Depends(require("users:manage"))):
    if plugins.TEAMS:
        return plugins.TEAMS.set_member(slug, username, body.role, principal)
    raise _not_licensed()


@router.delete("/api/projects/{slug}/members/{username}")
async def remove_member(slug: str, username: str, principal: Principal = Depends(require("users:manage"))):
    if plugins.TEAMS:
        return plugins.TEAMS.remove_member(slug, username, principal)
    raise _not_licensed()
