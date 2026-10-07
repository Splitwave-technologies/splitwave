"""Удаление среды из платформы (общая логика для API и для платных модулей, например временных сред)."""
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, Environment, Project
from app.services import secrets as secrets_svc


def remove_environment(db: Session, project: Project, env: Environment) -> None:
    """Удаляет среду, её релизы и секреты этой среды из платформы (ресурсы в кластере не трогает).
    Аудит сохраняется: события отвязываются от среды, а её имя и проект пишутся в context."""
    name = env.name
    for ev in db.query(AuditEvent).filter(AuditEvent.environment_id == env.id).all():
        ev.context = {**(ev.context or {}), "project_slug": project.slug, "environment_name": name}
        ev.environment_id = None
    db.flush()
    secrets_svc.drop_environment_secrets(db, project, name)
    db.delete(env)
    db.commit()
