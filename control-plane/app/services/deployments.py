"""Выкат и откат: общая логика для прямых вызовов и для выполнения согласованных заявок."""
import uuid
from typing import Optional

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.db.models import Environment, Project, Release
from app.services import audit, deploy, targets


def resolve_rollback_target(db: Session, env: Environment, to: Optional[str]) -> Release:
    """Определяет, на какой релиз откатываемся (ошибки — как у прямого вызова)."""
    if to:
        try:
            target_id = uuid.UUID(to)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid release id")
        target = db.get(Release, target_id)
        if not target or target.environment_id != env.id:
            raise HTTPException(status_code=404, detail="release not found for this environment")
    else:
        deployed = (db.query(Release).filter_by(environment_id=env.id, status="deployed")
                    .order_by(Release.created_at.desc()).limit(2).all())
        if len(deployed) < 2:
            raise HTTPException(status_code=409, detail="no previous successful release to roll back to")
        target = deployed[1]
    if not target.image_digest:
        raise HTTPException(status_code=409, detail="target release has no image to deploy")
    return target


def execute_rollback(db: Session, project: Project, env: Environment, target: Release, actor: str, via: Optional[dict] = None) -> dict:
    targets.deploy(db, project, env, target.image_digest, sync_secrets=False)
    rollback_release = Release(environment_id=env.id, image_digest=target.image_digest, git_revision=target.git_revision,
                               status="deployed", triggered_by=f"rollback:{target.id}", deployed_at=func.now())
    db.add(rollback_release)
    db.commit()
    detail = {"rolled_back_to_release": str(target.id), "image": target.image_digest}
    if via:
        detail.update(via)
    audit.log_event(db, actor, "rollback", project.id, env.id, detail)
    return {"accepted": True, "slug": project.slug, "environment": env.name, "rolled_back_to": str(target.id), "image": target.image_digest}


def schedule_redeploy(background_tasks: BackgroundTasks, db: Session, project: Project, env: Environment,
                      revision: Optional[str], actor: str, git_url: Optional[str] = None, via: Optional[dict] = None,
                      label: Optional[str] = None) -> dict:
    from app.routers.webhooks import run_build_and_deploy   # ленивый импорт: избегаем цикла
    url = git_url or project.clone_url
    rev = revision or env.branch
    background_tasks.add_task(run_build_and_deploy, project.id, env.id, url, rev, f"api:{label or actor}")
    detail = {"branch": env.branch, "revision": revision}
    if via:
        detail.update(via)
    audit.log_event(db, actor, "redeploy_requested", project.id, env.id, detail)
    return {"accepted": True, "slug": project.slug, "environment": env.name, "revision": rev}
