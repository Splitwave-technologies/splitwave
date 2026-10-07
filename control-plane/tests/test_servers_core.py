"""Ядро целей «сервер»: проверка параметров контейнера, лицензия, маршрутизация выката/отката/секретов/состояния."""
from types import SimpleNamespace as NS

import pytest

from app import plugins
from app.db.base import SessionLocal
from app.db.models import Environment, Release
from app.routers import webhooks
from app.services import runtime_spec, targets

APP = {"name": "prod", "namespace": "default", "deployment_name": "shop", "container_name": "shop"}
SRV = dict(APP, name="vps", server="vps-1", runtime={"ports": [{"host": 8080, "container": 8000}], "health": {"mode": "http", "port": 8080, "path": "/health"}})


class FakeTarget:
    def __init__(self):
        self.calls = []

    def names(self):
        return ["vps-1"]

    def deploy(self, db, project, env, image):
        self.calls.append(("deploy", env.name, image))

    def sync_secrets(self, db, project, env):
        self.calls.append(("sync", env.name))
        return 3

    def runtime(self, env):
        return {"kind": "server", "server": env.server, "online": True, "containers": [{"name": env.deployment_name, "state": "running"}]}


@pytest.fixture
def licensed(monkeypatch):
    t = FakeTarget()
    monkeypatch.setattr(targets, "current_license", lambda: NS(features=frozenset({"servers"})))
    monkeypatch.setattr(plugins, "SERVER_TARGET", t)
    return t


def mk(client, admin, *envs):
    return client.post("/api/projects", json={"slug": "shop", "repo_full_name": "acme/shop", "environments": list(envs)}, headers=admin)


def test_server_environment_needs_license(client, admin):
    r = mk(client, admin, APP, SRV)
    assert r.status_code == 403 and r.json()["detail"]["feature"] == "servers"


def test_validation_unknown_server_and_exclusive_target(client, admin, licensed):
    assert mk(client, admin, APP).status_code == 201
    post = lambda env: client.post("/api/projects/shop/environments", json=env, headers=admin)
    assert post(dict(SRV, server="nope")).status_code == 422
    assert post(dict(SRV, cluster="edge")).status_code == 422
    r = post(SRV)
    assert r.status_code == 201 and r.json()["server"] == "vps-1" and r.json()["runtime"]["ports"][0]["host"] == 8080
    assert r.json()["runtime"]["restart"] == "unless-stopped"                        # значения по умолчанию подставлены


@pytest.mark.parametrize("spec", [
    {"ports": [{"host": 0, "container": 80}]}, {"ports": [{"host": 70000, "container": 80}]},
    {"volumes": [{"name": "../etc", "path": "/data"}]}, {"volumes": [{"name": "data", "path": "/../etc"}]}, {"volumes": [{"name": "data", "path": "relative"}]},
    {"restart": "sometimes"}, {"memory": "lots"}, {"cpus": 0}, {"health": {"mode": "http"}},
    {"health": {"mode": "http", "port": 9999}, "ports": [{"host": 8080, "container": 80}]}, {"health": {"path": "no-slash"}},
    {"ports": [{"host": 80, "container": 80, "bind": "10.0.0.1"}]}, {"unknown": 1}.__class__({"ports": [{"host": 1, "container": 1, "protocol": "sctp"}]}),
])
def test_runtime_spec_rejects_unsafe_values(spec):
    with pytest.raises(ValueError):
        runtime_spec.validate(spec)


def test_runtime_spec_defaults_and_ok_values():
    v = runtime_spec.validate({"ports": [{"host": 443, "container": 8443, "bind": "127.0.0.1"}], "volumes": [{"name": "data", "path": "/var/data"}], "memory": "512m", "cpus": 1.5})
    assert v["restart"] == "unless-stopped" and v["health"]["mode"] == "running" and v["memory"] == "512m"


def test_build_deploy_rollback_secrets_and_runtime_use_the_server_target(client, admin, licensed, monkeypatch):
    mk(client, admin, APP)
    client.post("/api/projects/shop/environments", json=SRV, headers=admin)
    db = SessionLocal()
    env = db.query(Environment).filter_by(name="vps").one()
    for img, ago in (("reg/shop@sha256:" + "1" * 64, 20), ("reg/shop@sha256:" + "2" * 64, 5)):
        from datetime import datetime, timedelta, timezone
        db.add(Release(environment_id=env.id, status="deployed", image_digest=img, git_revision="r", triggered_by="t", created_at=datetime.now(timezone.utc) - timedelta(minutes=ago)))
    db.commit(); pid, eid = env.project_id, env.id; db.close()
    monkeypatch.setattr(webhooks.deploy, "deploy_image", lambda *a, **k: pytest.fail("kubernetes must not be used for server environments"))
    monkeypatch.setattr("app.services.secrets.sync_to_cluster", lambda *a, **k: pytest.fail("kubernetes secrets must not be used"))
    assert client.post("/api/projects/shop/rollback?environment=vps", headers=admin).status_code == 200
    assert licensed.calls[-1] == ("deploy", "vps", "reg/shop@sha256:" + "1" * 64)
    assert client.post("/api/projects/shop/secrets/sync?environment=vps", headers=admin).json()["synced"] == 3
    rt = client.get("/api/projects/shop/runtime?environment=vps", headers=admin).json()
    assert rt["available"] is True and rt["kind"] == "server" and rt["containers"][0]["state"] == "running"
    monkeypatch.setattr(webhooks.kpack, "trigger_build", lambda *a, **k: False)
    monkeypatch.setattr(webhooks.kpack, "get_status", lambda *a, **k: {"latest_image": "reg/shop@sha256:" + "3" * 64})
    webhooks.run_build_and_deploy(pid, eid, "https://h/a.git", "a" * 40, "t")
    assert licensed.calls[-1] == ("deploy", "vps", "reg/shop@sha256:" + "3" * 64)
    assert SessionLocal().query(Release).filter_by(git_revision="a" * 40).one().status == "deployed"


def test_server_failure_marks_release_failed(client, admin, licensed, monkeypatch):
    mk(client, admin, APP)
    client.post("/api/projects/shop/environments", json=SRV, headers=admin)
    db = SessionLocal(); env = db.query(Environment).filter_by(name="vps").one(); pid, eid = env.project_id, env.id; db.close()
    monkeypatch.setattr(licensed, "deploy", lambda *a: (_ for _ in ()).throw(RuntimeError("server offline")))
    monkeypatch.setattr(webhooks.kpack, "trigger_build", lambda *a, **k: False)
    monkeypatch.setattr(webhooks.kpack, "get_status", lambda *a, **k: {"latest_image": "reg/shop@sha256:" + "4" * 64})
    webhooks.run_build_and_deploy(pid, eid, "https://h/a.git", "b" * 40, "t")
    r = SessionLocal().query(Release).filter_by(git_revision="b" * 40).one()
    assert r.status == "failed" and "server offline" in r.error_message


def test_switching_environment_back_to_kubernetes_clears_server(client, admin, licensed):
    mk(client, admin, APP)
    client.post("/api/projects/shop/environments", json=SRV, headers=admin)
    r = client.patch("/api/projects/shop/environments/vps", json={"server": "local"}, headers=admin)
    assert r.json()["server"] is None and r.json()["runtime"] is None
    assert client.patch("/api/projects/shop/environments/prod", json={"runtime": {"restart": "no"}}, headers=admin).status_code == 422
