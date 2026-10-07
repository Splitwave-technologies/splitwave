"""Состояние среды: kpack для buildpacks, релизы для проектов с Dockerfile и сред на серверах (регрессия: вечное «Сборка…» и пустой образ)."""
from datetime import datetime, timedelta, timezone

from app.db.base import SessionLocal
from app.db.models import Environment, Release
from app.services import kpack

ENV = {"name": "prod", "namespace": "apps", "deployment_name": "shop", "container_name": "shop"}


def mk(client, admin, method="dockerfile"):
    r = client.post("/api/projects", json={"slug": "shop", "repo_full_name": "acme/shop", "build_method": method, "environments": [ENV]}, headers=admin)
    assert r.status_code == 201, r.text


def add(status, digest=None, minutes_ago=0, err=None):
    db = SessionLocal()
    env = db.query(Environment).one()
    db.add(Release(environment_id=env.id, status=status, image_digest=digest, git_revision="r", triggered_by="t", error_message=err,
                   created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)))
    db.commit(); db.close()


def status(client, admin):
    return client.get("/api/projects/shop/status", headers=admin).json()


def forbid_kpack(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("kpack не должен вызываться для проекта с Dockerfile")
    monkeypatch.setattr(kpack, "get_status", boom)


def test_dockerfile_project_without_releases_is_unknown_not_building(client, admin, monkeypatch):
    forbid_kpack(monkeypatch); mk(client, admin)
    s = status(client, admin)
    assert s["state"] == "unknown" and s["ready"] is None and s["latest_image"] is None


def test_dockerfile_project_deployed_is_ready_with_image(client, admin, monkeypatch):
    forbid_kpack(monkeypatch); mk(client, admin)
    add("deployed", "reg/shop@sha256:" + "1" * 64, 10); add("deployed", "reg/shop@sha256:" + "2" * 64, 1)
    s = status(client, admin)
    assert s["state"] == "ready" and s["ready"] is True and s["latest_image"].endswith("2" * 64)


def test_dockerfile_project_building_and_failed(client, admin, monkeypatch):
    forbid_kpack(monkeypatch); mk(client, admin)
    add("deployed", "reg/shop@sha256:" + "1" * 64, 10); add("building", None, 1)
    s = status(client, admin)
    assert s["state"] == "building" and s["ready"] is False and s["latest_image"].endswith("1" * 64)     # прежний образ остаётся виден
    add("failed", None, 0, "build failed: exit 7")
    s = status(client, admin)
    assert s["state"] == "failed" and s["ready"] is None and "exit 7" in s["error"] and s["latest_image"].endswith("1" * 64)


def test_list_uses_the_same_logic(client, admin, monkeypatch):
    forbid_kpack(monkeypatch); mk(client, admin)
    add("deployed", "reg/shop@sha256:" + "3" * 64, 1)
    row = client.get("/api/projects", headers=admin).json()[0]
    assert row["state"] == "ready" and row["ready"] is True and row["latest_image"].endswith("3" * 64) and row["error"] is None


def test_buildpacks_project_still_asks_kpack(client, admin, monkeypatch):
    mk(client, admin, "buildpacks")
    monkeypatch.setattr(kpack, "get_status", lambda name, ns: {"ready": False, "latest_image": "reg/x@sha256:" + "4" * 64})
    s = status(client, admin)
    assert s["state"] == "building" and s["ready"] is False and s["latest_image"].endswith("4" * 64)
    monkeypatch.setattr(kpack, "get_status", lambda name, ns: {"ready": True, "latest_image": "reg/x@sha256:" + "5" * 64})
    assert status(client, admin)["state"] == "ready"


def test_list_survives_cluster_errors_for_buildpacks(client, admin, monkeypatch):
    mk(client, admin, "buildpacks")
    def boom(*a, **k):
        raise RuntimeError("cluster down")
    monkeypatch.setattr(kpack, "get_status", boom)
    row = client.get("/api/projects", headers=admin).json()[0]
    assert row["state"] == "unknown" and "cluster down" in row["error"]
