from app.db.base import SessionLocal
from app.db.models import Environment, Project
from app.services import secrets as svc

BODY = {"slug": "shop", "repo_full_name": "acme/shop", "environments": [
    {"name": "prod", "namespace": "shop-prod", "deployment_name": "shop", "container_name": "shop"},
    {"name": "staging", "namespace": "shop-stg", "deployment_name": "shop", "container_name": "shop"}]}


def _mk(client, admin):
    assert client.post("/api/projects", json=BODY, headers=admin).status_code == 201


def test_environment_value_overrides_common_one(client, admin):
    _mk(client, admin)
    client.put("/api/projects/shop/secrets/DB_URL", json={"value": "common"}, headers=admin)
    client.put("/api/projects/shop/secrets/DB_URL?environment=prod", json={"value": "prod-only"}, headers=admin)
    client.put("/api/projects/shop/secrets/ONLY_STG?environment=staging", json={"value": "s"}, headers=admin)

    prod = {s["key"]: s for s in client.get("/api/projects/shop/secrets?environment=prod", headers=admin).json()}
    stg = {s["key"]: s for s in client.get("/api/projects/shop/secrets?environment=staging", headers=admin).json()}
    assert prod["DB_URL"]["scope"] == "prod" and prod["DB_URL"]["inherited"] is False
    assert stg["DB_URL"]["scope"] == "*" and stg["DB_URL"]["inherited"] is True
    assert "ONLY_STG" not in prod and "ONLY_STG" in stg

    db = SessionLocal(); p = db.query(Project).one()
    assert svc.decrypt_all(db, p, "prod")["DB_URL"] == "prod-only"
    assert svc.decrypt_all(db, p, "staging")["DB_URL"] == "common"
    assert set(svc.decrypt_all(db, p)) == {"DB_URL"}            # без среды — только общие
    db.close()


def test_unfiltered_list_shows_every_scope(client, admin):
    _mk(client, admin)
    client.put("/api/projects/shop/secrets/K", json={"value": "a"}, headers=admin)
    client.put("/api/projects/shop/secrets/K?environment=prod", json={"value": "b"}, headers=admin)
    rows = client.get("/api/projects/shop/secrets", headers=admin).json()
    assert sorted((r["key"], r["scope"]) for r in rows) == [("K", "*"), ("K", "prod")]


def test_scope_validation_and_delete(client, admin):
    _mk(client, admin)
    assert client.put("/api/projects/shop/secrets/K?environment=nope", json={"value": "v"}, headers=admin).status_code == 404
    assert client.get("/api/projects/shop/secrets?environment=nope", headers=admin).status_code == 404
    client.put("/api/projects/shop/secrets/K?environment=prod", json={"value": "v"}, headers=admin)
    assert client.delete("/api/projects/shop/secrets/K", headers=admin).status_code == 404           # общего нет
    assert client.delete("/api/projects/shop/secrets/K?environment=prod", headers=admin).json()["scope"] == "prod"


def test_sync_sends_the_environment_view_to_its_own_namespace(client, admin, fake_k8s):
    core, apps = fake_k8s
    _mk(client, admin)
    client.put("/api/projects/shop/secrets/DB_URL", json={"value": "common"}, headers=admin)
    client.put("/api/projects/shop/secrets/DB_URL?environment=prod", json={"value": "prod-only"}, headers=admin)
    assert client.post("/api/projects/shop/secrets/sync?environment=prod", headers=admin).json()["synced"] == 1
    assert client.post("/api/projects/shop/secrets/sync?environment=staging", headers=admin).json()["synced"] == 1
    assert core.secrets[("shop-prod", "shop-prod-platform-env")].string_data == {"DB_URL": "prod-only"}
    assert core.secrets[("shop-stg", "shop-staging-platform-env")].string_data == {"DB_URL": "common"}


def test_audit_records_scope_but_not_value(client, admin):
    _mk(client, admin)
    client.put("/api/projects/shop/secrets/K?environment=prod", json={"value": "TopSecret123"}, headers=admin)
    ev = client.get("/api/audit?action=secret_set", headers=admin)
    assert ev.json()[0]["detail"]["scope"] == "prod" and "TopSecret123" not in ev.text
