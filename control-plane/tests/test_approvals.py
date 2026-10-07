import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.base import SessionLocal
from app.db.models import Approval, Environment, Release
from app.main import app
from app.routers import webhooks

BODY = {"slug": "shop", "repo_full_name": "acme/shop", "environments": [
    {"name": "prod", "namespace": "shop", "deployment_name": "shop", "container_name": "shop", "require_approval": True},
    {"name": "dev", "namespace": "shop-dev", "deployment_name": "shop", "container_name": "shop", "branch": "develop"}]}


@pytest.fixture
def setup(client, admin, monkeypatch):
    """Проект со средой prod (нужно согласование) и dev (не нужно); запуск сборки подменён на запись вызовов."""
    calls = []
    monkeypatch.setattr(webhooks, "run_build_and_deploy", lambda *a, **k: calls.append(a))
    assert client.post("/api/projects", json=BODY, headers=admin).status_code == 201
    return calls


def _release(env_name="prod", image="reg/shop@sha256:aaa", status="deployed", ago=0):
    db = SessionLocal()
    env = db.query(Environment).filter_by(name=env_name).one()
    r = Release(environment_id=env.id, status=status, image_digest=image, git_revision="abc", triggered_by="api",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=ago))
    db.add(r); db.commit(); rid = str(r.id); db.close()
    return rid


def _bearer(client, admin, role, project=None, name=None):
    data = {"name": name or f"{role}-{uuid.uuid4().hex[:4]}", "role": role}
    if project:
        data["project"] = project
    return {"Authorization": f"Bearer {client.post('/api/tokens', json=data, headers=admin).json()['token']}"}


def test_redeploy_on_protected_environment_creates_a_request_not_a_deploy(client, admin, setup):
    dev = _bearer(client, admin, "developer")
    r = client.post("/api/projects/shop/redeploy?environment=prod&revision=abc1234", headers=dev)
    assert r.status_code == 202 and r.json()["requires_approval"] and r.json()["status"] == "pending"
    assert setup == []                                                      # сборка не запущена
    assert client.post("/api/projects/shop/redeploy?environment=dev", headers=dev).status_code == 200   # среда без согласования — сразу
    assert len(setup) == 1
    pend = client.get("/api/approvals?status=pending", headers=admin).json()
    assert len(pend) == 1 and pend[0]["params"]["revision"] == "abc1234" and pend[0]["requested_by"].startswith("developer-")


def test_four_eyes_another_person_approves_and_it_executes(client, admin, setup):
    dev, ops = _bearer(client, admin, "developer"), _bearer(client, admin, "devops")
    aid = client.post("/api/projects/shop/redeploy?revision=abc1234", headers=dev).json()["approval_id"]
    assert client.post(f"/api/approvals/{aid}/approve", json={}, headers=dev).status_code == 403          # разработчик — нет права отката
    r = client.post(f"/api/approvals/{aid}/approve", json={"note": "ok, release window"}, headers=ops)
    assert r.status_code == 200 and r.json()["status"] == "executed" and r.json()["decided_by"].startswith("devops-")
    assert len(setup) == 1 and setup[0][3] == "abc1234" and "approved by devops-" in setup[0][4]          # ревизия и метка «кем согласовано»
    assert client.post(f"/api/approvals/{aid}/approve", json={}, headers=ops).status_code == 409          # повторно нельзя
    actions = {e["action"] for e in client.get("/api/audit?limit=100", headers=admin).json()}
    assert {"approval_requested", "approval_approved", "approval_executed", "redeploy_requested"} <= actions


def test_requester_cannot_approve_own_request(client, admin, setup):
    ops = _bearer(client, admin, "devops")
    aid = client.post("/api/projects/shop/redeploy?revision=abc1234", headers=ops).json()["approval_id"]
    r = client.post(f"/api/approvals/{aid}/approve", json={}, headers=ops)
    assert r.status_code == 403 and "four-eyes" in r.json()["detail"]
    other = _bearer(client, admin, "devops")
    assert client.post(f"/api/approvals/{aid}/approve", json={}, headers=other).status_code == 200


def test_deny_and_cancel(client, admin, setup):
    dev, ops = _bearer(client, admin, "developer"), _bearer(client, admin, "devops")
    a1 = client.post("/api/projects/shop/redeploy?revision=aaa1111", headers=dev).json()["approval_id"]
    d = client.post(f"/api/approvals/{a1}/deny", json={"note": "not today"}, headers=ops).json()
    assert d["status"] == "denied" and d["note"] == "not today" and setup == []
    a2 = client.post("/api/projects/shop/redeploy?revision=bbb2222", headers=dev).json()["approval_id"]
    assert client.delete(f"/api/approvals/{a2}", headers=ops).status_code == 403                          # чужую заявку отзывает только автор/админ
    assert client.delete(f"/api/approvals/{a2}", headers=dev).json()["status"] == "cancelled"
    assert client.post(f"/api/approvals/{a2}/approve", json={}, headers=ops).status_code == 409


def test_expired_request_cannot_be_approved(client, admin, setup):
    dev, ops = _bearer(client, admin, "developer"), _bearer(client, admin, "devops")
    aid = client.post("/api/projects/shop/redeploy?revision=aaa1111", headers=dev).json()["approval_id"]
    db = SessionLocal()
    a = db.get(Approval, uuid.UUID(aid)); a.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1); db.commit(); db.close()
    r = client.post(f"/api/approvals/{aid}/approve", json={}, headers=ops)
    assert r.status_code == 409 and "expired" in r.json()["detail"] and setup == []


def test_rollback_needs_approval_and_uses_the_snapshot(client, admin, setup, fake_k8s):
    core, apps = fake_k8s
    old, new = _release(image="reg/shop@sha256:old", ago=10), _release(image="reg/shop@sha256:new", ago=1)
    ops1, ops2 = _bearer(client, admin, "devops"), _bearer(client, admin, "devops")
    r = client.post("/api/projects/shop/rollback?environment=prod", headers=ops1)
    assert r.status_code == 202
    aid = r.json()["approval_id"]
    assert client.get(f"/api/approvals/{aid}", headers=admin).json()["params"]["image"] == "reg/shop@sha256:old"   # видно, что именно откатим
    assert apps.patches == []                                                # до согласования кластер не тронут
    out = client.post(f"/api/approvals/{aid}/approve", json={}, headers=ops2)
    assert out.json()["status"] == "executed"
    assert apps.patches[-1][2]["spec"]["template"]["spec"]["containers"][0]["image"] == "reg/shop@sha256:old"
    assert "approval" in next(e for e in client.get("/api/audit?action=rollback", headers=admin).json())["detail"]


def test_break_glass_is_admin_only_needs_a_reason_and_is_audited(client, admin, setup):
    ops = _bearer(client, admin, "devops")
    assert client.post("/api/projects/shop/redeploy?revision=abc1234&break_glass=true&reason=urgent+hotfix+for+outage", headers=ops).status_code == 403
    assert client.post("/api/projects/shop/redeploy?revision=abc1234&break_glass=true", headers=admin).status_code == 422
    assert client.post("/api/projects/shop/redeploy?revision=abc1234&break_glass=true&reason=short", headers=admin).status_code == 422
    r = client.post("/api/projects/shop/redeploy?revision=abc1234&break_glass=true&reason=urgent+hotfix+for+the+outage", headers=admin)
    assert r.status_code == 200 and len(setup) == 1
    ev = client.get("/api/audit?action=break_glass", headers=admin).json()
    assert ev and ev[0]["detail"]["reason"] == "urgent hotfix for the outage" and ev[0]["detail"]["action"] == "redeploy"


def test_webhook_on_protected_environment_creates_an_approval(client, admin, setup):
    import hashlib, hmac, json
    body = json.dumps({"ref": "refs/heads/main", "after": "f00dbabe", "repository": {"full_name": "acme/shop", "clone_url": "https://github.com/acme/shop.git"}}).encode()
    sig = "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    r = client.post("/webhook/github", content=body, headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    assert r.status_code == 200 and r.json()["requires_approval"] is True and setup == []
    ops = _bearer(client, admin, "devops")
    aid = r.json()["approval_id"]
    assert client.get(f"/api/approvals/{aid}", headers=admin).json()["requested_by"] == "webhook"
    assert client.post(f"/api/approvals/{aid}/approve", json={}, headers=ops).json()["status"] == "executed"
    assert setup[0][2] == "https://github.com/acme/shop.git" and setup[0][3] == "f00dbabe"


def test_project_scoped_visibility_and_rights(client, admin, setup):
    client.post("/api/projects", json={"slug": "other", "repo_full_name": "acme/other", "environments": [
        {"name": "prod", "namespace": "o", "deployment_name": "o", "container_name": "o", "require_approval": True}]}, headers=admin)
    ops_shop = _bearer(client, admin, "devops", project="shop")
    dev = _bearer(client, admin, "developer")
    aid = client.post("/api/projects/shop/redeploy?revision=abc1234", headers=dev).json()["approval_id"]
    oid = client.post("/api/projects/other/redeploy?revision=abc1234", headers=dev).json()["approval_id"]
    assert {a["project"] for a in client.get("/api/approvals?status=pending", headers=ops_shop).json()} == {"shop"}   # видит только свой проект
    assert client.post(f"/api/approvals/{oid}/approve", json={}, headers=ops_shop).status_code == 404
    assert client.post(f"/api/approvals/{aid}/approve", json={}, headers=ops_shop).status_code == 200                  # в своём проекте — может


def test_can_decide_flag_and_overview_counter(client, admin, setup):
    dev, ops = _bearer(client, admin, "developer"), _bearer(client, admin, "devops")
    client.post("/api/projects/shop/redeploy?revision=abc1234", headers=dev)
    assert client.get("/api/approvals", headers=ops).json()[0]["can_decide"] is True
    assert client.get("/api/approvals", headers=dev).json()[0]["can_decide"] is False
    assert client.get("/api/overview", headers=admin).json()["totals"]["pending_approvals"] == 1


def test_environment_flag_is_managed_through_the_api(client, admin, setup):
    envs = {e["name"]: e for e in client.get("/api/projects/shop/environments", headers=admin).json()}
    assert envs["prod"]["require_approval"] is True and envs["dev"]["require_approval"] is False
    r = client.patch("/api/projects/shop/environments/dev", json={"require_approval": True}, headers=admin)
    assert r.json()["require_approval"] is True
    dev = _bearer(client, admin, "developer")
    assert client.post("/api/projects/shop/redeploy?environment=dev", headers=dev).status_code == 202


def test_webhook_handles_every_environment_tracking_the_branch(client, admin, setup):
    import hashlib, hmac, json
    client.patch("/api/projects/shop/environments/dev", json={"branch": "main"}, headers=admin)      # dev и prod следят за main
    body = json.dumps({"ref": "refs/heads/main", "after": "cafe1234", "repository": {"full_name": "acme/shop", "clone_url": "https://github.com/acme/shop.git"}}).encode()
    sig = "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    r = client.post("/webhook/github", content=body, headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": sig, "Content-Type": "application/json"}).json()
    assert r["environments"] == ["dev"] and [a["environment"] for a in r["approvals"]] == ["prod"]   # dev сразу, prod — заявка
    assert len(setup) == 1
