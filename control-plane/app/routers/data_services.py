"""Сервисы данных среды: БД и S3-хранилища в namespace проекта — список, смена версии, миграция, копирование S3."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.auth import Principal, require, require_scoped
from app.db.base import SessionLocal
from app.services import audit, project_repo
from app.services import dataservices as ds

router = APIRouter(prefix="/api/projects/{slug}/environments/{env}/data-services", tags=["data-services"])


class VersionBody(BaseModel):
    tag: str = Field(min_length=1, max_length=128)
    backup: bool = True


class MigrateBody(BaseModel):
    target_tag: str = Field(min_length=1, max_length=128)
    stop_app: bool = False           # остановить приложения на время копирования (согласованные данные)


class S3Dest(BaseModel):
    endpoint: str = Field(max_length=300)
    access_key: str = Field(max_length=256)
    secret_key: str = Field(max_length=256)
    provider: Optional[str] = Field(default=None, max_length=30)


class FinalizeBody(BaseModel):
    confirm: str


def _ctx(db, slug: str, env_name: str):
    project = project_repo.get_project_by_slug(db, slug)
    if not project:
        raise HTTPException(status_code=404, detail=f"unknown project {slug}")
    env = project_repo.get_environment(db, project, env_name)
    if not env:
        raise HTTPException(status_code=404, detail=f"unknown environment {env_name} for project {slug}")
    if env.server:
        raise HTTPException(status_code=422, detail="data services are managed in Kubernetes environments only")
    return project, env


async def _run(fn, *args, **kw):
    try:
        return await run_in_threadpool(fn, *args, **kw)
    except ds.DataServiceError as e:
        raise HTTPException(status_code=e.status, detail=str(e))


def _audit(db, principal, project, env, action: str, name: str, detail: dict):
    audit.log_event(db, principal.name, action, project.id, environment_id=env.id, detail={"service": name, **detail})


@router.get("")
async def list_services(slug: str, env: str, principal: Principal = Depends(require_scoped("read"))):
    db = SessionLocal()
    try:
        _, e = _ctx(db, slug, env)
        return {"namespace": e.namespace, "services": await _run(ds.discover, e.namespace, e.cluster),
                "engines": {k: {"label": v["label"], "category": v["category"]} for k, v in ds.ENGINES.items()}}
    finally:
        db.close()


@router.post("/{name}/version")
async def change_version(slug: str, env: str, name: str, body: VersionBody, principal: Principal = Depends(require("projects:manage"))):
    """Смена тега образа в пределах той же мажорной версии (для БД — с дампом в отдельный том)."""
    db = SessionLocal()
    try:
        project, e = _ctx(db, slug, env)
        out = await _run(ds.change_version, e.namespace, name, body.tag, e.cluster, body.backup)
        _audit(db, principal, project, e, "data_service_version", name, {"from": out["from"], "to": out["to"]})
        return out
    finally:
        db.close()


@router.post("/{name}/migrate")
async def start_migration(slug: str, env: str, name: str, body: MigrateBody, principal: Principal = Depends(require("projects:manage"))):
    db = SessionLocal()
    try:
        project, e = _ctx(db, slug, env)
        st = await _run(ds.start_migration, e.namespace, name, body.target_tag, e.cluster, body.stop_app)
        _audit(db, principal, project, e, "data_migration_start", name, {"engine": st["engine"], "from": st["from_tag"], "to": st["to_tag"],
                                                                         "stop_app": st["stop_app"]})
        return st
    finally:
        db.close()


@router.post("/{name}/s3-copy")
async def start_s3_copy(slug: str, env: str, name: str, body: S3Dest, principal: Principal = Depends(require("projects:manage"))):
    db = SessionLocal()
    try:
        project, e = _ctx(db, slug, env)
        st = await _run(ds.start_s3_copy, e.namespace, name, body.model_dump(), e.cluster)
        _audit(db, principal, project, e, "data_s3_copy_start", name, {"destination": st["dest_endpoint"]})   # ключи в журнал не попадают
        return st
    finally:
        db.close()


@router.get("/{name}/migration")
async def migration_status(slug: str, env: str, name: str, principal: Principal = Depends(require("projects:manage"))):
    """Состояние миграции; каждый запрос продвигает автомат (создание → копирование → готово)."""
    db = SessionLocal()
    try:
        _, e = _ctx(db, slug, env)
        st = await _run(ds.advance, e.namespace, name, e.cluster)
        if not st:
            raise HTTPException(status_code=404, detail="no migration for this service")
        if st.get("phase") in ("failed", "copied", "copying") and st.get("job"):
            st = {**st, "log": await _run(ds.job_log, e.namespace, st["job"], e.cluster)}
        return st
    finally:
        db.close()


@router.post("/{name}/migration/cutover")
async def cutover(slug: str, env: str, name: str, principal: Principal = Depends(require("projects:manage"))):
    db = SessionLocal()
    try:
        project, e = _ctx(db, slug, env)
        st = await _run(ds.cutover, e.namespace, name, e.cluster)
        _audit(db, principal, project, e, "data_migration_cutover", name, {"to": st["to_tag"]})
        return st
    finally:
        db.close()


@router.post("/{name}/migration/rollback")
async def rollback(slug: str, env: str, name: str, principal: Principal = Depends(require("projects:manage"))):
    db = SessionLocal()
    try:
        project, e = _ctx(db, slug, env)
        st = await _run(ds.rollback, e.namespace, name, e.cluster)
        _audit(db, principal, project, e, "data_migration_rollback", name, {})
        return st
    finally:
        db.close()


@router.post("/{name}/migration/finalize")
async def finalize(slug: str, env: str, name: str, body: FinalizeBody, principal: Principal = Depends(require("projects:manage"))):
    """Необратимо удаляет старый экземпляр и том. Требует ввода имени сервиса."""
    if body.confirm != name:
        raise HTTPException(status_code=422, detail="confirm must equal the service name")
    db = SessionLocal()
    try:
        project, e = _ctx(db, slug, env)
        st = await _run(ds.finalize, e.namespace, name, e.cluster)
        _audit(db, principal, project, e, "data_migration_finalize", name, {})
        return st
    finally:
        db.close()
