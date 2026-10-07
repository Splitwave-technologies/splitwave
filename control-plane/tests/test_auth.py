def test_health_is_open(client):
    assert client.get("/health").status_code == 200


def test_api_requires_token(client, project):
    assert client.get("/api/projects/demo/releases").status_code == 401
    assert client.get("/api/projects/demo/releases", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_admin_can_create_list_and_revoke_tokens(client, admin):
    r = client.post("/api/tokens", json={"name": "ci-bot", "role": "developer"}, headers=admin)
    assert r.status_code == 201
    token = r.json()["token"]
    assert token.startswith("dsp_")

    listing = client.get("/api/tokens", headers=admin).json()
    assert {t["name"] for t in listing} == {"bootstrap-admin", "ci-bot"}
    assert token not in str(listing)  # сам токен больше нигде не виден

    tid = next(t["id"] for t in listing if t["name"] == "ci-bot")
    assert client.delete(f"/api/tokens/{tid}", headers=admin).status_code == 200
    assert client.get("/api/projects/demo/releases", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_token_stored_only_as_hash(client, admin):
    from app.db.base import SessionLocal
    from app.db.models import ApiToken
    token = client.post("/api/tokens", json={"name": "x-bot", "role": "viewer"}, headers=admin).json()["token"]
    db = SessionLocal()
    row = db.query(ApiToken).filter_by(name="x-bot").one()
    db.close()
    assert token not in (row.token_hash, row.token_prefix) and row.token_hash != token


def test_cannot_revoke_own_token_and_bad_role(client, admin):
    tokens = client.get("/api/tokens", headers=admin).json()
    assert client.delete(f"/api/tokens/{tokens[0]['id']}", headers=admin).status_code == 409
    assert client.post("/api/tokens", json={"name": "bad", "role": "root"}, headers=admin).status_code == 422


def test_roles(client, project, make_token, fake_k8s):
    viewer, dev, ops = make_token("viewer"), make_token("developer"), make_token("devops")
    assert client.get("/api/projects/demo/releases", headers=viewer).status_code == 200
    # viewer не может деплоить, developer не может откатывать, devops может (нет релизов -> 409, но не 403)
    assert client.post("/api/projects/demo/redeploy", headers=viewer).status_code == 403
    assert client.post("/api/projects/demo/rollback", headers=dev).status_code == 403
    assert client.post("/api/projects/demo/rollback", headers=ops).status_code == 409
    assert client.post("/api/tokens", json={"name": "z", "role": "viewer"}, headers=ops).status_code == 403


def test_denied_access_is_audited(client, project, make_token, admin):
    viewer = make_token("viewer")
    client.post("/api/projects/demo/redeploy", headers=viewer)
    events = client.get("/api/audit?action=access_denied", headers=admin).json()
    assert events and events[0]["actor"] == "viewer-tester"
    assert events[0]["detail"]["permission"] == "deploy"


def test_bootstrap_ignores_short_token(monkeypatch):
    from app import auth
    from app.db.base import Base, SessionLocal, engine
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    monkeypatch.setattr(auth.settings, "admin_bootstrap_token", "short")
    db = SessionLocal()
    assert auth.bootstrap_admin(db) is False
    db.close()
