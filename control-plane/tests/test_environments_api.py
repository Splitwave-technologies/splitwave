BODY = {"slug": "shop", "repo_full_name": "acme/shop", "environments": [
    {"name": "prod", "namespace": "shop", "deployment_name": "shop", "container_name": "shop"}]}
STG = {"name": "staging", "namespace": "shop-stg", "deployment_name": "shop", "container_name": "shop", "branch": "develop", "auto_deploy": False}


def test_admin_manages_environments(client, admin):
    client.post("/api/projects", json=BODY, headers=admin)
    r = client.post("/api/projects/shop/environments", json=STG, headers=admin)
    assert r.status_code == 201 and r.json()["branch"] == "develop" and r.json()["auto_deploy"] is False
    assert client.post("/api/projects/shop/environments", json=STG, headers=admin).status_code == 409
    names = [e["name"] for e in client.get("/api/projects/shop/environments", headers=admin).json()]
    assert names == ["prod", "staging"]

    p = client.patch("/api/projects/shop/environments/staging", json={"branch": "release", "auto_deploy": True}, headers=admin)
    assert p.json()["branch"] == "release" and p.json()["auto_deploy"] is True
    assert client.patch("/api/projects/shop/environments/staging", json={}, headers=admin).status_code == 422
    assert client.patch("/api/projects/shop/environments/nope", json={"branch": "x"}, headers=admin).status_code == 404
    assert client.patch("/api/projects/shop/environments/staging", json={"namespace": "Bad_NS"}, headers=admin).status_code == 422


def test_delete_environment_cleans_up_but_keeps_audit(client, admin):
    client.post("/api/projects", json=BODY, headers=admin)
    client.post("/api/projects/shop/environments", json=STG, headers=admin)
    client.put("/api/projects/shop/secrets/K?environment=staging", json={"value": "v"}, headers=admin)
    client.put("/api/projects/shop/secrets/COMMON", json={"value": "v"}, headers=admin)
    assert client.delete("/api/projects/shop/environments/staging", headers=admin).json() == {"deleted": "staging"}
    rows = client.get("/api/projects/shop/secrets", headers=admin).json()
    assert [(r["key"], r["scope"]) for r in rows] == [("COMMON", "*")]      # секрет среды удалён, общий остался
    events = client.get("/api/audit?limit=100", headers=admin).json()
    assert {"env_create", "env_delete"} <= {e["action"] for e in events}


def test_cannot_delete_the_last_environment(client, admin):
    client.post("/api/projects", json=BODY, headers=admin)
    r = client.delete("/api/projects/shop/environments/prod", headers=admin)
    assert r.status_code == 409 and "last environment" in r.json()["detail"]


def test_permissions(client, admin, make_token):
    client.post("/api/projects", json=BODY, headers=admin)
    ops, viewer = make_token("devops"), make_token("viewer")
    assert client.get("/api/projects/shop/environments", headers=viewer).status_code == 200
    assert client.post("/api/projects/shop/environments", json=STG, headers=ops).status_code == 403
    assert client.patch("/api/projects/shop/environments/prod", json={"branch": "x"}, headers=ops).status_code == 403
    assert client.delete("/api/projects/shop/environments/prod", headers=ops).status_code == 403
    assert client.get("/api/projects/nope/environments", headers=viewer).status_code == 404
