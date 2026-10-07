"""Изолированный сервер для UI-тестов: SQLite, «кластер» подменён заглушками. Запуск: python tests/ui_server.py"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # корень control-plane

from cryptography.fernet import Fernet

_tmp = tempfile.mkdtemp()
os.environ.update(SPLITWAVE_TEST_MODE="1", UPDATE_CHECK="false", TIER_LIMITS_ENFORCE="false", 
    DATABASE_URL=f"sqlite:///{_tmp}/ui.db",
    SECRET_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    ADMIN_BOOTSTRAP_TOKEN="ui-test-admin-token-0123456789abcdef",
    GITHUB_WEBHOOK_SECRET="ui-test",
)

import uvicorn  # noqa: E402

from app import auth  # noqa: E402
from app.db.base import Base, SessionLocal, engine  # noqa: E402
from app.db.models import Release  # noqa: E402
from app.routers import webhooks  # noqa: E402
from app.services import deploy, kpack  # noqa: E402
from app.services import secrets as secrets_svc  # noqa: E402
import app.main as main  # noqa: E402
from app.services import notifications  # noqa: E402

from app import plugins  # noqa: E402

# Командные сценарии (несколько пользователей, роли, несколько сред на проект) проверяются на настоящем закрытом модуле: UI_WITH_EE=1 и EE_PATH.
if os.environ.get("UI_WITH_EE") == "1":
    sys.path.insert(0, os.environ["EE_PATH"])
    from platform_ee import environments as _ee_envs, teams as _ee_teams  # noqa: E402  (остальные платные модули здесь заменены заглушками)
    plugins.set_teams(_ee_teams.provider)
    plugins.set_environments(_ee_envs.provider)
    from platform_ee import projects as _ee_projects  # noqa: E402
    plugins.set_projects(_ee_projects.provider)


class _Projects:
    """Заглушка платного модуля projects для интерфейсных тестов (настоящий проверяется в закрытом репозитории)."""
    def check_new(self, db):
        pass

    def routed(self, db, project):
        return True


if plugins.PROJECTS is None:
    plugins.set_projects(_Projects())

plugins.register_login_provider("oidc", "Test IdP", "/api/sso/login")   # как если бы подключён платный модуль SSO
# Имитация платного модуля SIEM (настоящий лежит в закрытом репозитории): проверяем только интерфейс.
from types import SimpleNamespace  # noqa: E402
from fastapi import APIRouter, Depends  # noqa: E402
from app.auth import require  # noqa: E402

main.current_license = lambda refresh=False: SimpleNamespace(features=frozenset({"siem", "multi_cluster", "preview_envs", "servers"}))
_siem = APIRouter(prefix="/api/siem")
_dests: list[dict] = []


@_siem.get("/destinations")
async def _siem_list(_p=Depends(require("integrations:manage"))):
    return _dests


@_siem.post("/destinations", status_code=201)
async def _siem_create(body: dict, _p=Depends(require("integrations:manage"))):
    d = {"id": f"d{len(_dests)}", "name": body["name"], "kind": body["kind"], "format": body.get("format", "json"), "enabled": True,
         "target": body.get("host") or "logs.example.com", "cursor": 0, "head": 5, "lag": 5, "sent_total": 0, "last_ok_at": None, "last_error": None}
    _dests.append(d)
    return d


@_siem.post("/destinations/{did}/test")
async def _siem_test(did: str, _p=Depends(require("integrations:manage"))):
    return {"ok": True, "error": None}


@_siem.delete("/destinations/{did}")
async def _siem_delete(did: str, _p=Depends(require("integrations:manage"))):
    _dests[:] = [d for d in _dests if d["id"] != did]
    return {"deleted": did}


_clusters = APIRouter(prefix="/api/clusters")
_cl: list[dict] = []


@_clusters.get("")
async def _cl_list(_p=Depends(require("integrations:manage"))):
    return _cl


@_clusters.post("", status_code=201)
async def _cl_create(body: dict, _p=Depends(require("integrations:manage"))):
    c = {"name": body["name"], "server": body["server"], "has_ca": bool(body.get("ca_pem")), "environments": 0, "last_check": None, "version": None}
    _cl.append(c)
    return c


@_clusters.post("/{name}/test")
async def _cl_test(name: str, _p=Depends(require("integrations:manage"))):
    for c in _cl:
        if c["name"] == name:
            c["version"] = "v1.31.2+k3s1"
    return {"ok": True, "version": "v1.31.2+k3s1", "error": None}


@_clusters.delete("/{name}")
async def _cl_delete(name: str, _p=Depends(require("integrations:manage"))):
    _cl[:] = [c for c in _cl if c["name"] != name]
    return {"deleted": name}


_pv_cfg: dict = {}
_pv: list[dict] = [{"name": "pr-7", "pr": "7", "branch": "feature/login", "namespace": "apps", "deployment": "shop-pr-7", "created_at": "2026-10-01T10:00:00+00:00"}]
_previews = APIRouter(prefix="/api/projects/{slug}/previews")


@_previews.get("")
async def _pv_get(slug: str, _p=Depends(require("read"))):
    return {"config": _pv_cfg.get(slug, {"enabled": False, "base_env": ""}), "limit": 5, "ttl_hours": 168, "previews": _pv}


@_previews.put("")
async def _pv_put(slug: str, body: dict, _p=Depends(require("projects:manage"))):
    _pv_cfg[slug] = body
    return body


@_previews.delete("/{pr}")
async def _pv_del(slug: str, pr: str, _p=Depends(require("projects:manage"))):
    _pv[:] = [p for p in _pv if p["pr"] != pr]
    return {"deleted": pr}


_srv: list[dict] = [{"name": "vps-demo", "status": "online", "os": "Ubuntu 24.04", "docker": "27.1.1", "cpus": 2, "mem_mb": 1967, "disk_free_gb": 14.2,
                     "environments": 0, "last_seen_at": "2026-10-02T10:00:00+00:00", "has_registry": False, "containers": []}]
_servers = APIRouter(prefix="/api/servers")


@_servers.get("")
async def _srv_list(_p=Depends(require("integrations:manage"))):
    return _srv


@_servers.post("", status_code=201)
async def _srv_create(body: dict, _p=Depends(require("integrations:manage"))):
    s = {"name": body["name"], "status": "pending", "os": None, "docker": None, "cpus": None, "mem_mb": None, "disk_free_gb": None, "environments": 0,
         "last_seen_at": None, "has_registry": False, "containers": []}
    _srv.append(s)
    return {**s, "enroll_token": "dspe_demo", "enroll_expires_at": "2026-10-02T11:00:00+00:00",
            "install_command": "curl -fsSL http://platform.test/agent/install.sh | sudo sh -s -- --token dspe_demo"}


@_servers.delete("/{name}")
async def _srv_delete(name: str, _p=Depends(require("integrations:manage"))):
    _srv[:] = [s for s in _srv if s["name"] != name]
    return {"deleted": name}


@_servers.get("/{name}/jobs")
async def _srv_jobs(name: str, _p=Depends(require("integrations:manage"))):
    return []


main.app.include_router(_servers)
main.app.include_router(_previews)
main.app.include_router(_clusters)
main.app.include_router(_siem)

# Только для UI-тестов: включить/выключить лимиты и выбрать редакцию без перезапуска сервера
from app import licensing as _lic  # noqa: E402
from app.config import settings as _settings  # noqa: E402
_test = APIRouter(prefix="/__test")
_real_license = _lic.current_license


@_test.post("/limits")
async def _limits(body: dict):
    _settings.tier_limits_enforce = bool(body.get("enforce"))
    tier = body.get("tier", "community")
    _lic.current_license = (lambda refresh=False: SimpleNamespace(valid=True, tier=tier, status="valid", features=frozenset(), public=lambda: {"status": "valid", "tier": tier, "customer": "ui", "features": [], "expires_at": None, "reason": "ok"})) if body.get("enforce") else _real_license
    return {"ok": True}


main.app.include_router(_test)

notifications._resolve = lambda host: ["93.184.216.34"]          # без DNS: имена считаются публичными
notifications._post = lambda url, body, headers: 200             # наружу ничего не отправляем

Base.metadata.create_all(engine)
db = SessionLocal()
auth.bootstrap_admin(db)
db.close()

main.run_migrations = lambda: None  # миграции рассчитаны на Postgres
kpack.get_status = lambda slug, namespace=None: {"latest_image": f"reg/{slug}@sha256:" + "ab" * 32, "ready": True}
secrets_svc.sync_to_cluster = lambda db, project, env: len(secrets_svc.decrypt_all(db, project, env.name))
deploy.deploy_image = lambda *a, **k: None
deploy.wait_rollout = lambda *a, **k: None   # настоящий кластер в UI-тестах не нужен


def fake_build(project_id, environment_id, git_url, revision, triggered_by):
    session = SessionLocal()
    session.add(Release(environment_id=environment_id, git_revision=revision, status="deployed",
                        image_digest="reg/x@sha256:" + os.urandom(32).hex(), triggered_by=triggered_by))
    session.commit()
    session.close()


webhooks.run_build_and_deploy = fake_build

from app.services import runtime as runtime_svc  # noqa: E402

runtime_svc.deployment_runtime = lambda ns, dep, con, cluster=None: {
    "replicas": 2, "ready": 1, "updated": 2, "available_replicas": 1, "image": f"reg/{dep}@sha256:" + "ab" * 32,
    "conditions": [{"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable"}],
    "pods": [{"name": f"{dep}-7d9f-aaaaa", "phase": "Running", "ready": True, "restarts": 0, "started": "2026-09-29T10:00:00+00:00", "reason": None},
             {"name": f"{dep}-7d9f-bbbbb", "phase": "Running", "ready": False, "restarts": 4, "started": "2026-09-29T10:05:00+00:00", "reason": "CrashLoopBackOff"}]}
runtime_svc.list_builds = lambda slug, ns, limit=15: [
    {"name": f"{slug}-build-3", "created_at": "2026-09-29T12:00:00Z", "status": "building", "reason": None, "message": "", "revision": "d4e5f6a1", "latest_image": None},
    {"name": f"{slug}-build-2", "created_at": "2026-09-29T11:00:00Z", "status": "failed", "reason": "BuildFailed", "message": "exit status 1", "revision": "b2c3d4e5", "latest_image": None},
    {"name": f"{slug}-build-1", "created_at": "2026-09-29T10:00:00Z", "status": "succeeded", "reason": "", "message": "", "revision": "a1b2c3d4", "latest_image": "reg/x@sha256:1"}]
runtime_svc.build_logs = lambda slug, ns, build, tail_lines=400: f"=== prepare ===\ncloning {slug}\n=== build ===\nbuilding {build}\n<script>window.__logxss=1</script>\n"

# Мастер подключения: Git-провайдер и кластер подменены (репозиторий выбирается по имени: docker / bare / private / missing, иначе Node-приложение)
from app.services import provision, repoinspect  # noqa: E402


class _UiRepo:
    def __init__(self, ref, token):
        name = ref.full_name.split("/")[-1]
        if "missing" in name:
            raise repoinspect.InspectError("Репозиторий не найден. Если он приватный, укажите токен доступа.")
        self.private = "private" in name
        if "docker" in name:
            self.files = {"Dockerfile": "FROM python:3.12\nEXPOSE 7000\n", "requirements.txt": "fastapi\n"}
        elif "bare" in name:
            self.files = {"README.md": "hello"}
        else:
            self.files = {"package.json": '{"dependencies": {"express": "4"}, "scripts": {"start": "node ."}}', ".env.example": "DB_URL=x\nAPI_KEY=y\n"}

    def repo_info(self):
        return {"default_branch": "main", "private": self.private}

    def head_sha(self, branch):
        return "c0ffee1" + "0" * 33 if branch == "main" else ""

    def list_dir(self, path, ref):
        prefix = path + "/" if path else ""
        out = {}
        for fp in self.files:
            if fp.startswith(prefix):
                rest = fp[len(prefix):]
                out[rest.split("/")[0]] = "/" in rest
        return list(out.items())

    def read_file(self, path, ref):
        return self.files.get(path)


repoinspect.CLIENTS = {pr: _UiRepo for pr in repoinspect.CLIENTS}
provision.check_access = lambda cluster=None, ingress=False: {"allowed": True, "missing": []}


def _fake_apply(docs, cluster=None):
    if docs[0]["metadata"]["name"].startswith("noperm"):
        raise provision.ProvisionDenied("Deployment " + docs[2]["metadata"]["name"])
    return [{"kind": d["kind"], "name": d["metadata"]["name"], "result": "created"} for d in docs]


provision.apply = _fake_apply

# логи подов и домены: в памяти (в журнале «прошлого запуска» есть значение секрета — маршрут обязан его скрыть)
runtime_svc.pod_logs = lambda ns, dep, con, pod, tail=300, previous=False, cluster=None: (
    "Started app\nERROR boom: password=hunter2222 refused\n" if previous else "current log line\n")
_domains = {}
provision.get_ingress_host = lambda ns, name, cluster=None: _domains.get((ns, name))


def _fake_set_ingress(ns, name, host, cluster=None, tls_secret=None):
    had = (ns, name) in _domains
    if host is None:
        _domains.pop((ns, name), None)
        return {"result": "removed" if had else "none"}
    _domains[(ns, name)] = host
    return {"result": "updated" if had else "created"}


provision.set_ingress = _fake_set_ingress


if __name__ == "__main__":
    uvicorn.run(main.app, host="127.0.0.1", port=int(os.environ.get("PORT", "18099")), log_level="warning")
