"""Мастер подключения: API разбора репозитория и создания проекта (Git-провайдер и кластер подменены)."""
import json
from types import SimpleNamespace as NS

import pytest
import yaml
from kubernetes import client as k8s

from app import plugins
from app.config import settings
from app.db.base import SessionLocal
from app.db.models import Environment, Project
from app.routers import onboarding
from app.services import kubeclient, provision, repoinspect, targets

FILES = {"Dockerfile": "FROM python:3.12\nEXPOSE 9000\n", "requirements.txt": "fastapi\n", ".env.example": "DB_URL=x\nSECRET_KEY=y\n"}


class FakeRepo:
    def __init__(self, files=None, private=False):
        self.files, self.private = files or FILES, private

    def __call__(self, ref, token):
        self.last_token = token
        return self

    def repo_info(self):
        return {"default_branch": "main", "private": self.private}

    def head_sha(self, branch):
        return "a" * 40 if branch == "main" else ""

    def list_dir(self, path, ref):
        prefix = path + "/" if path else ""
        out = {}
        for p in self.files:
            if p.startswith(prefix):
                rest = p[len(prefix):]
                out[rest.split("/")[0]] = "/" in rest
        return list(out.items())

    def read_file(self, path, ref):
        return self.files.get(path)


@pytest.fixture
def repo(monkeypatch):
    r = FakeRepo()
    monkeypatch.setitem(repoinspect.CLIENTS, "github", r)
    return r


class FakeKube:
    def __init__(self):
        self.created, self.fail, self.allowed = [], {}, True

    def _do(self, kind, name):
        self.created.append((kind, name))
        if kind in self.fail:
            from kubernetes.client.exceptions import ApiException
            raise ApiException(status=self.fail[kind])

    def create_namespace(self, body): self._do("Namespace", body["metadata"]["name"])
    def create_namespaced_role_binding(self, ns, body): self._do("RoleBinding", body["metadata"]["name"])
    def create_namespaced_deployment(self, ns, body): self._do("Deployment", body["metadata"]["name"])
    def create_namespaced_service(self, ns, body): self._do("Service", body["metadata"]["name"])
    def create_namespaced_ingress(self, ns, body): self._do("Ingress", body["metadata"]["name"])

    def create_self_subject_access_review(self, body):
        return k8s.V1SelfSubjectAccessReview(spec=body.spec, status=k8s.V1SubjectAccessReviewStatus(allowed=self.allowed))


@pytest.fixture
def kube(monkeypatch):
    f = FakeKube()
    for fn in ("core_v1_api", "apps_v1_api", "rbac_v1_api", "networking_v1_api", "authorization_v1_api"):
        monkeypatch.setattr(kubeclient, fn, lambda cluster=None, _f=f: _f)
    monkeypatch.setattr(settings, "app_pull_secret", "")
    return f


@pytest.fixture
def deploys(monkeypatch):
    calls = []
    monkeypatch.setattr(onboarding.deps, "schedule_redeploy", lambda bg, db, project, env, rev, actor, **kw: (calls.append((project.slug, env.name, rev, actor)), {"accepted": True})[1])
    return calls


def inspect(client, admin, **kw):
    return client.post("/api/onboarding/inspect", json=dict({"url": "https://github.com/acme/My-Shop"}, **kw), headers=admin)


CREATE = {"slug": "my-shop", "repo_full_name": "acme/My-Shop", "build_method": "dockerfile", "port": 9000, "head_sha": "a" * 40}


def create(client, admin, **kw):
    return client.post("/api/onboarding/create", json=dict(CREATE, **kw), headers=admin)


# ---------- inspect ----------

def test_inspect_returns_proposal_and_capabilities(client, admin, repo, kube):
    r = inspect(client, admin)
    assert r.status_code == 200, r.text
    p, caps = r.json()["proposal"], r.json()["capabilities"]
    assert p["slug"] == "my-shop" and p["build_method"] == "dockerfile" and p["port"] == 9000 and p["head_sha"] == "a" * 40
    assert p["env_hints"] == ["DB_URL", "SECRET_KEY"] and p["existing_project"] is None
    assert caps["clusters"] == ["local"] and caps["servers"] == [] and caps["provision"]["local"] == {"allowed": True, "missing": []}


def test_inspect_forwards_token_and_never_echoes_it(client, admin, repo, kube):
    r = inspect(client, admin, token="ghp_secret_token")
    assert repo.last_token == "ghp_secret_token" and "ghp_secret_token" not in r.text


def test_inspect_reports_existing_project_and_free_slug(client, admin, repo, kube, project):
    db = SessionLocal()
    db.add(Project(slug="my-shop", repo_full_name="other/thing"))
    db.commit(); db.close()
    p = inspect(client, admin).json()["proposal"]
    assert p["existing_project"] is None and p["slug"] == "my-shop-2"           # имя занято другим проектом → предложено свободное
    assert client.post("/api/onboarding/inspect", json={"url": "https://github.com/acme/demo"}, headers=admin).json()["proposal"]["existing_project"] == "demo"


def test_inspect_errors_are_explained(client, admin, repo, kube, monkeypatch):
    assert client.post("/api/onboarding/inspect", json={"url": "https://github.com/onlyowner"}, headers=admin).status_code == 422
    repo.files = {}
    r = inspect(client, admin)
    assert r.status_code == 422 and "пуст" in r.json()["detail"]


def test_inspect_needs_projects_manage(client, admin, repo, kube, make_token):
    assert inspect(client, make_token("developer")).status_code == 403
    assert client.post("/api/onboarding/inspect", json={"url": "x"}).status_code in (401, 403)


def test_inspect_is_rate_limited(client, admin, repo, kube, monkeypatch):
    monkeypatch.setattr(onboarding, "RATE_PER_MINUTE", 3)
    onboarding._recent.clear()
    codes = [inspect(client, admin).status_code for _ in range(5)]
    assert codes[:3] == [200] * 3 and codes[3:] == [429, 429]


@pytest.fixture
def live_server(monkeypatch):
    """Настоящий uvicorn: общий цикл событий (TestClient создаёт цикл на каждый запрос и блокировку цикла не ловит)."""
    import socket
    import threading
    import time
    import uvicorn
    import app.main as main
    app = main.app
    monkeypatch.setattr(main, "run_migrations", lambda: None)            # схема уже создана тестовой фикстурой
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    assert srv.started, "uvicorn не стартовал"
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(10)


def test_slow_git_provider_does_not_block_the_event_loop(live_server, repo, kube, monkeypatch, admin):
    """Регрессия: разбор репозитория ждёт ответа провайдера — остальные запросы платформы в это время должны обслуживаться."""
    import threading
    import time
    import httpx

    orig = repo.list_dir

    def slow(path, ref):
        time.sleep(1.0)
        return orig(path, ref)
    monkeypatch.setattr(repo, "list_dir", slow)
    res = {}
    t = threading.Thread(target=lambda: res.update(r=httpx.post(f"{live_server}/api/onboarding/inspect", json={"url": "https://github.com/acme/shop"}, headers=admin, timeout=30)))
    t.start()
    time.sleep(0.3)
    started = time.time()
    other = httpx.get(f"{live_server}/api/projects", headers=admin, timeout=10)
    quick = time.time() - started
    t.join(30)
    assert other.status_code == 200 and quick < 0.7, f"цикл событий заблокирован: {quick:.2f}с"
    assert res["r"].status_code == 200


# ---------- create: кластер ----------

def test_create_provisions_and_starts_first_deploy(client, admin, kube, deploys):
    r = create(client, admin, host="shop.example.com")
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["provisioned"] is True and out["target"] == "cluster" and "manifest_yaml" not in out
    assert [c[0] for c in kube.created] == ["Namespace", "RoleBinding", "Deployment", "Service", "Ingress"]
    assert out["deploy"] == {"started": True, "revision": "a" * 40}
    assert len(deploys) == 1 and deploys[0][:3] == ("my-shop", "prod", "a" * 40)
    db = SessionLocal()
    e = db.query(Environment).one()
    assert (e.namespace, e.deployment_name, e.container_name, e.app_port, e.branch, e.server) == ("my-shop", "my-shop", "my-shop", 9000, "main", None)
    p = db.query(Project).one()
    assert p.build_method == "dockerfile" and p.provider == "github" and p.git_token_enc is None
    db.close()
    assert out["webhook"]["provider"] == "github" and out["webhook"]["path"] == "/webhook/github"


def test_create_without_permission_returns_manifest_and_waits(client, admin, kube, deploys):
    kube.fail = {"Deployment": 403}
    out = create(client, admin).json()
    assert out["provisioned"] is False and out["reason"] == "no_permission" and "Deployment" in out["detail"]
    docs = list(yaml.safe_load_all(out["manifest_yaml"]))
    assert [d["kind"] for d in docs] == ["Namespace", "RoleBinding", "Deployment", "Service"]
    assert out["deploy"] == {"started": False, "reason": "apply_manifest_first"} and deploys == []


def test_create_manual_mode_and_cluster_error(client, admin, kube, deploys):
    out = create(client, admin, provision=False).json()
    assert out["reason"] == "manual" and kube.created == [] and "manifest_yaml" in out
    client.delete("/api/projects/my-shop", headers=admin)
    kube.fail = {"Service": 500}
    out = create(client, admin).json()
    assert out["provisioned"] is False and out["reason"] == "error" and "manifest_yaml" in out


def test_create_stores_encrypted_token_and_forwards_nothing_else(client, admin, kube, deploys):
    assert create(client, admin, git_token="ghp_x", git_username="bot").status_code == 201
    db = SessionLocal()
    p = db.query(Project).one()
    assert p.git_token_enc and b"ghp_x" not in p.git_token_enc and p.git_username == "bot"
    db.close()


def test_create_deploy_off_and_approval(client, admin, kube, deploys):
    assert create(client, admin, deploy=False).json()["deploy"] == {"started": False, "reason": "deploy_not_requested"}
    client.delete("/api/projects/my-shop", headers=admin)
    assert create(client, admin, require_approval=True).json()["deploy"] == {"started": False, "reason": "approval_required"}
    assert deploys == []


@pytest.mark.parametrize("kw,code", [
    ({"namespace": "kube-system"}, 422), ({"host": "*.example.com"}, 422), ({"port": 0}, 422), ({"slug": "Bad_Slug"}, 422),
    ({"cluster": "x", "server": "y"}, 422), ({"build_method": "magic"}, 422),
])
def test_create_validation_creates_nothing(client, admin, kube, deploys, kw, code):
    assert create(client, admin, **kw).status_code == code
    assert SessionLocal().query(Project).count() == 0 and kube.created == [] and deploys == []


def test_create_conflicts(client, admin, kube, deploys):
    assert create(client, admin).status_code == 201
    kube.created.clear()
    assert create(client, admin).status_code == 409 and kube.created == []
    assert create(client, admin, slug="other").status_code == 409                 # тот же репозиторий


def test_create_requires_projects_manage(client, make_token, kube, deploys):
    assert create(client, make_token("devops")).status_code == 403


# ---------- создание: сервер ----------

@pytest.fixture
def licensed(monkeypatch):
    class T:
        def names(self): return ["vps-1"]
    monkeypatch.setattr(targets, "current_license", lambda: NS(features=frozenset({"servers"})))
    monkeypatch.setattr(onboarding, "current_license", lambda: NS(features=frozenset({"servers"})))
    monkeypatch.setattr(plugins, "SERVER_TARGET", T())


def test_create_on_server_needs_no_manifest_and_deploys(client, admin, kube, deploys, licensed):
    out = create(client, admin, server="vps-1").json()
    assert out["target"] == "server" and "manifest_yaml" not in out and "provisioned" not in out and kube.created == []
    assert out["deploy"]["started"] is True and len(deploys) == 1
    e = SessionLocal().query(Environment).one()
    assert e.server == "vps-1" and e.runtime["ports"] == [{"host": 9000, "container": 9000, "protocol": "tcp", "bind": "0.0.0.0"}]


def test_server_target_needs_license_and_creates_nothing(client, admin, kube, deploys):
    r = create(client, admin, server="vps-1")
    assert r.status_code == 403 and SessionLocal().query(Project).count() == 0 and deploys == []


def test_capabilities_list_servers_when_licensed(client, admin, repo, kube, licensed):
    assert inspect(client, admin).json()["capabilities"]["servers"] == ["vps-1"]


# ---------- манифест и повторное создание ----------

def test_manifest_endpoint_and_reprovision(client, admin, kube, deploys):
    kube.fail = {"Namespace": 403}
    assert create(client, admin).json()["provisioned"] is False
    m = client.get("/api/onboarding/my-shop/manifest?host=shop.example.com", headers=admin)
    assert m.status_code == 200 and "kind: Ingress" in m.json()["manifest_yaml"] and "9000" in m.json()["manifest_yaml"]
    kube.fail, kube.created = {}, []
    again = client.post("/api/onboarding/my-shop/provision", headers=admin).json()
    assert again["provisioned"] is True and [c[0] for c in kube.created] == ["Namespace", "RoleBinding", "Deployment", "Service"]
    assert client.get("/api/onboarding/nope/manifest", headers=admin).status_code == 404


def test_manifest_unavailable_for_server_and_legacy_envs(client, admin, kube, deploys, licensed, project):
    assert client.get("/api/onboarding/demo/manifest", headers=admin).status_code == 422       # у старой среды порт неизвестен
    create(client, admin, server="vps-1", slug="svr", repo_full_name="acme/svr")
    assert client.get("/api/onboarding/svr/manifest", headers=admin).status_code == 422


def test_access_endpoint(client, admin, kube):
    assert client.get("/api/onboarding/access", headers=admin).json() == {"allowed": True, "missing": []}
    kube.allowed = False
    r = client.get("/api/onboarding/access?ingress=true", headers=admin).json()
    assert r["allowed"] is False and len(r["missing"]) == 6
    assert client.get("/api/onboarding/access?cluster=edge", headers=admin).status_code == 403     # удалённые кластеры — платная функция


def test_buildpacks_without_kpack_are_refused_with_a_clear_message(monkeypatch):
    from app.services import kpack
    monkeypatch.setattr(kpack, "available", lambda: False)
    import importlib
    from app.routers import onboarding
    assert onboarding.kpack.available() is False
