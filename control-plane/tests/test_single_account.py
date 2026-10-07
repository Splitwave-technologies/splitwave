"""Без платного модуля teams ядро ведёт одну встроенную учётную запись администратора."""
from fastapi.testclient import TestClient

from app import plugins, security
from app.db.base import SessionLocal
from app.db.models import User
from app.main import app

PW = "Str0ng-Passphrase-2026"


def _first(client, admin, **extra):
    return client.post("/api/users", json={"username": "owner", "password": PW, **extra}, headers=admin)


def test_first_account_is_an_administrator_and_a_second_one_needs_the_paid_module(client, admin):
    assert plugins.TEAMS is None
    r = _first(client, admin)
    assert r.status_code == 201 and r.json()["role"] == "admin"
    for role in (None, "admin", "viewer"):
        body = {"username": "second", "password": PW, **({"role": role} if role else {})}
        r = client.post("/api/users", json=body, headers=admin)
        assert r.status_code == 403 and r.json()["detail"]["feature"] == "teams", role


def test_other_roles_are_refused_even_for_the_first_account(client, admin):
    r = _first(client, admin, role="viewer")
    assert r.status_code == 403 and r.json()["detail"]["feature"] == "teams"
    assert client.get("/api/users", headers=admin).json() == []


def test_members_and_deletion_are_paid_features(client, admin):
    uid = _first(client, admin).json()["id"]
    r = client.post("/api/projects", json={"slug": "alpha", "repo_full_name": "acme/alpha", "environments": [
        {"name": "prod", "namespace": "alpha", "deployment_name": "alpha", "container_name": "alpha"}]}, headers=admin)
    assert r.status_code == 201
    assert client.get("/api/projects/alpha/members", headers=admin).json() == []
    assert client.put("/api/projects/alpha/members/owner", json={"role": "developer"}, headers=admin).status_code == 403
    assert client.delete("/api/projects/alpha/members/owner", headers=admin).status_code == 403
    assert client.delete(f"/api/users/{uid}", headers=admin).status_code == 403
    assert client.patch(f"/api/users/{uid}", json={"role": "viewer"}, headers=admin).status_code == 403
    assert client.patch(f"/api/users/{uid}", json={"disabled": True}, headers=admin).status_code == 403
    assert client.patch(f"/api/users/{uid}", json={"reset_totp": True}, headers=admin).status_code == 200


def test_a_user_row_with_another_role_has_no_rights_without_the_module(client, admin):
    """Даже если строку пользователя с ролью вписали в базу вручную, права она без модуля не получает."""
    db = SessionLocal()
    db.add(User(username="intruder", role="devops", password_hash=security.hash_password(PW), must_change_password=False))
    db.commit()
    db.close()
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": "intruder", "password": PW})
    assert r.status_code == 200, r.text
    me = c.get("/api/me").json()
    assert me["role"] == "none" and me["permissions"] == [] and me["memberships"] == []
    assert c.get("/api/projects").status_code == 403
