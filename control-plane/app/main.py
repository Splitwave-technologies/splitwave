import asyncio
import logging
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db.migrate import run_migrations
from app import auth
from app.auth import Principal, require_any
from app.db.base import get_db
from app.db.models import Project
from app.db.base import SessionLocal
from app.routers import audit as audit_router
from app.routers import connections as connections_router
from app.routers import explorer as explorer_router
from app.licensing import current_license
from app.netutil import is_https
from app import plugins
from app.plugins import load_enterprise
from app.services import checks
from app.routers import license as license_router
from app.routers import account, agent_files, approvals, environments, notifications, onboarding, overview, projects, runtime, secrets, tokens, users, webhooks

logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name)
app.include_router(webhooks.router)
app.include_router(agent_files.router)
app.include_router(projects.router)
app.include_router(onboarding.router)
app.include_router(secrets.router)
app.include_router(environments.router)
app.include_router(overview.router)
app.include_router(account.router)
app.include_router(approvals.router)
app.include_router(notifications.router)
app.include_router(users.router)
app.include_router(runtime.router)
app.include_router(connections_router.router)
app.include_router(explorer_router.router)
app.include_router(tokens.router)
app.include_router(audit_router.router)
app.include_router(license_router.router)
from app.routers import updates as updates_router  # noqa: E402
app.include_router(updates_router.router)
app.state.ee_loaded = load_enterprise(app)

UI_DIR = Path(__file__).parent / "ui"

# Строгая политика содержимого: только свои скрипты и стили, без внешних ресурсов (работает и в закрытых сетях).
UI_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
          "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    path = request.url.path
    if path.startswith("/ui"):
        response.headers["Content-Security-Policy"] = UI_CSP
        response.headers.setdefault("Cache-Control", "no-cache")          # браузер проверяет файлы интерфейса при каждой загрузке (304, если не менялись): после обновления видно новую версию сразу
    if path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    if is_https(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/api/me")
async def me(principal: Principal = Depends(require_any), db=Depends(get_db)):
    """Кто вы и что вам можно (интерфейс по этому решает, какие действия показывать)."""
    slugs = {str(p.id): p.slug for p in db.query(Project).all()}
    return {
        "name": principal.name, "role": principal.role, "kind": principal.kind,
        "permissions": principal.permissions(),
        "features": sorted(current_license().features),   # платные функции, действующие по лицензии (интерфейс показывает их разделы)
        "projects": plugins.PROJECTS is not None,           # платный модуль: несколько проектов
        "environments": plugins.ENVIRONMENTS is not None,   # платный модуль: несколько сред на проект
        "teams": plugins.TEAMS is not None,      # платный модуль команд: несколько пользователей, роли, участники проектов
        "restricted": principal.restricted, "totp_enabled": principal.totp_enabled,
        "csrf_token": principal.csrf,            # только для сессий (нужен интерфейсу для изменяющих запросов)
        "memberships": [{"project": slugs.get(pid, pid), "role": r} for pid, r in principal.memberships.items()],
        "project_scope": slugs.get(principal.project_scope) if principal.project_scope else None,
        # эффективные права по проектам (роль участника или ограничение токена): по ним интерфейс показывает действия на странице проекта
        "project_permissions": {slugs.get(pid, pid): principal.permissions(pid)
                                for pid in ({*principal.memberships} | ({principal.project_scope} if principal.project_scope else set()))},
    }


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/ui/")


@app.on_event("startup")
async def on_startup():
    run_migrations()
    db = SessionLocal()
    try:
        auth.bootstrap_admin(db)
    finally:
        db.close()
    asyncio.create_task(checks.periodic())


@app.get("/health")
async def health():
    return {"status": "ok"}


if UI_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")
