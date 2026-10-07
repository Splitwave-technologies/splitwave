"""Лимиты редакций в ядре: проекты (Community 1, Lite 5, Pro 25, Enterprise без лимита); существующее не трогаем. Среды и пользователей проверяет платный модуль."""
from datetime import date, timedelta
from types import SimpleNamespace as NS

import pytest

from app import licensing, plugins
from app.config import settings
from app.db.base import SessionLocal
from app.db.models import Environment, Project
from app.routers import onboarding
from app.services import provision

ENV = {"name": "prod", "namespace": "apps", "deployment_name": "a", "container_name": "a"}


def lic(tier, status="valid"):
    return licensing.LicenseInfo(status, tier if status == "valid" else "community", "Acme", frozenset(), None, "ok")


@pytest.fixture
def enforce(monkeypatch):
    monkeypatch.setattr(settings, "tier_limits_enforce", True)

    def use(tier, status="valid"):
        monkeypatch.setattr(licensing, "current_license", lambda refresh=False: lic(tier, status))
    use("community")
    return use


def mk(client, admin, n, envs=1):
    return client.post("/api/projects", json={"slug": f"p{n}", "repo_full_name": f"acme/p{n}",
                                              "environments": [dict(ENV, name=f"e{i}") for i in range(envs)]}, headers=admin)


def test_limit_table_matches_the_pricing():
    t = licensing.TIER_LIMITS
    assert t["community"] == {"projects": 1, "environments": 1, "users": 1, "clusters": 0, "servers": 0, "previews": 0}
    assert t["lite"] == {"projects": 5, "environments": 3, "users": 3, "clusters": 0, "servers": 0, "previews": 3}
    assert t["pro"] == {"projects": 25, "environments": 10, "users": 10, "clusters": 3, "servers": 5, "previews": 20}
    assert set(t["enterprise"].values()) == {None}
    assert licensing.limits(lic("lite")) == {"tier": "lite", "max_projects": 5, "max_environments": 3, "max_users": 3, "max_clusters": 0, "max_servers": 0, "max_previews": 3}
    assert licensing.limits(lic("pro", "expired"))["tier"] == "community"                       # истёкшая лицензия = Community
    assert licensing.limits(lic("weird"))["max_projects"] == 5                                    # неизвестная метка — как Lite


def test_without_the_projects_module_the_platform_runs_one_project(client, admin, single_project):
    assert mk(client, admin, 1).status_code == 201
    r = mk(client, admin, 2)
    assert r.status_code == 403
    d = r.json()["detail"]
    assert d["error"] == "feature_not_licensed" and d["feature"] == "projects"
    assert SessionLocal().query(Project).count() == 1
    assert client.get("/api/me", headers=admin).json()["projects"] is False


def test_existing_projects_stay_usable_when_the_module_goes_away(client, admin, monkeypatch):
    for i in range(1, 4):
        assert mk(client, admin, i).status_code == 201                                           # накоплено, пока модуль был
    monkeypatch.setattr(plugins, "PROJECTS", None)
    assert len(client.get("/api/projects", headers=admin).json()) == 3                           # всё видно и работает
    assert mk(client, admin, 9).status_code == 403                                                # новое нельзя
    assert client.delete("/api/projects/p1", headers=admin).status_code == 200
    assert client.delete("/api/projects/p2", headers=admin).status_code == 200
    assert mk(client, admin, 9).status_code == 403                                                # остался один: второй всё равно нельзя
    assert client.delete("/api/projects/p3", headers=admin).status_code == 200
    assert mk(client, admin, 9).status_code == 201


def test_only_the_first_project_deploys_on_push_without_the_module(client, admin, monkeypatch):
    from app.services import project_repo
    assert mk(client, admin, 1).status_code == 201 and mk(client, admin, 2).status_code == 201
    db = SessionLocal()
    first, second = [project_repo.get_project_by_slug(db, s) for s in ("p1", "p2")]
    assert project_repo.is_routed(db, first) and project_repo.is_routed(db, second)              # с модулем (заглушка) — оба
    monkeypatch.setattr(plugins, "PROJECTS", None)
    assert project_repo.is_routed(db, first) and not project_repo.is_routed(db, second)
    db.close()


def test_license_endpoint_reports_limits_and_usage(client, admin, enforce):
    mk(client, admin, 1)
    d = client.get("/api/license", headers=admin).json()
    assert d["limits"] == {"tier": "community", "max_projects": 1, "max_environments": 1, "max_users": 1, "max_clusters": 0, "max_servers": 0, "max_previews": 0}
    assert d["usage"] == {"projects": 1, "users": 0} and d["limits_enforced"] is True
    enforce("pro")
    d = client.get("/api/license", headers=admin).json()
    assert d["limits"]["max_projects"] == 25 and d["limits"]["max_users"] == 10 and d["limits"]["tier"] == "pro"


# ---------- мастер ----------

def test_wizard_refuses_a_second_project_before_touching_the_cluster(client, admin, single_project, monkeypatch):
    calls = []
    monkeypatch.setattr(provision, "apply", lambda docs, cluster=None: calls.append(docs) or [])
    monkeypatch.setattr(provision, "check_access", lambda cluster=None, ingress=False: {"allowed": True, "missing": []})
    assert mk(client, admin, 1).status_code == 201
    body = {"slug": "wiz", "repo_full_name": "acme/wiz", "build_method": "dockerfile", "port": 8080}
    r = client.post("/api/onboarding/create", json=body, headers=admin)
    assert r.status_code == 403 and r.json()["detail"]["error"] == "feature_not_licensed"
    assert calls == [] and SessionLocal().query(Project).count() == 1                           # в кластере ничего не создано


def test_wizard_inspect_reports_limits_and_usage(client, admin, enforce, monkeypatch):
    from app.services import repoinspect
    monkeypatch.setattr(provision, "check_access", lambda cluster=None, ingress=False: {"allowed": True, "missing": []})

    class Repo:
        def __init__(self, ref, token): pass
        def repo_info(self): return {"default_branch": "main", "private": False}
        def head_sha(self, b): return "a" * 40
        def list_dir(self, path, ref): return [("go.mod", False)] if not path else []
        def read_file(self, path, ref): return "module x\n"
    monkeypatch.setitem(repoinspect.CLIENTS, "github", Repo)
    mk(client, admin, 1)
    caps = client.post("/api/onboarding/inspect", json={"url": "https://github.com/acme/new"}, headers=admin).json()["capabilities"]
    assert caps["limits"] == {"tier": "community", "max_projects": 1, "max_environments": 1, "max_users": 1, "max_clusters": 0, "max_servers": 0, "max_previews": 0, "enforced": True}
    assert caps["usage"] == {"projects": 1, "users": 0}


def test_limits_cannot_be_switched_off_outside_test_mode():
    from app.config import Settings, apply_license_guard
    assert apply_license_guard(Settings(tier_limits_enforce=False), {}).tier_limits_enforce is True            # без тестового режима переключатель игнорируется
    assert apply_license_guard(Settings(tier_limits_enforce=False), {"SPLITWAVE_TEST_MODE": "1"}).tier_limits_enforce is False
