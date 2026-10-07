"""Без платного модуля environments у проекта одна среда; вторая создаётся только модулем."""
from app.db.base import SessionLocal
from app.db.models import Environment, Project

ENV = {"name": "prod", "namespace": "apps", "deployment_name": "a", "container_name": "a"}


def _project(client, admin, envs=1, slug="p1"):
    return client.post("/api/projects", json={"slug": slug, "repo_full_name": f"acme/{slug}",
                                              "environments": [dict(ENV, name=f"e{i}") for i in range(envs)]}, headers=admin)


def test_a_project_with_several_environments_needs_the_paid_module(client, admin, single_environment):
    r = _project(client, admin, envs=2)
    assert r.status_code == 403 and r.json()["detail"]["feature"] == "environments"
    assert SessionLocal().query(Project).count() == 0
    assert _project(client, admin).status_code == 201


def test_a_second_environment_cannot_be_added(client, admin, single_environment):
    assert _project(client, admin).status_code == 201
    r = client.post("/api/projects/p1/environments", json=dict(ENV, name="staging"), headers=admin)
    assert r.status_code == 403 and r.json()["detail"]["feature"] == "environments"
    assert client.get("/api/me", headers=admin).json()["environments"] is False


def test_push_deploys_only_the_single_environment(client, admin, single_environment):
    from app.services import project_repo
    assert _project(client, admin).status_code == 201
    db = SessionLocal()
    p = db.query(Project).one()
    db.add(Environment(project_id=p.id, name="extra", namespace="x", deployment_name="x", container_name="x", branch="main"))  # строку вписали в базу вручную
    db.commit()
    db.refresh(p)
    routed = project_repo.list_environments_by_branch(db, p, "main")
    assert [e.name for e in routed] == ["e0"]                       # выкатывается только исходная среда
    assert project_repo.list_environments_by_branch(db, p, "develop") == []
    db.close()
