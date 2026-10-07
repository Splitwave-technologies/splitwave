"""Аутентификация и права. Два способа входа:
  * API-токен (Authorization: Bearer ...) — для CI и автоматизации;
  * сессия пользователя (httpOnly-кука + CSRF-заголовок на изменяющих запросах) — для веб-интерфейса.
Права: глобальная роль пользователя/токена + роль участника в конкретном проекте.
Вебхук GitHub защищён отдельно (HMAC), /health открыт."""
import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app import plugins
from app.config import settings
from app.db.base import get_db
from app.db.models import ApiToken, Project, UserSession
from app.security import hash_session_token
from app.services import audit

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "dsp_"
SESSION_COOKIE = "dsp_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Права по ролям. Разработчик видит только ИМЕНА секретов и может деплоить, но не читает значения
# (значения не отдаёт никакой эндпоинт) и не меняет их — секреты ведёт DevOps/админ.
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"*"},
    "devops": {"read", "deploy", "rollback", "secrets:list", "secrets:write", "audit:read", "license:read"},
    "developer": {"read", "deploy", "secrets:list"},
    "viewer": {"read"},
    "none": set(),   # глобальных прав нет: только проекты, где пользователь участник
}
ROLES = ("admin", "devops", "developer", "viewer")            # роли токенов и глобальные роли пользователей (плюс "none")
PROJECT_ROLES = ("devops", "developer", "viewer")             # роли участника проекта
# Права, которые имеют смысл на уровне проекта; остальные (токены, пользователи, аудит, проекты) — только глобально.
PROJECT_PERMS = {"read", "deploy", "rollback", "secrets:list", "secrets:write"}
ALL_PERMISSIONS = sorted({"read", "deploy", "rollback", "secrets:list", "secrets:write", "audit:read",
                          "license:read", "tokens:manage", "projects:manage", "users:manage", "notifications:manage", "integrations:manage"})


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Principal:
    id: uuid.UUID
    name: str
    role: str
    kind: str = "token"                                   # token | session
    user_id: Optional[uuid.UUID] = None
    memberships: dict = field(default_factory=dict)       # str(project_id) -> роль участника
    project_scope: Optional[str] = None                   # токен, ограниченный одним проектом
    restricted: Optional[str] = None                      # "totp_setup" | "password_change"
    csrf: Optional[str] = None
    session_id: Optional[uuid.UUID] = None
    totp_enabled: bool = False

    def _global(self) -> set[str]:
        return ROLE_PERMISSIONS.get(self.role, set())

    def can(self, permission: str, project_id=None) -> bool:
        if self.restricted:
            return False
        if self.project_scope is not None:                # ограниченный токен: только свой проект и проектные права
            return (project_id is not None and str(project_id) == self.project_scope
                    and permission in PROJECT_PERMS and ("*" in self._global() or permission in self._global()))
        if "*" in self._global() or permission in self._global():
            return True
        if project_id is not None and permission in PROJECT_PERMS:
            member_role = self.memberships.get(str(project_id))
            return bool(member_role) and permission in ROLE_PERMISSIONS.get(member_role, set())
        return False

    def permissions(self, project_id=None) -> list[str]:
        if self.restricted:
            return []
        return [p for p in ALL_PERMISSIONS if self.can(p, project_id)]

    def allowed_project_ids(self) -> Optional[set[str]]:
        """None — все проекты (есть глобальное право read); иначе множество id проектов, доступных по членству/ограничению."""
        if self.restricted:
            return set()
        if self.project_scope is not None:
            return {self.project_scope}
        if "*" in self._global() or "read" in self._global():
            return None
        return set(self.memberships)

    def has_any_access(self) -> bool:
        ids = self.allowed_project_ids()
        return ids is None or len(ids) > 0


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def _authenticate(db: Session, request: Request, authorization: Optional[str]) -> Principal:
    now = datetime.now(timezone.utc)

    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
        token = db.query(ApiToken).filter_by(token_hash=hash_token(raw)).first()
        if token is None or token.revoked_at is not None:
            raise _unauthorized("invalid token")
        token.last_used_at = now
        db.commit()
        return Principal(id=token.id, name=token.name, role=token.role, kind="token",
                         project_scope=str(token.project_id) if token.project_id else None)

    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        sess = db.query(UserSession).filter_by(token_hash=hash_session_token(cookie)).first()
        if sess is None or sess.revoked_at is not None or _aware(sess.expires_at) < now:
            raise _unauthorized("session expired")
        last = _aware(sess.last_seen_at) or _aware(sess.created_at)
        if last and now - last > timedelta(minutes=settings.session_idle_minutes):
            sess.revoked_at = now
            db.commit()
            raise _unauthorized("session expired")
        user = sess.user
        if user.disabled:
            raise _unauthorized("account disabled")
        if request.method not in SAFE_METHODS:
            sent = request.headers.get("x-csrf-token", "")
            if not sent or not hmac.compare_digest(sent, sess.csrf_token):
                raise HTTPException(status_code=403, detail="csrf token missing or invalid")
        sess.last_seen_at = now
        db.commit()
        # ограничение считается по текущему состоянию учётной записи и настройкам: политика действует сразу и на открытые сессии
        restricted = None
        if user.must_change_password:
            restricted = "password_change"
        elif settings.require_2fa and not user.totp_enabled and user.auth_source == "local":
            restricted = "totp_setup"
        role, memberships = plugins.TEAMS.resolve_session(user) if plugins.TEAMS else _single_account(user)
        return Principal(id=user.id, name=user.username, role=role, kind="session", user_id=user.id,
                         memberships=memberships,
                         restricted=restricted, csrf=sess.csrf_token, session_id=sess.id, totp_enabled=user.totp_enabled)

    raise _unauthorized("missing credentials")


def _single_account(user) -> tuple[str, dict]:
    """Без платного модуля команд действует только встроенный администратор; остальные учётные записи прав не имеют."""
    return ("admin" if user.role == "admin" else "none"), {}


def _project_id_from_path(db: Session, request: Request):
    slug = request.path_params.get("slug")
    if not slug:
        return None
    project = db.query(Project).filter_by(slug=slug).first()
    return project.id if project else None


def require(permission: str):
    """Зависимость FastAPI: пускает только при нужном праве (для путей с {slug} — с учётом роли в этом проекте);
    отказ пишется в аудит."""

    def dependency(request: Request, authorization: Optional[str] = Header(default=None),
                   db: Session = Depends(get_db)) -> Principal:
        principal = _authenticate(db, request, authorization)
        if principal.restricted:
            raise HTTPException(status_code=403, detail={"error": "restricted", "reason": principal.restricted})
        project_id = _project_id_from_path(db, request)
        if not principal.can(permission, project_id):
            audit.log_event(db, principal.name, "access_denied",
                            detail={"permission": permission, "method": request.method, "path": request.url.path})
            raise HTTPException(status_code=403, detail=f"role '{principal.role}' lacks permission '{permission}'")
        return principal

    return dependency


def require_scoped(permission: str):
    """Для списков и сводок: пускает глобальное право ИЛИ участие хотя бы в одном проекте; эндпоинт сам фильтрует данные."""

    def dependency(request: Request, authorization: Optional[str] = Header(default=None),
                   db: Session = Depends(get_db)) -> Principal:
        principal = _authenticate(db, request, authorization)
        if principal.restricted:
            raise HTTPException(status_code=403, detail={"error": "restricted", "reason": principal.restricted})
        if not (principal.can(permission) or principal.has_any_access()):
            audit.log_event(db, principal.name, "access_denied",
                            detail={"permission": permission, "method": request.method, "path": request.url.path})
            raise HTTPException(status_code=403, detail=f"role '{principal.role}' lacks permission '{permission}'")
        return principal

    return dependency


def require_any(request: Request, authorization: Optional[str] = Header(default=None),
                db: Session = Depends(get_db)) -> Principal:
    """Любая действительная учётка, в том числе «ограниченная» (для /api/auth/* и /api/me)."""
    return _authenticate(db, request, authorization)


def bootstrap_admin(db: Session) -> bool:
    """Создаёт первого администратора из ADMIN_BOOTSTRAP_TOKEN, если токенов в БД ещё нет."""
    if db.query(ApiToken).count() > 0:
        return False
    raw = settings.admin_bootstrap_token
    if not raw:
        logger.warning("В БД нет токенов API, а ADMIN_BOOTSTRAP_TOKEN не задан — API будет недоступен")
        return False
    if len(raw) < 24:
        logger.error("ADMIN_BOOTSTRAP_TOKEN слишком короткий (нужно >= 24 символов) — игнорирую")
        return False
    db.add(ApiToken(name="bootstrap-admin", role="admin", token_hash=hash_token(raw), token_prefix=raw[:8]))
    db.commit()
    audit.log_event(db, "system", "token_bootstrap", detail={"name": "bootstrap-admin"})
    return True
