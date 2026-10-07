import re
from datetime import datetime
from typing import Optional

from starlette.concurrency import run_in_threadpool
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.auth import Principal, require
from app.db.base import SessionLocal
from app.db.models import Secret
from app.services import audit, crypto, dotenv, project_repo, repoinspect, targets
from app.services import secrets as secrets_svc

router = APIRouter(prefix="/api/projects/{slug}/secrets", tags=["secrets"])


class SecretValue(BaseModel):
    value: str = Field(min_length=1)
    expires_at: Optional[datetime] = None          # когда секрет перестаёт быть действительным (напоминание, не блокировка)
    rotation_days: Optional[int] = Field(default=None, ge=1, le=3650)  # как часто напоминать о смене значения


class SecretPolicy(BaseModel):
    expires_at: Optional[datetime] = None
    rotation_days: Optional[int] = Field(default=None, ge=1, le=3650)


class ImportBody(BaseModel):
    content: Optional[str] = Field(default=None, max_length=dotenv.MAX_TEXT)      # текст .env (вставлен или прочитан из файла на компьютере пользователя)
    from_repo: bool = False                                                       # или прочитать файл из репозитория проекта
    path: str = Field(default=".env", max_length=200)
    ref: Optional[str] = Field(default=None, max_length=200)                      # ветка/тег/коммит; по умолчанию ветка среды
    token: Optional[str] = Field(default=None, max_length=500)                    # токен чтения репозитория, если у проекта его нет; не сохраняется
    overwrite: bool = False                                                       # перезаписывать существующие секреты (иначе они пропускаются)
    dry_run: bool = False                                                         # только показать, что произойдёт
    keys: Optional[list[str]] = Field(default=None, max_length=512)               # применить только эти имена


class RestoreBody(BaseModel):
    version: int = Field(ge=1)


def _project(db, slug: str):
    project = project_repo.get_project_by_slug(db, slug)
    if not project:
        raise HTTPException(status_code=404, detail=f"unknown project {slug}")
    return project


def _scope(db, project, environment: Optional[str]) -> str:
    """None -> "*" (все среды); иначе имя существующей среды."""
    if environment is None:
        return secrets_svc.ALL
    if not project_repo.get_environment(db, project, environment):
        raise HTTPException(status_code=404, detail=f"unknown environment {environment} for project {project.slug}")
    return environment


@router.get("")
async def list_secret_keys(slug: str, response: Response, environment: Optional[str] = None,
                           principal: Principal = Depends(require("secrets:list"))):
    """Только имена и метаданные — значения не отдаются никому и никогда.
    С параметром environment — действующий набор этой среды (общие значения + значения среды)."""
    db = SessionLocal()
    try:
        response.headers["Cache-Control"] = "no-store"
        project = _project(db, slug)
        if environment is not None:
            _scope(db, project, environment)
        return secrets_svc.list_secrets(db, project, environment)
    finally:
        db.close()


@router.put("/{key}")
async def put_secret(slug: str, key: str, body: SecretValue, environment: Optional[str] = None,
                     principal: Principal = Depends(require("secrets:write"))):
    """Без environment — значение для всех сред проекта; с environment — только для этой среды (перекрывает общее)."""
    db = SessionLocal()
    try:
        project = _project(db, slug)
        scope = _scope(db, project, environment)
        given = body.model_fields_set
        try:
            created = secrets_svc.set_secret(
                db, project, key, body.value, scope, actor=principal.name,
                expires_at=body.expires_at if "expires_at" in given else secrets_svc.UNSET,
                rotation_days=body.rotation_days if "rotation_days" in given else secrets_svc.UNSET)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        version = db.query(Secret).filter_by(project_id=project.id, scope=scope, key=key).one().version
        audit.log_event(db, principal.name, "secret_set", project.id,
                        detail={"key": key, "scope": scope, "created": created, "version": version})
        return {"key": key, "scope": scope, "created": created, "version": version}
    finally:
        db.close()


@router.put("/{key}/policy")
async def put_policy(slug: str, key: str, body: SecretPolicy, environment: Optional[str] = None,
                     principal: Principal = Depends(require("secrets:write"))):
    """Срок действия и период ротации без смены значения. null сбрасывает поле."""
    db = SessionLocal()
    try:
        project = _project(db, slug)
        scope = _scope(db, project, environment)
        try:
            row = secrets_svc.set_policy(db, project, key, scope, body.expires_at, body.rotation_days)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if not row:
            raise HTTPException(status_code=404, detail=f"unknown secret {key}")
        audit.log_event(db, principal.name, "secret_policy", project.id,
                        detail={"key": key, "scope": scope, "expires_at": body.expires_at.isoformat() if body.expires_at else None,
                                "rotation_days": body.rotation_days})
        return {"key": key, "scope": scope, "status": secrets_svc.secret_status(row)}
    finally:
        db.close()


@router.get("/{key}/versions")
async def secret_versions(slug: str, key: str, response: Response, environment: Optional[str] = None,
                          principal: Principal = Depends(require("secrets:list"))):
    """История версий: кто и когда менял. Значения не отдаются."""
    db = SessionLocal()
    try:
        response.headers["Cache-Control"] = "no-store"
        project = _project(db, slug)
        versions = secrets_svc.list_versions(db, project, key, _scope(db, project, environment))
        if versions is None:
            raise HTTPException(status_code=404, detail=f"unknown secret {key}")
        return versions
    finally:
        db.close()


@router.post("/{key}/restore")
async def restore_secret(slug: str, key: str, body: RestoreBody, environment: Optional[str] = None,
                         principal: Principal = Depends(require("secrets:write"))):
    """Возвращает значение выбранной версии как новую версию. Чтобы оно попало в кластер, нужен sync или деплой."""
    db = SessionLocal()
    try:
        project = _project(db, slug)
        scope = _scope(db, project, environment)
        new_version = secrets_svc.restore_version(db, project, key, scope, body.version, principal.name)
        if new_version is None:
            raise HTTPException(status_code=404, detail=f"unknown secret version {key}@{body.version}")
        audit.log_event(db, principal.name, "secret_restore", project.id,
                        detail={"key": key, "scope": scope, "from_version": body.version, "version": new_version})
        return {"key": key, "scope": scope, "version": new_version}
    finally:
        db.close()


@router.delete("/{key}")
async def delete_secret(slug: str, key: str, environment: Optional[str] = None,
                        principal: Principal = Depends(require("secrets:write"))):
    db = SessionLocal()
    try:
        project = _project(db, slug)
        scope = _scope(db, project, environment)
        if not secrets_svc.delete_secret(db, project, key, scope):
            raise HTTPException(status_code=404, detail=f"unknown secret {key}")
        audit.log_event(db, principal.name, "secret_delete", project.id, detail={"key": key, "scope": scope})
        return {"deleted": key, "scope": scope}
    finally:
        db.close()


@router.post("/sync")
async def sync_secrets(slug: str, environment: str = "prod", principal: Principal = Depends(require("secrets:write"))):
    """Отправляет действующий набор секретов среды в кластер сразу, без сборки (например, после смены пароля)."""
    db = SessionLocal()
    try:
        project = _project(db, slug)
        env = project_repo.get_environment(db, project, environment)
        if not env:
            raise HTTPException(status_code=404, detail=f"unknown environment {environment} for project {slug}")
        try:
            count = await run_in_threadpool(targets.sync_secrets, db, project, env)
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        audit.log_event(db, principal.name, "secrets_sync", project.id, env.id, {"keys": count})
        return {"synced": count, "environment": env.name}
    finally:
        db.close()


_REPO_PATH = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._@/-]{0,199}$")


def _read_repo_env(project, scope: str, body: ImportBody, db) -> tuple[str, str]:
    """(текст файла, откуда). Файл читается через API провайдера: значения попадают сразу в секреты и в ответ не возвращаются."""
    path = body.path.strip()
    if not _REPO_PATH.match(path) or ".." in path.split("/") or "//" in path:
        raise HTTPException(status_code=422, detail="invalid path: a relative file path inside the repository, e.g. .env or deploy/prod.env")
    env = project_repo.get_environment(db, project, scope) if scope != secrets_svc.ALL else (project.environments[0] if project.environments else None)
    ref = body.ref or (env.branch if env else "main")
    try:
        repo = repoinspect.parse_repo_url(project.clone_url, project.provider)
        token = body.token or (crypto.decrypt_value(project.git_token_enc) if project.git_token_enc else None) or repoinspect.platform_token(repo)
        text = repoinspect.CLIENTS[repo.provider](repo, token).read_file(path, ref)
    except repoinspect.InspectError as e:
        raise HTTPException(status_code=e.status if e.status in (404, 422, 429, 502) else 422, detail=e.message)
    if text is None:
        raise HTTPException(status_code=404, detail=f"file {path} not found in the repository (branch {ref})")
    if len(text.encode()) >= repoinspect.MAX_FILE:
        raise HTTPException(status_code=422, detail=f"file {path} is too large (limit {repoinspect.MAX_FILE // 1024} KB)")
    return text, f"repo:{path}@{ref}"


@router.post("/import")
async def import_env(slug: str, body: ImportBody, environment: Optional[str] = None, principal: Principal = Depends(require("secrets:write"))):
    """Импорт переменных из .env в секреты проекта. Значения нигде не возвращаются и не попадают в аудит (только имена и счётчики).
    dry_run — предпросмотр без записи. Существующие секреты пропускаются, если не указано overwrite; совпадающие значения не создают новую версию."""
    if (body.content is None) == (not body.from_repo):
        raise HTTPException(status_code=422, detail="pass either content or from_repo=true")
    db = SessionLocal()
    try:
        project = _project(db, slug)
        scope = _scope(db, project, environment)
        if body.from_repo:
            text, source = await run_in_threadpool(_read_repo_env, project, scope, body, db)
        else:
            text, source = body.content, "upload"
        parsed = dotenv.parse(text)
        wanted = set(body.keys) if body.keys is not None else None
        existing = {r.key: r for r in db.query(Secret).filter_by(project_id=project.id, scope=scope).all()}
        items, to_write = [], []
        for key, value in parsed.entries.items():
            row = existing.get(key)
            if wanted is not None and key not in wanted:
                action = "not_selected"
            elif row is None:
                action = "create"
            elif crypto.decrypt_value(row.encrypted_value) == value:
                action = "same"
            else:
                action = "update" if body.overwrite else "exists"
            items.append({"key": key, "action": action, "notes": parsed.notes.get(key, [])})
            if action in ("create", "update"):
                to_write.append((key, value))
        counts = {a: sum(1 for x in items if x["action"] == a) for a in ("create", "update", "same", "exists", "not_selected")}
        counts["invalid"] = len(parsed.problems)
        if not body.dry_run and to_write:
            try:
                for key, value in to_write:
                    secrets_svc.set_secret(db, project, key, value, scope, actor=principal.name, reason="import")
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
            audit.log_event(db, principal.name, "secrets_imported", project.id,
                            detail={"source": source, "scope": scope, "created": counts["create"], "updated": counts["update"],
                                    "skipped": counts["exists"] + counts["same"] + counts["not_selected"], "invalid": counts["invalid"],
                                    "keys": [k for k, _ in to_write][:100]})
        return {"source": source, "scope": scope, "applied": (not body.dry_run) and bool(to_write), "items": items, "problems": parsed.problems, "counts": counts}
    finally:
        db.close()
