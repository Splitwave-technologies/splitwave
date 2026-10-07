"""Один проект без платного модуля projects. Проверяются при создании; существующее не затрагивается.
Среды проекта, пользователей, кластеры, серверы и временные среды PR проверяет платный модуль."""
from fastapi import HTTPException

from app import plugins
from app.db.models import Project, User

def usage(db) -> dict:
    return {"projects": db.query(Project).count(), "users": db.query(User).count()}


def check_project(db) -> None:
    """Можно ли создать ещё один проект. Без платного модуля projects платформа ведёт один проект; предел редакции проверяет модуль."""
    if plugins.PROJECTS:
        plugins.PROJECTS.check_new(db)
        return
    if db.query(Project).count() >= 1:
        raise HTTPException(status_code=403, detail={"error": "feature_not_licensed", "feature": "projects",
                                                      "message": "Several projects are part of the paid Projects module."})
