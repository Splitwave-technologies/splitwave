"""Мастер подключения проекта: «вставил ссылку — получил работающее приложение».

inspect  — по адресу репозитория предлагает настройки (ничего не создаёт);
create   — создаёт проект и среду, готовит приложение в кластере (сам или YAML для администратора) и запускает первый выкат;
access / manifest / provision — права платформы в кластере и повторная выдача или применение манифеста.

Блокирующие обращения к Git-провайдеру и Kubernetes выполняются в пуле потоков, чтобы не останавливать цикл событий."""
import json
import threading
import time
from collections import deque

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app import licensing, plugins
from app.auth import Principal, require
from app.config import settings
from app.db.base import SessionLocal
from app.db.models import Environment
from app.licensing import current_license
from app.routers import projects as projects_router
from app.services import audit, kpack, manifests, project_repo, provision, repoinspect
from app.services import dockerfiles as dockergen
from app.services import webhook_register as hookreg
from app.services import limits as limits_svc
from app.services import deployments as deps

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])

RATE_PER_MINUTE = 20
_recent: dict[str, deque] = {}
_lock = threading.Lock()


def _rate_limit(who: str) -> None:
    """Разбор репозитория ходит во внешний API: ограничиваем частоту на пользователя."""
    now = time.monotonic()
    with _lock:
        q = _recent.setdefault(who, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_PER_MINUTE:
            raise HTTPException(status_code=429, detail="too many repository checks, wait a minute")
        q.append(now)


class InspectIn(BaseModel):
    url: str = Field(max_length=300)
    token: str | None = Field(default=None, max_length=500)
    provider: str | None = Field(default=None, max_length=20)
    branch: str | None = Field(default=None, max_length=100)


def capabilities() -> dict:
    feats = current_license().features
    return {"clusters": ["local"] + (plugins.cluster_names() if "multi_cluster" in feats else []),
            "servers": plugins.SERVER_TARGET.names() if "servers" in feats and plugins.SERVER_TARGET else [],
            "registry_prefix": settings.registry_prefix, "ingress_class": settings.ingress_class or None,
            "pull_secret": bool(settings.app_pull_secret)}


@router.post("/inspect")
async def inspect(body: InspectIn, principal: Principal = Depends(require("projects:manage"))):
    _rate_limit(principal.name)
    try:
        proposal = await run_in_threadpool(repoinspect.inspect_repo, body.url, body.token, body.provider, body.branch)
    except repoinspect.InspectError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    if proposal.get("build_method") == "buildpacks" and not await run_in_threadpool(kpack.available):
        proposal.setdefault("warnings", []).append({"code": "buildpacks_unavailable", "message": "В кластере не установлен kpack: сборка через buildpacks недоступна. Добавьте Dockerfile в репозиторий или создайте проект с Dockerfile платформы."})
    db = SessionLocal()
    try:
        existing = project_repo.get_project_by_repo(db, proposal["repo_full_name"], proposal["provider"])
        proposal["existing_project"] = existing.slug if existing else None
        taken = project_repo.get_project_by_slug(db, proposal["slug"])
        if taken and not existing:
            n = 2
            while project_repo.get_project_by_slug(db, f"{proposal['slug'][:36]}-{n}"):
                n += 1
            proposal["slug"] = f"{proposal['slug'][:36]}-{n}"
    finally:
        db.close()
    caps = capabilities()
    db = SessionLocal()
    try:
        caps["usage"] = limits_svc.usage(db)
    finally:
        db.close()
    caps["limits"] = {**licensing.limits(), "enforced": settings.tier_limits_enforce}
    caps["provision"] = {"local": await run_in_threadpool(provision.check_access, None, True)}
    return {"proposal": proposal, "capabilities": caps}


@router.get("/access")
async def access(cluster: str | None = None, ingress: bool = False, principal: Principal = Depends(require("projects:manage"))):
    """Может ли платформа сама создавать приложения в этом кластере (иначе мастер даст YAML для администратора)."""
    from app.services import clusters as clusters_svc
    name = clusters_svc.normalize(cluster)
    return await run_in_threadpool(provision.check_access, name, ingress)


class DockerfileIn(BaseModel):
    language: str = Field(max_length=20)
    facts: dict = Field(default_factory=dict)         # из ответа inspect (dockerfile_template.facts)
    params: dict = Field(default_factory=dict)        # port, start_command, runtime_version


@router.post("/dockerfile")
async def regenerate_dockerfile(body: DockerfileIn, principal: Principal = Depends(require("projects:manage"))):
    """Пересоздаёт предложенный Dockerfile по изменённым параметрам (без обращений к провайдеру)."""
    if len(json.dumps(body.facts)) > 8000 or len(json.dumps(body.params)) > 2000:
        raise HTTPException(status_code=422, detail="parameters are too large")
    try:
        return dockergen.render(body.language, body.facts, body.params)
    except dockergen.DockerfileError as e:
        raise HTTPException(status_code=422, detail=str(e))


class CreateIn(BaseModel):
    slug: str = Field(pattern=projects_router.DNS_NAME, max_length=40)
    repo_full_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+$")
    provider: str = "github"
    git_url: str | None = Field(default=None, max_length=300)
    git_token: str | None = Field(default=None, max_length=500)
    git_username: str | None = Field(default=None, max_length=100)
    sub_path: str = ""
    build_method: str = "buildpacks"
    dockerfile_path: str = "Dockerfile"
    dockerfile_content: str | None = Field(default=None, max_length=20000)    # Dockerfile платформы (предложен мастером, возможно отредактирован)
    registry_prefix: str | None = None
    branch: str = Field(default="main", min_length=1, max_length=100)
    environment: str = Field(default="prod", pattern=projects_router.DNS_NAME)
    port: int = Field(default=8080, ge=1, le=65535)
    namespace: str | None = Field(default=None, pattern=projects_router.DNS_NAME)
    host: str | None = Field(default=None, max_length=253)           # доменное имя → Ingress (только кластер)
    cluster: str | None = Field(default=None, pattern=projects_router.DNS_NAME)
    server: str | None = Field(default=None, pattern=projects_router.DNS_NAME)
    runtime: dict | None = None                                      # для сервера; по умолчанию публикуется порт приложения
    auto_deploy: bool = True
    require_approval: bool = False
    provision: bool = True                                           # создать приложение в кластере самим (если есть права)
    deploy: bool = True                                              # сразу запустить первый выкат
    head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")
    webhook_token: str | None = Field(default=None, max_length=500)       # токен с правом управлять webhook (используется один раз, не сохраняется)
    public_url: str | None = Field(default=None, max_length=300)          # внешний адрес платформы для webhook (мастер подставляет адрес страницы)


def _manifests_for(env: Environment, host: str | None) -> list[dict]:
    if env.server:
        raise HTTPException(status_code=422, detail="server environments do not use Kubernetes manifests")
    if not env.app_port:
        raise HTTPException(status_code=422, detail="the application port is unknown for this environment")
    try:
        return manifests.build(name=env.deployment_name, namespace=env.namespace, container=env.container_name, port=env.app_port, host=host or None)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


def _try_provision(docs: list[dict], cluster: str | None) -> dict:
    try:
        return {"provisioned": True, "results": provision.apply(docs, cluster)}
    except provision.ProvisionDenied as e:
        return {"provisioned": False, "reason": "no_permission", "detail": e.what}
    except provision.ProvisionError as e:
        return {"provisioned": False, "reason": "error", "detail": str(e)}
    except Exception as e:                        # кластер недоступен и т. п.: проект уже создан, отдаём YAML
        return {"provisioned": False, "reason": "unavailable", "detail": type(e).__name__}


@router.post("/create", status_code=201)
async def create(body: CreateIn, background_tasks: BackgroundTasks, principal: Principal = Depends(require("projects:manage"))):
    if body.cluster and body.server:
        raise HTTPException(status_code=422, detail="choose either a cluster or a server")
    on_server = bool(body.server and body.server != "local")
    if body.build_method == "buildpacks" and not on_server and not body.cluster and not await run_in_threadpool(kpack.available):
        raise HTTPException(status_code=422, detail="buildpacks are not available: kpack is not installed in the cluster. Add a Dockerfile to the repository or use the platform Dockerfile.")
    namespace = body.namespace or body.slug
    docs = None
    if not on_server:
        try:                                       # проверяем имена ДО создания проекта
            docs = manifests.build(name=body.slug, namespace=namespace, container=body.slug, port=body.port, host=body.host or None)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    runtime = body.runtime
    if on_server and runtime is None:
        runtime = {"ports": [{"host": body.port, "container": body.port}]}
    env_in = projects_router.EnvironmentIn(name=body.environment, namespace=namespace, deployment_name=body.slug, container_name=body.slug,
                                           branch=body.branch, auto_deploy=body.auto_deploy, require_approval=body.require_approval,
                                           cluster=body.cluster, server=body.server, runtime=runtime, app_port=body.port)
    project_in = projects_router.ProjectIn(
        slug=body.slug, repo_full_name=body.repo_full_name, provider=body.provider, git_url=body.git_url, git_token=body.git_token,
        git_username=body.git_username, sub_path=body.sub_path, build_method=body.build_method, dockerfile_path=body.dockerfile_path,
        dockerfile_content=body.dockerfile_content,
        registry_prefix=body.registry_prefix, environments=[env_in])
    db = SessionLocal()
    try:
        project = projects_router.create_project_record(db, project_in, principal)
        env = project_repo.get_environment(db, project, body.environment)
        out = {"slug": project.slug, "environment": env.name, "target": "server" if on_server else "cluster",
               "webhook": projects_router.webhook_info(project)}
        if body.webhook_token:
            out["webhook_registered"] = await run_in_threadpool(_register_hook, project, body.webhook_token, body.public_url)
            audit.log_event(db, principal.name, "webhook_registered" if out["webhook_registered"]["ok"] else "webhook_register_failed", project.id,
                            detail={"via": "wizard", "ok": out["webhook_registered"]["ok"]})
        ready = on_server                          # для сервера ничего готовить не нужно: приложение запустит агент
        if not on_server:
            cluster = env.cluster
            if body.provision:
                result = await run_in_threadpool(_try_provision, docs, cluster)
            else:
                result = {"provisioned": False, "reason": "manual"}
            out.update(result)
            ready = result["provisioned"]
            if not ready:
                out["manifest_yaml"] = manifests.to_yaml(docs)
        audit.log_event(db, principal.name, "onboarding_create", project.id, env.id,
                        {"target": out["target"], "provisioned": out.get("provisioned", True), "first_deploy": bool(body.deploy and ready)})
        if body.deploy and ready and body.require_approval:
            out["deploy"] = {"started": False, "reason": "approval_required"}     # первый выкат в среде с согласованием — через заявку
        elif body.deploy and ready:
            deps.schedule_redeploy(background_tasks, db, project, env, body.head_sha, principal.name, label="onboarding")
            out["deploy"] = {"started": True, "revision": body.head_sha or body.branch}
        else:
            out["deploy"] = {"started": False, "reason": "deploy_not_requested" if not body.deploy else "apply_manifest_first"}
        return out
    finally:
        db.close()


def _register_hook(project, token: str, public_url: str | None) -> dict:
    """Создаёт webhook в репозитории; любая ошибка — {ok: False, error}, проект при этом остаётся созданным."""
    info = projects_router.webhook_info(project)
    secret = info["secret"] or settings.github_webhook_secret
    try:
        ref = repoinspect.parse_repo_url(project.clone_url, project.provider)
        out = hookreg.register(project.provider, ref, token, public_url, info["path"], secret)
        return {"ok": True, **out}
    except (hookreg.RegisterError, repoinspect.InspectError) as e:
        return {"ok": False, "error": getattr(e, "message", None) or str(e)}
    except Exception as e:                       # непредвиденное: не роняем создание проекта и не раскрываем подробности
        return {"ok": False, "error": f"Не удалось создать webhook ({type(e).__name__})."}


class HookIn(BaseModel):
    token: str = Field(min_length=1, max_length=500)
    public_url: str | None = Field(default=None, max_length=300)


@router.post("/{slug}/webhook")
async def create_webhook(slug: str, body: HookIn, principal: Principal = Depends(require("projects:manage"))):
    """Автоматически создаёт (или обновляет) webhook проекта в репозитории. Токен нигде не сохраняется."""
    db = SessionLocal()
    try:
        project = project_repo.get_project_by_slug(db, slug)
        if not project:
            raise HTTPException(status_code=404, detail=f"unknown project {slug}")
        res = await run_in_threadpool(_register_hook, project, body.token, body.public_url)
        audit.log_event(db, principal.name, "webhook_registered" if res["ok"] else "webhook_register_failed", project.id,
                        detail={"result": res.get("result"), "url": res.get("url")} if res["ok"] else {"error": res["error"][:200]})
        return res
    finally:
        db.close()


@router.get("/{slug}/manifest")
async def get_manifest(slug: str, environment: str = "prod", host: str | None = Query(default=None, max_length=253),
                       principal: Principal = Depends(require("projects:manage"))):
    db = SessionLocal()
    try:
        env = projects_router._get_project_and_env(db, slug, environment)[1]
        return {"manifest_yaml": manifests.to_yaml(_manifests_for(env, host))}
    finally:
        db.close()


@router.post("/{slug}/provision")
async def provision_again(slug: str, environment: str = "prod", host: str | None = Query(default=None, max_length=253),
                          principal: Principal = Depends(require("projects:manage"))):
    """Повторная попытка создать приложение в кластере (например, после выдачи платформе прав provisioner-rbac.yaml)."""
    db = SessionLocal()
    try:
        env = projects_router._get_project_and_env(db, slug, environment)[1]
        docs = _manifests_for(env, host)
        result = await run_in_threadpool(_try_provision, docs, env.cluster)
        if not result["provisioned"]:
            result["manifest_yaml"] = manifests.to_yaml(docs)
        return result
    finally:
        db.close()
