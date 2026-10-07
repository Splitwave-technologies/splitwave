"""One-off/idempotent seed: ensure the control-plane project itself exists in DB."""
from app.db.base import SessionLocal
from app.db.models import Environment, Project


def seed_control_plane() -> None:
    db = SessionLocal()
    try:
        project = db.query(Project).filter_by(slug="control-plane").first()
        if not project:
            project = Project(
                slug="control-plane",
                repo_full_name="Splitwave-technologies/splitwave",
                sub_path="control-plane",
                builder="default",
                registry_prefix="ghcr.io/splitwave-technologies",
            )
            db.add(project)
            db.flush()

        env = (
            db.query(Environment)
            .filter_by(project_id=project.id, name="prod")
            .first()
        )
        if not env:
            env = Environment(
                project_id=project.id,
                name="prod",
                namespace="default",
                deployment_name="control-plane",
                container_name="control-plane",
                branch="main",
                auto_deploy=True,
            )
            db.add(env)

        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    seed_control_plane()
