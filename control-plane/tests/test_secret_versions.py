from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db.base import SessionLocal
from app.db.models import Secret, SecretVersion
from app.services import crypto

BODY = {"slug": "shop", "repo_full_name": "acme/shop", "environments": [
    {"name": "prod", "namespace": "shop", "deployment_name": "shop", "container_name": "shop"}]}


def _setup(client, admin):
    client.post("/api/projects", json=BODY, headers=admin)


def _put(client, admin, value, **extra):
    return client.put("/api/projects/shop/secrets/DB_PASS", json={"value": value, **extra}, headers=admin)


def iso(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def test_each_change_creates_a_version_without_exposing_values(client, admin):
    _setup(client, admin)
    assert _put(client, admin, "one").json()["version"] == 1
    assert _put(client, admin, "two").json()["version"] == 2
    vs = client.get("/api/projects/shop/secrets/DB_PASS/versions", headers=admin).json()
    assert [v["version"] for v in vs] == [2, 1] and vs[0]["current"] and not vs[1]["current"]
    assert vs[0]["created_by"] and "value" not in vs[0] and "one" not in str(vs) and "two" not in str(vs)


def test_restore_makes_new_version_with_old_value(client, admin):
    _setup(client, admin)
    _put(client, admin, "one"); _put(client, admin, "two")
    r = client.post("/api/projects/shop/secrets/DB_PASS/restore", json={"version": 1}, headers=admin)
    assert r.status_code == 200 and r.json()["version"] == 3
    db = SessionLocal()
    row = db.query(Secret).filter_by(key="DB_PASS").one()
    assert crypto.decrypt_value(row.encrypted_value) == "one" and row.version == 3
    hist = db.query(SecretVersion).filter_by(key="DB_PASS").order_by(SecretVersion.version).all()
    assert [h.version for h in hist] == [1, 2, 3] and hist[2].reason == "restore"
    db.close()
    assert client.post("/api/projects/shop/secrets/DB_PASS/restore", json={"version": 9}, headers=admin).status_code == 404


def test_history_is_pruned(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "secret_versions_keep", 3)
    _setup(client, admin)
    for i in range(6):
        _put(client, admin, f"v{i}")
    vs = client.get("/api/projects/shop/secrets/DB_PASS/versions", headers=admin).json()
    assert [v["version"] for v in vs] == [6, 5, 4]


def test_expiry_and_rotation_status(client, admin):
    _setup(client, admin)
    _put(client, admin, "x", expires_at=iso(5))
    st = lambda: client.get("/api/projects/shop/secrets", headers=admin).json()[0]["status"]
    assert st() == "expiring"
    client.put("/api/projects/shop/secrets/DB_PASS/policy", json={"expires_at": iso(60)}, headers=admin)
    assert st() == "ok"
    db = SessionLocal()
    row = db.query(Secret).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit(); db.close()
    assert st() == "expired"
    client.put("/api/projects/shop/secrets/DB_PASS/policy", json={"rotation_days": 30}, headers=admin)
    db = SessionLocal()
    row = db.query(Secret).one(); row.updated_at = datetime.now(timezone.utc) - timedelta(days=31); db.commit(); db.close()
    assert st() == "rotation_due"
    _put(client, admin, "rotated")   # смена значения сбрасывает напоминание, политика сохраняется
    got = client.get("/api/projects/shop/secrets", headers=admin).json()[0]
    assert got["status"] == "ok" and got["rotation_days"] == 30


def test_policy_validation_and_permissions(client, admin, make_token):
    _setup(client, admin)
    assert _put(client, admin, "x", expires_at=iso(-1)).status_code == 422
    _put(client, admin, "x")
    assert client.put("/api/projects/shop/secrets/DB_PASS/policy", json={"rotation_days": 0}, headers=admin).status_code == 422
    assert client.put("/api/projects/shop/secrets/NOPE/policy", json={}, headers=admin).status_code == 404
    dev = make_token("developer")
    assert client.get("/api/projects/shop/secrets/DB_PASS/versions", headers=dev).status_code == 200
    assert client.post("/api/projects/shop/secrets/DB_PASS/restore", json={"version": 1}, headers=dev).status_code == 403
    assert client.put("/api/projects/shop/secrets/DB_PASS/policy", json={}, headers=dev).status_code == 403


def test_delete_removes_history_and_audit_has_no_values(client, admin):
    _setup(client, admin)
    _put(client, admin, "topsecret-value")
    client.delete("/api/projects/shop/secrets/DB_PASS", headers=admin)
    db = SessionLocal()
    assert db.query(SecretVersion).count() == 0
    db.close()
    assert "topsecret-value" not in str(client.get("/api/audit", headers=admin).json())
    assert client.delete("/api/projects/shop", headers=admin).status_code == 200


def test_project_delete_removes_versions(client, admin):
    _setup(client, admin)
    _put(client, admin, "a"); _put(client, admin, "b")
    assert client.delete("/api/projects/shop", headers=admin).status_code == 200
    db = SessionLocal()
    assert db.query(SecretVersion).count() == 0
    db.close()


def test_overview_lists_secret_alerts(client, admin):
    _setup(client, admin)
    _put(client, admin, "x", expires_at=iso(3))
    o = client.get("/api/overview", headers=admin).json()
    assert o["secret_alerts_total"] == 1 and o["secret_alerts"][0]["status"] == "expiring" and o["secret_alerts"][0]["project"] == "shop"
