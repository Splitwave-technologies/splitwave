"""Маршрутизация по кластеру среды (ядро). Настоящий реестр кластеров — в платном модуле; здесь поставщик подменён."""
from types import SimpleNamespace

import pytest

from app import plugins
from app.db.base import SessionLocal
from app.db.models import Environment, Release
from app.services import clusters as clusters_svc
from app.services import deploy, kubeclient

ENV = {"name": "prod", "namespace": "shop", "deployment_name": "shop", "container_name": "shop"}
EDGE = dict(ENV, name="edge", namespace="shop-edge", cluster="edge-1")


class Provider:
    def __init__(self):
        self.calls = []

    def __call__(self, name):
        self.calls.append(name)
        return object()

    def names(self):
        return ["edge-1"]


@pytest.fixture
def licensed(monkeypatch):
    prov = Provider()
    monkeypatch.setattr(clusters_svc, "current_license", lambda: SimpleNamespace(features=frozenset({"multi_cluster"})))
    monkeypatch.setattr(plugins, "CLUSTER_PROVIDER", prov)
    return prov


def mk_project(client, admin, *envs):
    return client.post("/api/projects", json={"slug": "shop", "repo_full_name": "acme/shop", "environments": list(envs)}, headers=admin)


def test_remote_cluster_requires_license(client, admin):
    r = mk_project(client, admin, ENV, EDGE)
    assert r.status_code == 403 and r.json()["detail"]["feature"] == "multi_cluster"
    assert mk_project(client, admin, ENV).status_code == 201
    assert client.post("/api/projects/shop/environments", json=EDGE, headers=admin).status_code == 403
    assert client.post("/api/projects/shop/environments", json=dict(EDGE, cluster="local", name="l"), headers=admin).status_code == 201


def test_unknown_cluster_rejected_and_view_shows_cluster(client, admin, licensed):
    assert mk_project(client, admin, ENV).status_code == 201
    assert client.post("/api/projects/shop/environments", json=dict(EDGE, cluster="nope"), headers=admin).status_code == 422
    r = client.post("/api/projects/shop/environments", json=EDGE, headers=admin)
    assert r.status_code == 201 and r.json()["cluster"] == "edge-1"
    envs = {e["name"]: e for e in client.get("/api/projects/shop/environments", headers=admin).json()}
    assert envs["edge"]["cluster"] == "edge-1" and envs["prod"]["cluster"] is None
    assert client.patch("/api/projects/shop/environments/edge", json={"cluster": "local"}, headers=admin).json()["cluster"] is None


def test_build_namespace_is_home_for_remote_environments(client, admin, licensed):
    mk_project(client, admin, ENV, EDGE)
    db = SessionLocal()
    envs = {e.name: e for e in db.query(Environment).all()}
    assert envs["prod"].build_namespace == "shop" and envs["edge"].build_namespace == "default" and envs["edge"].namespace == "shop-edge"
    db.close()


def test_rollback_and_runtime_and_secrets_go_to_the_environment_cluster(client, admin, licensed, monkeypatch, fake_k8s):
    core, apps = fake_k8s
    calls = []
    monkeypatch.setattr("app.services.kubeclient.apps_v1_api", lambda cluster=None: (calls.append(("apps", cluster)), apps)[1])
    monkeypatch.setattr("app.services.kubeclient.core_v1_api", lambda cluster=None: (calls.append(("core", cluster)), core)[1])
    monkeypatch.setattr("app.services.deploy.apps_v1_api", lambda cluster=None: (calls.append(("deploy", cluster)), apps)[1])
    mk_project(client, admin, ENV, EDGE)
    db = SessionLocal()
    env = db.query(Environment).filter_by(name="edge").one()
    for img, rev in (("reg/shop@sha256:old", "a1"), ("reg/shop@sha256:new", "b2")):
        db.add(Release(environment_id=env.id, status="deployed", image_digest=img, git_revision=rev, triggered_by="t")); db.commit()
    db.close()
    client.put("/api/projects/shop/secrets/K?environment=edge", json={"value": "v"}, headers=admin)
    calls.clear()
    assert client.post("/api/projects/shop/secrets/sync?environment=edge", headers=admin).status_code == 200
    assert ("core", "edge-1") in calls and ("apps", "edge-1") in calls and ("core", None) not in calls
    calls.clear()
    assert client.post("/api/projects/shop/rollback?environment=edge", headers=admin).status_code == 200
    assert ("deploy", "edge-1") in calls
    assert apps.patches[-1][1] == "shop-edge"
    calls.clear()
    assert client.get("/api/projects/shop/runtime?environment=edge", headers=admin).json()["available"] is True
    assert ("apps", "edge-1") in calls


def test_home_environments_never_touch_the_provider(client, admin, licensed, fake_k8s):
    mk_project(client, admin, ENV)
    client.get("/api/projects/shop/runtime", headers=admin)
    assert licensed.calls == []


def test_unlicensed_or_missing_provider_fails_safely(monkeypatch):
    monkeypatch.setattr(plugins, "CLUSTER_PROVIDER", None)
    with pytest.raises(RuntimeError, match="not licensed or not installed"):
        kubeclient.apps_v1_api("edge-1")
