import pytest
from cryptography.fernet import Fernet

SECRET = "sup3r-s3cret-value-XYZ"


def test_developer_lists_keys_but_never_values(client, project, make_token):
    ops, dev = make_token("devops"), make_token("developer")
    assert client.put("/api/projects/demo/secrets/DB_PASSWORD", json={"value": SECRET}, headers=ops).status_code == 200

    listing = client.get("/api/projects/demo/secrets", headers=dev)
    assert listing.status_code == 200
    assert [s["key"] for s in listing.json()] == ["DB_PASSWORD"]
    assert SECRET not in listing.text
    assert listing.headers["cache-control"] == "no-store"


def test_developer_cannot_write_or_delete(client, project, make_token):
    dev = make_token("developer")
    assert client.put("/api/projects/demo/secrets/K", json={"value": "v"}, headers=dev).status_code == 403
    assert client.delete("/api/projects/demo/secrets/K", headers=dev).status_code == 403
    assert client.post("/api/projects/demo/secrets/sync", headers=dev).status_code == 403


def test_value_is_encrypted_in_db_and_absent_from_audit(client, project, admin):
    from app.db.base import SessionLocal
    from app.db.models import Secret
    client.put("/api/projects/demo/secrets/API_KEY", json={"value": SECRET}, headers=admin)

    db = SessionLocal()
    row = db.query(Secret).one()
    db.close()
    assert SECRET.encode() not in row.encrypted_value

    audit = client.get("/api/audit?project=demo", headers=admin)
    assert SECRET not in audit.text
    assert any(e["action"] == "secret_set" and e["detail"]["key"] == "API_KEY" for e in audit.json())


def test_upsert_delete_and_validation(client, project, admin):
    assert client.put("/api/projects/demo/secrets/A", json={"value": "1"}, headers=admin).json()["created"] is True
    assert client.put("/api/projects/demo/secrets/A", json={"value": "2"}, headers=admin).json()["created"] is False
    assert client.put("/api/projects/demo/secrets/1BAD", json={"value": "x"}, headers=admin).status_code == 422
    assert client.put("/api/projects/demo/secrets/BIG", json={"value": "x" * 70000}, headers=admin).status_code == 422
    assert client.delete("/api/projects/demo/secrets/A", headers=admin).status_code == 200
    assert client.delete("/api/projects/demo/secrets/A", headers=admin).status_code == 404
    assert client.put("/api/projects/nope/secrets/A", json={"value": "1"}, headers=admin).status_code == 404


def test_sync_creates_k8s_secret_and_wires_deployment(client, project, admin, fake_k8s):
    core, apps = fake_k8s
    client.put("/api/projects/demo/secrets/DB_PASSWORD", json={"value": SECRET}, headers=admin)
    r = client.post("/api/projects/demo/secrets/sync", headers=admin)
    assert r.json() == {"synced": 1, "environment": "prod"}

    body = core.secrets[("apps", "demo-prod-platform-env")]
    assert body.string_data == {"DB_PASSWORD": SECRET}

    name, ns, patch = apps.patches[-1]
    assert (name, ns) == ("demo", "apps")
    tpl = patch["spec"]["template"]
    assert tpl["spec"]["containers"][0]["envFrom"] == [{"secretRef": {"name": "demo-prod-platform-env"}}]
    assert "platform.split-wave.com/secrets-hash" in tpl["metadata"]["annotations"]


def test_sync_updates_existing_secret(client, project, admin, fake_k8s):
    core, apps = fake_k8s
    client.put("/api/projects/demo/secrets/K", json={"value": "one"}, headers=admin)
    client.post("/api/projects/demo/secrets/sync", headers=admin)
    h1 = apps.patches[-1][2]["spec"]["template"]["metadata"]["annotations"]["platform.split-wave.com/secrets-hash"]
    client.put("/api/projects/demo/secrets/K", json={"value": "two"}, headers=admin)
    client.post("/api/projects/demo/secrets/sync", headers=admin)
    h2 = apps.patches[-1][2]["spec"]["template"]["metadata"]["annotations"]["platform.split-wave.com/secrets-hash"]
    assert core.secrets[("apps", "demo-prod-platform-env")].string_data == {"K": "two"}
    assert h1 != h2  # изменение значения перезапускает поды


def test_key_rotation(monkeypatch):
    from app.services import crypto
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setattr(crypto.settings, "secret_encryption_key", old)
    ct = crypto.encrypt_value("v")
    monkeypatch.setattr(crypto.settings, "secret_encryption_key", f"{new},{old}")
    assert crypto.decrypt_value(ct) == "v"           # старый ключ ещё читает
    rotated = crypto.rotate_value(ct)
    monkeypatch.setattr(crypto.settings, "secret_encryption_key", new)
    assert crypto.decrypt_value(rotated) == "v"      # после ротации хватает нового ключа


def test_sync_refuses_to_overwrite_foreign_secret(client, project, admin, fake_k8s):
    from kubernetes import client as k8s
    core, apps = fake_k8s
    core.secrets[("apps", "demo-prod-platform-env")] = k8s.V1Secret(metadata=k8s.V1ObjectMeta(name="demo-prod-platform-env"))
    client.put("/api/projects/demo/secrets/K", json={"value": "v"}, headers=admin)
    import pytest
    from app.db.base import SessionLocal
    from app.db.models import Environment, Project
    from app.services import secrets as svc
    db = SessionLocal()
    p = db.query(Project).one(); e = db.query(Environment).one()
    with pytest.raises(RuntimeError, match="not managed by the platform"):
        svc.sync_to_cluster(db, p, e)
    db.close()
    assert apps.patches == []  # Deployment не тронут
