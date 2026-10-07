"""Dockerfile, который платформа хранит у проекта: передача в kaniko, создание/правка через API, мастер."""
from types import SimpleNamespace as NS

import pytest

from app.db.base import SessionLocal
from app.db.models import AuditEvent, Project
from app.services import kaniko

DF = "FROM node:20-alpine\nWORKDIR /app\nCOPY . .\nCMD [\"node\", \"x.js\"]\n"
ENV = {"name": "prod", "namespace": "apps", "deployment_name": "shop", "container_name": "shop"}


def proj(**kw):
    base = dict(slug="shop", sub_path="", dockerfile_path="Dockerfile", registry_prefix="reg/acme", dockerfile_content=None)
    base.update(kw)
    return NS(**base)


# ---------- kaniko ----------

def test_job_uses_platform_dockerfile_from_env_not_from_the_script():
    job = kaniko.job_body(proj(dockerfile_content=DF, sub_path="svc"), "shop", "ns", "https://git/x.git", "a" * 40, "j1")
    spec = job["spec"]["template"]["spec"]
    init, kan = spec["initContainers"][0], spec["containers"][0]
    env = {e["name"]: e.get("value") for e in init["env"]}
    assert env["DSP_DOCKERFILE"] == DF and env["DSP_SUB"] == "svc"
    assert "--dockerfile=Dockerfile.platform" in kan["args"] and "--context-sub-path=svc" in kan["args"]
    script = init["command"][2]
    assert 'printf "%s\\n" "$DSP_DOCKERFILE"' in script and "Dockerfile.platform" in script
    assert "node:20-alpine" not in script and DF not in script                        # содержимое не подставляется в команду


def test_hostile_dockerfile_text_stays_data():
    evil = "FROM alpine\nRUN echo $(id) `id` '\"; EVIL_MARKER_123 #\n"
    job = kaniko.job_body(proj(dockerfile_content=evil), "shop", "ns", "https://git/x.git", "a" * 40, "j1")
    init = job["spec"]["template"]["spec"]["initContainers"][0]
    assert {e["name"]: e.get("value") for e in init["env"]}["DSP_DOCKERFILE"] == evil and "EVIL_MARKER_123" not in init["command"][2]


def test_job_without_platform_dockerfile_is_unchanged():
    job = kaniko.job_body(proj(dockerfile_path="docker/Dockerfile"), "shop", "ns", "https://git/x.git", "a" * 40, "j1")
    spec = job["spec"]["template"]["spec"]
    assert "--dockerfile=docker/Dockerfile" in spec["containers"][0]["args"]
    assert "DSP_DOCKERFILE" not in {e["name"] for e in spec["initContainers"][0]["env"]}


# ---------- API проектов ----------

def create(client, admin, content=DF, method="dockerfile", slug="shop"):
    return client.post("/api/projects", json={"slug": slug, "repo_full_name": f"acme/{slug}", "build_method": method, "dockerfile_content": content,
                                              "environments": [ENV]}, headers=admin)


def test_create_stores_content_and_reports_it(client, admin):
    assert create(client, admin).status_code == 201
    assert client.get("/api/projects/shop/dockerfile", headers=admin).json() == {"content": DF}
    assert client.get("/api/projects/shop/source", headers=admin).json()["dockerfile_generated"] is True


def test_content_requires_dockerfile_method_and_valid_text(client, admin):
    assert create(client, admin, method="buildpacks").status_code == 422
    assert create(client, admin, content="RUN echo hi").status_code == 422           # нет FROM
    assert create(client, admin, content="FROM a\x00b").status_code == 422
    assert create(client, admin, content="FROM x\n" + "A" * 17000).status_code == 422
    assert SessionLocal().query(Project).count() == 0


def test_empty_content_means_repository_dockerfile(client, admin):
    assert create(client, admin, content=None).status_code == 201 and create(client, admin, content="  ", slug="b").status_code == 201
    assert client.get("/api/projects/shop/dockerfile", headers=admin).json() == {"content": None}
    assert client.get("/api/projects/shop/source", headers=admin).json()["dockerfile_generated"] is False


def test_patch_sets_changes_and_clears_content(client, admin):
    create(client, admin, content=None)
    r = client.patch("/api/projects/shop", json={"dockerfile_content": DF + "# edit\n"}, headers=admin)
    assert r.status_code == 200 and r.json()["dockerfile_generated"] is True
    assert client.get("/api/projects/shop/dockerfile", headers=admin).json()["content"].endswith("# edit\n")
    assert client.patch("/api/projects/shop", json={"dockerfile_content": "nonsense"}, headers=admin).status_code == 422
    assert client.patch("/api/projects/shop", json={"build_method": "buildpacks", "dockerfile_content": DF}, headers=admin).status_code == 422
    r = client.patch("/api/projects/shop", json={"dockerfile_content": ""}, headers=admin)
    assert r.status_code == 200 and r.json()["dockerfile_generated"] is False
    assert client.get("/api/projects/shop/dockerfile", headers=admin).json() == {"content": None}


def test_dockerfile_is_not_written_to_the_audit_log_and_needs_manage(client, admin, make_token):
    create(client, admin, content=None)
    client.patch("/api/projects/shop", json={"dockerfile_content": DF}, headers=admin)
    db = SessionLocal()
    ev = [e for e in db.query(AuditEvent).all() if e.action == "project_update"][-1]
    assert ev.detail["dockerfile_content"] == "<dockerfile>" and "node:20" not in str(ev.detail)
    db.close()
    dev = make_token("developer")
    assert client.get("/api/projects/shop/dockerfile", headers=dev).status_code == 403
    assert client.patch("/api/projects/shop", json={"dockerfile_content": DF}, headers=dev).status_code == 403


# ---------- мастер ----------

def test_wizard_regenerate_endpoint(client, admin, make_token):
    body = {"language": "node", "facts": {"package_manager": "npm", "has_start": True}, "params": {"port": 4100, "start_command": "node srv.js"}}
    r = client.post("/api/onboarding/dockerfile", json=body, headers=admin)
    assert r.status_code == 200 and "EXPOSE 4100" in r.json()["content"] and r.json()["params"]["port"] == 4100
    assert client.post("/api/onboarding/dockerfile", json=dict(body, params={"port": 0}), headers=admin).status_code == 422
    assert client.post("/api/onboarding/dockerfile", json=dict(body, language="cobol"), headers=admin).status_code == 422
    assert client.post("/api/onboarding/dockerfile", json=dict(body, facts={"package_manager": ["x"]}), headers=admin).status_code == 422   # мусор — 422, не 500
    assert client.post("/api/onboarding/dockerfile", json=dict(body, facts={"big": "x" * 9000}), headers=admin).status_code == 422
    assert client.post("/api/onboarding/dockerfile", json=body, headers=make_token("developer")).status_code == 403


def test_wizard_create_stores_the_edited_dockerfile(client, admin, monkeypatch):
    from app.routers import onboarding
    from app.services import provision
    monkeypatch.setattr(provision, "apply", lambda docs, cluster=None: [{"kind": d["kind"], "name": d["metadata"]["name"], "result": "created"} for d in docs])
    monkeypatch.setattr(onboarding.deps, "schedule_redeploy", lambda *a, **k: {"accepted": True})
    body = {"slug": "gen", "repo_full_name": "acme/gen", "build_method": "dockerfile", "port": 3000, "dockerfile_content": DF + "# моя правка\n"}
    r = client.post("/api/onboarding/create", json=body, headers=admin)
    assert r.status_code == 201, r.text
    assert client.get("/api/projects/gen/dockerfile", headers=admin).json()["content"].endswith("# моя правка\n")
    bad = client.post("/api/onboarding/create", json=dict(body, slug="gen2", repo_full_name="acme/gen2", dockerfile_content="oops"), headers=admin)
    assert bad.status_code == 422 and client.get("/api/projects/gen2/dockerfile", headers=admin).status_code == 404
    bad = client.post("/api/onboarding/create", json=dict(body, slug="gen3", repo_full_name="acme/gen3", build_method="buildpacks"), headers=admin)
    assert bad.status_code == 422
