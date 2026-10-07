def test_me_returns_role_and_permissions(client, admin, make_token):
    r = client.get("/api/me", headers=admin).json()
    assert r["role"] == "admin" and "tokens:manage" in r["permissions"] and "projects:manage" in r["permissions"]
    dev = client.get("/api/me", headers=make_token("developer")).json()
    assert dev["role"] == "developer" and "deploy" in dev["permissions"] and "secrets:write" not in dev["permissions"]
    viewer = client.get("/api/me", headers=make_token("viewer")).json()
    assert viewer["permissions"] == ["read"]


def test_me_requires_a_token(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_security_headers_and_csp_on_ui(client):
    r = client.get("/ui/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp and "'unsafe-inline'" not in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/health").headers["x-content-type-options"] == "nosniff"


def test_api_responses_are_not_cached(client, admin):
    assert client.get("/api/me", headers=admin).headers["cache-control"] == "no-store"


def test_root_redirects_to_ui(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/ui/"


def test_project_list_survives_cluster_errors(client, admin, monkeypatch):
    from app.routers import projects
    client.post("/api/projects", json={"slug": "shop", "repo_full_name": "acme/shop",
                "environments": [{"name": "prod", "namespace": "shop", "deployment_name": "shop", "container_name": "shop"}]}, headers=admin)
    def boom(*a, **k):
        raise RuntimeError("forbidden")
    monkeypatch.setattr(projects.kpack, "get_status", boom)
    rows = client.get("/api/projects", headers=admin).json()
    assert rows[0]["slug"] == "shop" and rows[0]["ready"] is None and "RuntimeError" in rows[0]["error"]
