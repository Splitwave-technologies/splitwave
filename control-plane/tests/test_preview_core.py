"""Ядро временных сред: отдельное имя образа kpack, хук pull_request, общее удаление среды."""
import hashlib
import hmac
import json

from app import plugins
from app.db.base import SessionLocal
from app.db.models import AuditEvent, Environment, Project, Secret
from app.routers import webhooks
from app.services import environments as envs_svc

BODY = {"slug": "shop", "repo_full_name": "acme/shop", "environments": [
    {"name": "prod", "namespace": "shop", "deployment_name": "shop", "container_name": "shop"}]}


def _post(client, event, payload):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"whsec", raw, hashlib.sha256).hexdigest()
    return client.post("/webhook/github", content=raw, headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": event, "Content-Type": "application/json"})


def test_pull_request_without_module_is_skipped(client, admin):
    client.post("/api/projects", json=BODY, headers=admin)
    r = _post(client, "pull_request", {"action": "opened"})
    assert r.status_code == 200 and r.json()["skipped"] is True


def test_pull_request_is_handed_to_the_module(client, admin, monkeypatch):
    seen = {}
    monkeypatch.setattr(plugins, "PULL_REQUEST_HANDLER", lambda payload, bg: seen.update(payload) or {"accepted": True})
    r = _post(client, "pull_request", {"action": "opened", "number": 7})
    assert r.json() == {"accepted": True} and seen["number"] == 7
    bad = client.post("/webhook/github", content=b"{}", headers={"X-Hub-Signature-256": "sha256=00", "X-GitHub-Event": "pull_request"})
    assert bad.status_code == 401                                    # подпись проверяется до передачи модулю


def test_preview_environment_uses_its_own_kpack_image_name(client, admin):
    client.post("/api/projects", json=BODY, headers=admin)
    db = SessionLocal()
    p = db.query(Project).one()
    db.add(Environment(project_id=p.id, name="pr-12", namespace="shop", deployment_name="shop-pr-12", container_name="shop", preview_of="12"))
    db.commit()
    envs = {e.name: e for e in db.query(Environment).all()}
    assert envs["prod"].image_name == "shop" and envs["pr-12"].image_name == "shop-pr-12"
    db.close()
    listed = {e["name"]: e for e in client.get("/api/projects/shop/environments", headers=admin).json()}
    assert listed["pr-12"]["preview_of"] == "12" and listed["prod"]["preview_of"] is None


def test_remove_environment_keeps_audit_and_drops_secrets(client, admin):
    client.post("/api/projects", json=dict(BODY, environments=BODY["environments"] + [
        {"name": "pr-3", "namespace": "shop", "deployment_name": "shop-pr-3", "container_name": "shop"}]), headers=admin)
    client.put("/api/projects/shop/secrets/K?environment=pr-3", json={"value": "v"}, headers=admin)
    db = SessionLocal()
    p = db.query(Project).one()
    env = db.query(Environment).filter_by(name="pr-3").one()
    from app.services import audit
    audit.log_event(db, "tester", "deploy", p.id, env.id, {"image": "x"})
    envs_svc.remove_environment(db, p, env)
    assert db.query(Environment).filter_by(name="pr-3").count() == 0 and db.query(Secret).filter_by(scope="pr-3").count() == 0
    ev = [e for e in db.query(AuditEvent).all() if (e.context or {}).get("environment_name") == "pr-3"]
    assert ev and all(e.environment_id is None for e in ev)
    db.close()
    assert client.get("/api/audit/verify", headers=admin).json()["ok"] is True
