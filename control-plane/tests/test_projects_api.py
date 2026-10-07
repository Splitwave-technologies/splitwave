BODY = {
    "slug": "shop", "repo_full_name": "acme/shop",
    "environments": [{"name": "prod", "namespace": "shop", "deployment_name": "shop", "container_name": "shop"}],
}


def test_admin_creates_project_and_environment(client, admin):
    r = client.post("/api/projects", json=BODY, headers=admin)
    assert r.status_code == 201 and r.json() == {"slug": "shop", "environments": ["prod"]}
    events = client.get("/api/audit?action=project_create", headers=admin).json()
    assert events[0]["detail"]["slug"] == "shop"


def test_only_admin_manages_projects(client, make_token):
    ops, dev = make_token("devops"), make_token("developer")
    assert client.post("/api/projects", json=BODY, headers=ops).status_code == 403
    assert client.post("/api/projects", json=BODY, headers=dev).status_code == 403
    assert client.delete("/api/projects/shop", headers=ops).status_code == 403


def test_duplicates_and_validation(client, admin):
    assert client.post("/api/projects", json=BODY, headers=admin).status_code == 201
    assert client.post("/api/projects", json=BODY, headers=admin).status_code == 409
    other_repo = {**BODY, "slug": "shop2"}
    assert client.post("/api/projects", json=other_repo, headers=admin).status_code == 409  # тот же репозиторий
    assert client.post("/api/projects", json={**BODY, "slug": "Bad_Slug", "repo_full_name": "a/b"}, headers=admin).status_code == 422
    assert client.post("/api/projects", json={**BODY, "slug": "x", "repo_full_name": "a/b", "environments": []}, headers=admin).status_code == 422
    dup_env = {**BODY, "slug": "y", "repo_full_name": "a/c", "environments": BODY["environments"] * 2}
    assert client.post("/api/projects", json=dup_env, headers=admin).status_code == 422


def test_delete_project_removes_secrets(client, admin):
    from app.db.base import SessionLocal
    from app.db.models import Secret
    client.post("/api/projects", json=BODY, headers=admin)
    client.put("/api/projects/shop/secrets/K", json={"value": "v"}, headers=admin)
    assert client.delete("/api/projects/shop", headers=admin).json() == {"deleted": "shop"}
    db = SessionLocal()
    assert db.query(Secret).count() == 0
    db.close()
    assert client.delete("/api/projects/shop", headers=admin).status_code == 404


def test_delete_project_keeps_audit_trail(client, admin):
    client.post("/api/projects", json=BODY, headers=admin)
    client.put("/api/projects/shop/secrets/K", json={"value": "v"}, headers=admin)
    assert client.delete("/api/projects/shop", headers=admin).status_code == 200
    events = client.get("/api/audit?limit=50", headers=admin).json()
    actions = {e["action"] for e in events}
    assert {"project_create", "secret_set", "project_delete"} <= actions
    orphan = next(e for e in events if e["action"] == "secret_set")
    assert orphan["context"]["project_slug"] == "shop" and orphan["project_id"] is None


def test_redeploy_accepts_exact_revision(client, admin, monkeypatch):
    from app.routers import webhooks
    calls = []
    monkeypatch.setattr(webhooks, "run_build_and_deploy", lambda *a, **k: calls.append(a))
    client.post("/api/projects", json=BODY, headers=admin)

    r = client.post("/api/projects/shop/redeploy?revision=e7533e4a1b2c", headers=admin)
    assert r.status_code == 200 and r.json()["revision"] == "e7533e4a1b2c"
    assert calls[-1][3] == "e7533e4a1b2c"  # в сборку уходит SHA, а не имя ветки

    client.post("/api/projects/shop/redeploy", headers=admin)
    assert calls[-1][3] == "main"

    assert client.post("/api/projects/shop/redeploy?revision=not-a-sha", headers=admin).status_code == 422
