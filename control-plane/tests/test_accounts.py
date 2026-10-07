from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import security
from app.config import settings
from app.db.base import SessionLocal
from app.db.models import User, UserSession
from app.main import app

PW = "Str0ng-Passphrase-2026"
NEW_PW = "An0ther-Str0ng-Passphrase"


def _mk_user(client, admin, username="alice", role="admin", password=PW):
    r = client.post("/api/users", json={"username": username, "role": role, "password": password}, headers=admin)
    assert r.status_code == 201, r.text
    return r.json()


def _login(username="alice", password=PW, totp=None, expect=200):
    c = TestClient(app)
    body = {"username": username, "password": password}
    if totp:
        body["totp"] = totp
    r = c.post("/api/auth/login", json=body)
    assert r.status_code == expect, r.text
    csrf = r.json().get("csrf_token") if r.status_code == 200 else None
    return c, r, {"X-CSRF-Token": csrf} if csrf else {}


def _ready_user(client, admin, username="alice", role="admin"):
    """Пользователь, который уже сменил временный пароль: возвращает (клиент, csrf-заголовки)."""
    _mk_user(client, admin, username, role)
    c, r, h = _login(username)
    assert r.json()["restricted"] == "password_change"
    assert c.post("/api/auth/password", json={"current": PW, "new": NEW_PW}, headers=h).status_code == 200
    return c, h


def _project(client, admin, slug):
    r = client.post("/api/projects", json={"slug": slug, "repo_full_name": f"acme/{slug}", "environments": [
        {"name": "prod", "namespace": slug, "deployment_name": slug, "container_name": slug}]}, headers=admin)
    assert r.status_code == 201


# ───────────── вход, сессии, CSRF ─────────────
def test_first_login_is_restricted_until_password_change(client, admin):
    _mk_user(client, admin)
    c, r, h = _login()
    assert r.json()["restricted"] == "password_change" and r.json()["csrf_token"]
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/api/projects").status_code == 403 and c.get("/api/projects").json()["detail"]["reason"] == "password_change"
    assert c.get("/api/me").json()["restricted"] == "password_change"        # /api/me доступен
    weak = c.post("/api/auth/password", json={"current": PW, "new": "short"}, headers=h)
    assert weak.status_code == 422
    assert c.post("/api/auth/password", json={"current": "wrong-password-xx", "new": NEW_PW}, headers=h).status_code == 403
    assert c.post("/api/auth/password", json={"current": PW, "new": NEW_PW}, headers=h).status_code == 200
    assert c.get("/api/me").json()["restricted"] is None


def test_csrf_is_required_for_cookie_sessions_but_not_for_tokens(client, admin):
    c, h = _ready_user(client, admin, role="admin")
    body = {"slug": "csrf1", "repo_full_name": "acme/csrf1", "environments": [
        {"name": "prod", "namespace": "n", "deployment_name": "d", "container_name": "c"}]}
    assert c.post("/api/projects", json=body).status_code == 403                                   # без заголовка
    assert c.post("/api/projects", json=body, headers={"X-CSRF-Token": "forged"}).status_code == 403
    assert c.get("/api/projects").status_code == 200                                                # чтение без CSRF разрешено
    assert c.post("/api/projects", json=body, headers=h).status_code == 201
    body2 = {**body, "slug": "csrf2", "repo_full_name": "acme/csrf2"}
    assert client.post("/api/projects", json=body2, headers=admin).status_code == 201               # Bearer без CSRF


def test_invalid_credentials_are_indistinguishable_and_audited(client, admin):
    _mk_user(client, admin)
    a = TestClient(app).post("/api/auth/login", json={"username": "alice", "password": "wrong-password-1"})
    b = TestClient(app).post("/api/auth/login", json={"username": "nobody", "password": "wrong-password-1"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()
    ev = client.get("/api/audit?action=login_failed", headers=admin).json()
    assert len(ev) == 2 and PW not in str(ev)


def test_bruteforce_lockout(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failures", 3)
    _mk_user(client, admin)
    for _ in range(3):
        _login(password="wrong-password-1", expect=401)
    r = TestClient(app).post("/api/auth/login", json={"username": "alice", "password": PW})     # даже верный пароль
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_session_lifecycle_expiry_logout_and_disable(client, admin):
    c, h = _ready_user(client, admin, role="admin")
    assert c.get("/api/projects").status_code == 200
    db = SessionLocal()
    sess = db.query(UserSession).order_by(UserSession.created_at.desc()).first()
    sess.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=5)                            # простой дольше лимита
    db.commit(); db.close()
    assert c.get("/api/projects").status_code == 401

    c2, _, h2 = _login(password=NEW_PW)
    assert c2.post("/api/auth/logout", headers=h2).status_code == 200
    assert c2.get("/api/projects").status_code == 401                                              # сессия отозвана

    c3, _, _ = _login(password=NEW_PW)
    uid = next(u["id"] for u in client.get("/api/users", headers=admin).json() if u["username"] == "alice")
    db = SessionLocal()
    db.query(User).filter_by(username="alice").update({"disabled": True})       # отключение учётных записей — функция платного модуля teams
    db.commit()
    db.close()
    assert c3.get("/api/projects").status_code == 401                                              # отключённый — сразу без доступа
    _login(password=NEW_PW, expect=401)


def test_absolute_session_expiry(client, admin):
    c, h = _ready_user(client, admin, role="admin")
    db = SessionLocal()
    for s in db.query(UserSession).all():
        s.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit(); db.close()
    assert c.get("/api/me").status_code == 401


# ───────────── двухфакторная защита ─────────────
def _enable_totp(c, h):
    secret = c.post("/api/auth/totp/setup", headers=h).json()["secret"]
    assert c.post("/api/auth/totp/enable", json={"code": "000000"}, headers=h).status_code == 422
    r = c.post("/api/auth/totp/enable", json={"code": security.totp_code(secret)}, headers=h)
    assert r.status_code == 200 and len(r.json()["recovery_codes"]) == 8
    return secret, r.json()["recovery_codes"]


def test_totp_flow_replay_protection_and_recovery_codes(client, admin):
    c, h = _ready_user(client, admin, role="admin")
    secret, codes = _enable_totp(c, h)
    assert c.post("/api/auth/totp/setup", headers=h).status_code == 409                            # уже включена

    _login(password=NEW_PW, expect=401)                                                            # без кода — нельзя
    assert TestClient(app).post("/api/auth/login", json={"username": "alice", "password": NEW_PW}).json()["detail"] == {"error": "totp_required"}
    _login(password=NEW_PW, totp="123456", expect=401)                                             # неверный код
    fresh = security.totp_code(secret, at=datetime.now(timezone.utc).timestamp() + 30)             # код следующего интервала (ещё не использован)
    _login(password=NEW_PW, totp=fresh, expect=200)
    _login(password=NEW_PW, totp=fresh, expect=401)                                                # тот же код повторно — отказ

    _login(password=NEW_PW, totp=codes[0], expect=200)                                             # код восстановления
    _login(password=NEW_PW, totp=codes[0], expect=401)                                             # одноразовый

    assert c.post("/api/auth/totp/disable", json={"password": "wrong-password-1", "code": codes[1]}, headers=h).status_code == 403
    assert c.post("/api/auth/totp/disable", json={"password": NEW_PW, "code": codes[1]}, headers=h).status_code == 200
    _login(password=NEW_PW, expect=200)                                                            # 2FA снята — вход без кода


def test_secret_is_never_stored_in_clear(client, admin):
    c, h = _ready_user(client, admin, role="admin")
    secret, _ = _enable_totp(c, h)
    db = SessionLocal()
    u = db.query(User).filter_by(username="alice").one()
    assert secret.encode() not in u.totp_secret and all(len(x) == 64 for x in u.recovery_codes)   # шифртекст и SHA-256
    db.close()


def test_mandatory_2fa_restricts_until_enabled(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "require_2fa", True)
    _mk_user(client, admin)
    c, r, h = _login()
    assert c.post("/api/auth/password", json={"current": PW, "new": NEW_PW}, headers=h).status_code == 200
    assert c.get("/api/me").json()["restricted"] == "totp_setup"
    assert c.get("/api/projects").status_code == 403
    _enable_totp(c, h)
    assert c.get("/api/projects").status_code == 200                                               # доступ открылся сразу
    assert c.post("/api/auth/totp/disable", json={"password": NEW_PW, "code": "000000"}, headers=h).status_code == 403


# ───────────── роли в проектах и токены проекта ─────────────
# ───────────── управление пользователями ─────────────
def test_own_sessions_listing_and_revocation(client, admin):
    c, h = _ready_user(client, admin, role="admin")
    c2, _, _ = _login(password=NEW_PW)
    sessions = c.get("/api/auth/sessions").json()
    assert len(sessions) >= 2 and sum(1 for s in sessions if s["current"]) == 1
    other = next(s for s in sessions if not s["current"])
    assert c.delete(f"/api/auth/sessions/{other['id']}", headers=h).status_code == 200
    assert c2.get("/api/me").status_code == 401 or c.get("/api/me").status_code == 200
    assert c.delete("/api/auth/sessions/not-a-uuid", headers=h).status_code == 400


def test_password_change_revokes_other_sessions(client, admin):
    c, h = _ready_user(client, admin, role="admin")
    other, _, _ = _login(password=NEW_PW)
    third = "Yet-Another-Str0ng-Pass"
    assert c.post("/api/auth/password", json={"current": NEW_PW, "new": third}, headers=h).json()["other_sessions_revoked"] >= 1
    assert other.get("/api/me").status_code == 401 and c.get("/api/me").status_code == 200
    assert c.post("/api/auth/password", json={"current": third, "new": third}, headers=h).status_code == 422   # тот же пароль


def test_api_tokens_cannot_use_account_actions(client, admin):
    assert client.post("/api/auth/password", json={"current": "x", "new": "y"}, headers=admin).status_code == 400
    assert client.post("/api/auth/totp/setup", headers=admin).status_code == 400
    me = client.get("/api/me", headers=admin).json()
    assert me["kind"] == "token" and me["csrf_token"] is None and "users:manage" in me["permissions"]


def test_providers_list_is_public_and_empty_by_default(client):
    assert client.get("/api/auth/providers").json() == []


def test_project_scoped_token(client, admin):
    _project(client, admin, "alpha"); _project(client, admin, "beta")
    assert client.post("/api/tokens", json={"name": "bad", "role": "admin", "project": "alpha"}, headers=admin).status_code == 422
    assert client.post("/api/tokens", json={"name": "bad2", "role": "devops", "project": "nope"}, headers=admin).status_code == 404
    tok = client.post("/api/tokens", json={"name": "ci-alpha", "role": "devops", "project": "alpha"}, headers=admin).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/projects/alpha/secrets", headers=h).status_code == 200
    assert client.put("/api/projects/alpha/secrets/K", json={"value": "v"}, headers=h).status_code == 200
    assert client.get("/api/projects/beta/secrets", headers=h).status_code == 403                 # чужой проект
    assert client.get("/api/audit", headers=h).status_code == 403                                  # глобальные права недоступны
    assert client.post("/api/tokens", json={"name": "x", "role": "viewer"}, headers=h).status_code == 403
    assert {r["slug"] for r in client.get("/api/projects", headers=h).json()} == {"alpha"}
    listing = client.get("/api/tokens", headers=admin).json()
    assert next(t for t in listing if t["name"] == "ci-alpha")["project"] == "alpha"
    me = client.get("/api/me", headers=h).json()
    assert me["project_scope"] == "alpha" and me["csrf_token"] is None

    assert client.delete("/api/projects/alpha", headers=admin).status_code == 200                  # проект удалён -> токен отозван
    assert client.get("/api/projects", headers=h).status_code == 401
