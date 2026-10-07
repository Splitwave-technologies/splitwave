from typing import Optional

from sqlalchemy.orm import Session

from app import plugins

from app.db.models import Environment, Project


def primary_project(db: Session) -> Optional[Project]:
    """Единственный проект без платного модуля: самый ранний."""
    return db.query(Project).join(Environment, Environment.project_id == Project.id).order_by(Environment.created_at, Project.slug).first()


def is_routed(db: Session, project: Project) -> bool:
    """Выкатывается ли проект по push: без платного модуля projects только основной."""
    if plugins.PROJECTS:
        return plugins.PROJECTS.routed(db, project)
    primary = primary_project(db)
    return primary is not None and primary.id == project.id


def get_project_by_repo(db: Session, repo_full_name: str, provider: str = "github") -> Optional[Project]:
    return db.query(Project).filter_by(repo_full_name=repo_full_name, provider=provider).first()


def get_project_by_slug(db: Session, slug: str) -> Optional[Project]:
    return db.query(Project).filter_by(slug=slug).first()


def list_projects(db: Session) -> list[Project]:
    return db.query(Project).all()


def get_environment(db: Session, project: Project, name: str = "prod") -> Optional[Environment]:
    return db.query(Environment).filter_by(project_id=project.id, name=name).first()


def get_environment_by_branch(db: Session, project: Project, branch: str) -> Optional[Environment]:
    return (
        db.query(Environment)
        .filter_by(project_id=project.id, branch=branch)
        .first()
    )


def primary_environment(project: Project) -> Optional[Environment]:
    """Единственная среда проекта без платного модуля: самая ранняя из обычных (не временных)."""
    regular = [e for e in project.environments if not e.preview_of]
    return min(regular, key=lambda e: (e.created_at is None, e.created_at, e.name)) if regular else None


def list_environments_by_branch(db: Session, project: Project, branch: str) -> list[Environment]:
    """Среды, которые собираются и выкатываются при push в ветку. Несколько сред на проект — платный модуль environments."""
    if plugins.ENVIRONMENTS:
        return plugins.ENVIRONMENTS.route_branch(db, project, branch)
    env = primary_environment(project)
    return [env] if env and env.branch == branch else []
