"""Вход в систему, выход, смена пароля, двухфакторная защита и сессии пользователя."""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import auth, security
from app.netutil import client_ip, is_https
from app.auth import SESSION_COOKIE, Principal, require_any
from app.config import settings
from app.db.base import get_db
from app.db.models import LoginAttempt, User, UserSession
from app.services import audit, crypto

router = APIRouter(prefix="/api/auth", tags=["account"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    totp: Optional[str] = Field(default=None, max_length=32)   # код из приложения или код восстановления


class PasswordBody(BaseModel):
    current: str = Field(min_length=1, max_length=256)
    new: str = Field(min_length=1, max_length=256)


class CodeBody(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class DisableBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=32)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ip(request: Request) -> str:
    return client_ip(request)


# ───────────── защита от перебора ─────────────
def _recent_failures(db: Session, key: str) -> int:
    since = _now() - timedelta(minutes=settings.login_window_minutes)
    last_ok = (db.query(LoginAttempt).filter(LoginAttempt.key == key, LoginAttempt.success.is_(True))
               .order_by(LoginAttempt.created_at.desc()).first())
    q = db.query(LoginAttempt).filter(LoginAttempt.key == key, LoginAttempt.success.is_(False), LoginAttempt.created_at >= since)
    if last_ok is not None and _aware(last_ok.created_at) >= since:
        q = q.filter(LoginAttempt.created_at > last_ok.created_at)
    return q.count()


def _record(db: Session, key: str, success: bool) -> None:
    db.add(LoginAttempt(key=key, success=success))
    db.query(LoginAttempt).filter(LoginAttempt.created_at < _now() - timedelta(days=2)).delete()   # чистим старые записи
    db.commit()


def _check_locked(db: Session, user_key: str, ip_key: str) -> None:
    if _recent_failures(db, user_key) >= settings.login_max_failures or _recent_failures(db, ip_key) >= settings.login_max_failures * 5:
        raise HTTPException(status_code=429, detail="too many failed attempts, try again later",
                            headers={"Retry-After": str(settings.login_window_minutes * 60)})


# ───────────── вспомогательное ─────────────
def _secure_cookie(request: Request) -> bool:
    return settings.cookie_secure if settings.cookie_secure is not None else is_https(request)


def _new_session(db: Session, request: Request, response: Response, user: User, restricted: Optional[str]) -> UserSession:
    token = security.new_session_token()
    sess = UserSession(user_id=user.id, token_hash=security.hash_session_token(token), csrf_token=security.new_session_token(),
                       restricted=restricted, ip=_ip(request), user_agent=(request.headers.get("user-agent") or "")[:200],
                       last_seen_at=_now(), expires_at=_now() + timedelta(hours=settings.session_ttl_hours))
    db.add(sess)
    db.commit()
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", secure=_secure_cookie(request),
                        max_age=settings.session_ttl_hours * 3600, path="/")
    return sess


def _user_of(db: Session, principal: Principal, local_only: bool = False) -> User:
    if principal.user_id is None:
        raise HTTPException(status_code=400, detail="this action is for user accounts, not API tokens")
    user = db.get(User, principal.user_id)
    if not user:
        raise HTTPException(status_code=401, detail="account not found")
    if local_only and user.auth_source != "local":
        raise HTTPException(status_code=403, detail="this account is managed by the identity provider")
    return user


def create_login_session(db: Session, request: Request, response: Response, user: User, restricted: Optional[str] = None) -> UserSession:
    """Открытая точка для модулей входа (SSO): выдаёт сессионную cookie так же, как обычный вход."""
    return _new_session(db, request, response, user, restricted)


def revoke_user_sessions(db: Session, user_id, except_session=None) -> int:
    n = 0
    for s in db.query(UserSession).filter(UserSession.user_id == user_id, UserSession.revoked_at.is_(None)).all():
        if except_session is not None and s.id == except_session:
            continue
        s.revoked_at = _now()
        n += 1
    db.commit()
    return n


def _check_second_factor(db: Session, user: User, code: Optional[str]) -> bool:
    """Проверяет код приложения (с защитой от повторного использования) или код восстановления (одноразовый)."""
    if not code:
        return False
    counter = security.verify_totp(crypto.decrypt_value(user.totp_secret), code)
    if counter is not None and (user.totp_last_counter is None or counter > user.totp_last_counter):
        user.totp_last_counter = counter
        db.commit()
        return True
    h = security.hash_recovery(code)
    codes = list(user.recovery_codes or [])
    if h in codes:
        codes.remove(h)
        user.recovery_codes = codes
        db.commit()
        return True
    return False


# ───────────── вход и выход ─────────────
@router.get("/providers")
async def providers():
    """Доступные внешние способы входа (без авторизации: нужны странице входа)."""
    from app import plugins
    return plugins.LOGIN_PROVIDERS


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db)):
    username = body.username.strip().lower()
    user_key, ip_key = f"user:{username}", f"ip:{_ip(request)}"
    _check_locked(db, user_key, ip_key)

    user = db.query(User).filter_by(username=username).first()
    ok = bool(user) and not user.disabled and user.auth_source == "local" and security.verify_password(body.password, user.password_hash)
    if not user:
        security.dummy_verify(body.password)          # одинаковое время ответа для существующих и несуществующих логинов
    if not ok:
        _record(db, user_key, False); _record(db, ip_key, False)
        audit.log_event(db, username[:64], "login_failed", detail={"ip": _ip(request)})
        raise HTTPException(status_code=401, detail="invalid credentials")

    if user.totp_enabled:
        if not body.totp:
            raise HTTPException(status_code=401, detail={"error": "totp_required"})
        if not _check_second_factor(db, user, body.totp):
            _record(db, user_key, False); _record(db, ip_key, False)
            audit.log_event(db, username, "login_failed", detail={"ip": _ip(request), "reason": "second_factor"})
            raise HTTPException(status_code=401, detail="invalid credentials")

    restricted = None
    if user.must_change_password:
        restricted = "password_change"
    elif settings.require_2fa and not user.totp_enabled and user.auth_source == "local":
        restricted = "totp_setup"

    _record(db, user_key, True)
    user.last_login_at = _now()
    db.commit()
    sess = _new_session(db, request, response, user, restricted)
    audit.log_event(db, username, "login", detail={"ip": _ip(request), "restricted": restricted})
    return {"name": user.username, "role": user.role, "csrf_token": sess.csrf_token, "restricted": restricted}


@router.post("/logout")
async def logout(response: Response, principal: Principal = Depends(require_any), db: Session = Depends(get_db)):
    if principal.session_id is not None:
        sess = db.get(UserSession, principal.session_id)
        if sess:
            sess.revoked_at = _now()
            db.commit()
        audit.log_event(db, principal.name, "logout")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


# ───────────── пароль ─────────────
@router.post("/password")
async def change_password(body: PasswordBody, principal: Principal = Depends(require_any), db: Session = Depends(get_db)):
    user = _user_of(db, principal, local_only=True)
    if not security.verify_password(body.current, user.password_hash):
        audit.log_event(db, user.username, "password_change_failed")
        raise HTTPException(status_code=403, detail="current password is incorrect")
    problems = security.password_problems(body.new, user.username)
    if problems:
        raise HTTPException(status_code=422, detail="weak password: " + "; ".join(problems))
    if security.verify_password(body.new, user.password_hash):
        raise HTTPException(status_code=422, detail="new password must differ from the current one")
    user.password_hash = security.hash_password(body.new)
    user.must_change_password = False
    db.commit()
    other = revoke_user_sessions(db, user.id, except_session=principal.session_id)
    audit.log_event(db, user.username, "password_change", detail={"other_sessions_revoked": other})
    return {"ok": True, "other_sessions_revoked": other}


# ───────────── двухфакторная защита ─────────────
@router.post("/totp/setup")
async def totp_setup(principal: Principal = Depends(require_any), db: Session = Depends(get_db)):
    user = _user_of(db, principal, local_only=True)
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="2FA is already enabled")
    secret = security.new_totp_secret()
    user.totp_secret = crypto.encrypt_value(secret)
    db.commit()
    return {"secret": secret, "uri": security.otpauth_uri(secret, user.username)}


@router.post("/totp/enable")
async def totp_enable(body: CodeBody, principal: Principal = Depends(require_any), db: Session = Depends(get_db)):
    user = _user_of(db, principal, local_only=True)
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="2FA is already enabled")
    if not user.totp_secret:
        raise HTTPException(status_code=409, detail="call /totp/setup first")
    counter = security.verify_totp(crypto.decrypt_value(user.totp_secret), body.code)
    if counter is None:
        raise HTTPException(status_code=422, detail="invalid code")
    codes = security.new_recovery_codes()
    user.totp_enabled = True
    user.totp_last_counter = counter
    user.recovery_codes = [security.hash_recovery(c) for c in codes]
    db.commit()
    audit.log_event(db, user.username, "totp_enable")
    return {"enabled": True, "recovery_codes": codes}      # показываются один раз


@router.post("/totp/disable")
async def totp_disable(body: DisableBody, principal: Principal = Depends(require_any), db: Session = Depends(get_db)):
    user = _user_of(db, principal, local_only=True)
    if settings.require_2fa:
        raise HTTPException(status_code=403, detail="2FA is mandatory on this installation")
    if not user.totp_enabled:
        raise HTTPException(status_code=409, detail="2FA is not enabled")
    if not security.verify_password(body.password, user.password_hash) or not _check_second_factor(db, user, body.code):
        audit.log_event(db, user.username, "totp_disable_failed")
        raise HTTPException(status_code=403, detail="password or code is incorrect")
    user.totp_enabled, user.totp_secret, user.recovery_codes, user.totp_last_counter = False, None, None, None
    db.commit()
    audit.log_event(db, user.username, "totp_disable")
    return {"enabled": False}


# ───────────── свои сессии ─────────────
def _session_view(s: UserSession, current_id) -> dict:
    return {"id": str(s.id), "created_at": s.created_at.isoformat() if s.created_at else None,
            "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None, "ip": s.ip, "user_agent": s.user_agent,
            "current": s.id == current_id}


@router.get("/sessions")
async def my_sessions(principal: Principal = Depends(require_any), db: Session = Depends(get_db)):
    user = _user_of(db, principal)
    live = [s for s in user.sessions if s.revoked_at is None and _aware(s.expires_at) > _now()]
    return [_session_view(s, principal.session_id) for s in sorted(live, key=lambda s: s.created_at or _now(), reverse=True)]


@router.delete("/sessions/{session_id}")
async def revoke_session(session_id: str, principal: Principal = Depends(require_any), db: Session = Depends(get_db)):
    user = _user_of(db, principal)
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid session id")
    sess = db.get(UserSession, sid)
    if not sess or sess.user_id != user.id:
        raise HTTPException(status_code=404, detail="unknown session")
    sess.revoked_at = _now()
    db.commit()
    audit.log_event(db, user.username, "session_revoke")
    return {"revoked": True}
