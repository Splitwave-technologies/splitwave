import re
import uuid
from datetime import datetime, timezone

from starlette.concurrency import run_in_threadpool
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.sql import func

from app import plugins
from app.auth import Principal, require, require_scoped
from app.db.base import SessionLocal
from app.db.models import ApiToken, AuditEvent, Environment, Project, Release, SecretVersion, NotificationChannel
from app.services import approvals as approvals_svc
from app.services import audit, envstatus, kpack, project_repo
from app.services import dockerfiles as dockergen
from app.services import limits as limits_svc
from app.services import clusters as clusters_svc
from app.services import crypto, scm, targets
from app.services import deployments as deps
from app.routers.webhooks import run_build_and_deploy

router = APIRouter(prefix="/api/projects", tags=["projects"])


DNS_NAME = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"  # формат имён объектов Kubernetes


class EnvironmentIn(BaseModel):
    name: str = Field(default="prod", pattern=DNS_NAME)
    namespace: str = Field(pattern=DNS_NAME)
    deployment_name: str = Field(pattern=DNS_NAME)
    container_name: str = Field(pattern=DNS_NAME)
    branch: str = Field(default="main", min_length=1, max_length=100)
    auto_deploy: bool = True
    require_approval: bool = False
    cluster: str | None = Field(default=None, pattern=DNS_NAME)   # удалённый кластер (платная функция); None — кластер платформы
    server: str | None = Field(default=None, pattern=DNS_NAME)    # сервер с агентом (платная функция servers): приложение запускается в Docker
    runtime: dict | None = None                                    # параметры контейнера для сервера: порты, тома, проверка
    app_port: int | None = Field(default=None, ge=1, le=65535)    # порт приложения (для Service/Ingress; задаёт мастер подключения)


class ProjectIn(BaseModel):
    slug: str = Field(pattern=DNS_NAME, max_length=40)
    repo_full_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+$")   # у GitLab бывают вложенные группы: a/b/c
    provider: str = "github"                     # github | gitlab | bitbucket | gitea
    git_url: str | None = Field(default=None, max_length=300)   # адрес клонирования: обязателен для Gitea и самохостинга
    git_token: str | None = Field(default=None, max_length=500) # токен доступа к приватному репозиторию
    git_username: str | None = Field(default=None, max_length=100)
    sub_path: str = ""
    build_method: str = "buildpacks"             # buildpacks | dockerfile
    dockerfile_path: str = "Dockerfile"          # путь к Dockerfile относительно sub_path
    dockerfile_content: str | None = Field(default=None, max_length=20000)   # Dockerfile платформы вместо файла из репозитория (мастер)
    builder: str = "default"
    registry_prefix: str | None = None
    environments: list[EnvironmentIn] = Field(min_length=1)


def _target_fields(e) -> dict:
    """Куда выкатывается среда: кластер или сервер (взаимоисключающие), с проверкой лицензии и параметров."""
    if e.server and e.server != "local" and e.cluster and e.cluster != "local":
        raise HTTPException(status_code=422, detail="an environment targets either a cluster or a server, not both")
    cluster = clusters_svc.normalize(e.cluster)
    server, runtime = targets.normalize_server(e.server, cluster, e.runtime)
    return {"cluster": cluster, "server": server, "runtime": runtime}


def _check_build(method: str, sub_path: str, dockerfile_path: str) -> None:
    from app.services import kaniko
    if method not in ("buildpacks", "dockerfile"):
        raise HTTPException(status_code=422, detail="build_method must be buildpacks or dockerfile")
    if not kaniko.safe_path(sub_path or "") or not kaniko.safe_path(dockerfile_path or "Dockerfile") or not (dockerfile_path or "Dockerfile"):
        raise HTTPException(status_code=422, detail="sub_path and dockerfile_path must be relative paths (letters, digits, . _ - /) without '..'")


def _check_dockerfile_content(method: str, content: str | None) -> str | None:
    """None/'' — файла платформы нет; иначе проверенный текст (только для способа dockerfile)."""
    if not content or not content.strip():
        return None
    if method != "dockerfile":
        raise HTTPException(status_code=422, detail="dockerfile_content requires build_method dockerfile")
    try:
        return dockergen.validate_content(content)
    except dockergen.DockerfileError as e:
        raise HTTPException(status_code=422, detail=str(e))


class ProjectPatch(BaseModel):
    build_method: str | None = None
    dockerfile_content: str | None = Field(default=None, max_length=20000)   # "" — убрать Dockerfile платформы (будет браться файл из репозитория)
    dockerfile_path: str | None = None
    sub_path: str | None = None
    registry_prefix: str | None = None


@router.patch("/{slug}")
async def update_project(slug: str, body: ProjectPatch, principal: Principal = Depends(require("projects:manage"))):
    """Настройки сборки проекта: способ (buildpacks / Dockerfile), путь к Dockerfile, подпапка."""
    db = SessionLocal()
    try:
        project = project_repo.get_project_by_slug(db, slug)
        if not project:
            raise HTTPException(status_code=404, detail=f"unknown project {slug}")
        changes = body.model_dump(exclude_none=True)
        if not changes:
            raise HTTPException(status_code=422, detail="nothing to update")
        _check_build(changes.get("build_method", project.build_method), changes.get("sub_path", project.sub_path),
                     changes.get("dockerfile_path", project.dockerfile_path))
        if "dockerfile_content" in changes:
            changes["dockerfile_content"] = _check_dockerfile_content(changes.get("build_method", project.build_method), changes["dockerfile_content"])
        for k, v in changes.items():
            setattr(project, k, v)
        db.commit()
        audit.log_event(db, principal.name, "project_update", project.id,
                        detail={k: ("<dockerfile>" if k == "dockerfile_content" and v else v) for k, v in changes.items()})
        return {"build_method": project.build_method, "dockerfile_path": project.dockerfile_path, "sub_path": project.sub_path,
                "dockerfile_generated": bool(project.dockerfile_content)}
    finally:
        db.close()


def _check_git_url(provider: str, git_url: str | None) -> str | None:
    """Адрес клонирования: только https без встроенных учётных данных; для Gitea обязателен."""
    if not git_url:
        if provider == "gitea":
            raise HTTPException(status_code=422, detail="git_url is required for Gitea / Forgejo")
        return None
    from urllib.parse import urlparse
    u = urlparse(git_url)
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        raise HTTPException(status_code=422, detail="git_url must be an https URL without credentials")
    return git_url


def create_project_record(db, body: ProjectIn, principal: Principal) -> Project:
    """Создаёт проект и его среды: проверки, шифрование токена, аудит. Общая часть обычного создания и мастера подключения."""
    if project_repo.get_project_by_slug(db, body.slug):
        raise HTTPException(status_code=409, detail=f"project {body.slug} already exists")
    if body.provider not in scm.PROVIDERS:
        raise HTTPException(status_code=422, detail=f"provider must be one of {', '.join(scm.PROVIDERS)}")
    git_url = _check_git_url(body.provider, body.git_url)
    _check_build(body.build_method, body.sub_path, body.dockerfile_path)
    dockerfile_content = _check_dockerfile_content(body.build_method, body.dockerfile_content)
    if project_repo.get_project_by_repo(db, body.repo_full_name, body.provider):
        raise HTTPException(status_code=409, detail=f"repository {body.repo_full_name} is already registered")
    if len({e.name for e in body.environments}) != len(body.environments):
        raise HTTPException(status_code=422, detail="environment names must be unique")
    if len(body.environments) > 1:
        if not plugins.ENVIRONMENTS:
            raise HTTPException(status_code=403, detail={"error": "feature_not_licensed", "feature": "environments",
                                                          "message": "Several environments per project are part of the paid Environments module."})
        plugins.ENVIRONMENTS.check_new_project(len(body.environments))
    limits_svc.check_project(db)
    project = Project(slug=body.slug, repo_full_name=body.repo_full_name, sub_path=body.sub_path,
                      builder=body.builder, registry_prefix=body.registry_prefix, provider=body.provider, git_url=git_url,
                      build_method=body.build_method, dockerfile_path=body.dockerfile_path, dockerfile_content=dockerfile_content,
                      git_username=body.git_username,
                      git_token_enc=crypto.encrypt_value(body.git_token) if body.git_token else None)
    db.add(project)
    db.flush()
    for e in body.environments:
        db.add(Environment(project_id=project.id, name=e.name, namespace=e.namespace,
                           deployment_name=e.deployment_name, container_name=e.container_name,
                           branch=e.branch, auto_deploy=e.auto_deploy, require_approval=e.require_approval, app_port=e.app_port,
                           **_target_fields(e)))
    db.commit()
    audit.log_event(db, principal.name, "project_create", project.id,
                    detail={"slug": body.slug, "repo": body.repo_full_name, "provider": body.provider,
                            "environments": [e.name for e in body.environments]})
    return project


@router.post("", status_code=201)
async def create_project(body: ProjectIn, principal: Principal = Depends(require("projects:manage"))):
    db = SessionLocal()
    try:
        project = create_project_record(db, body, principal)
        return {"slug": project.slug, "environments": [e.name for e in body.environments]}
    finally:
        db.close()


@router.delete("/{slug}")
async def delete_project(slug: str, principal: Principal = Depends(require("projects:manage"))):
    """Удаляет проект из платформы (вместе с секретами и релизами в БД). Ресурсы в кластере не трогает."""
    db = SessionLocal()
    try:
        project = project_repo.get_project_by_slug(db, slug)
        if not project:
            raise HTTPException(status_code=404, detail=f"unknown project {slug}")
        pid = project.id
        # Аудит не удаляем: события отвязываем от проекта (внешний ключ), запоминая slug в деталях.
        env_ids = [e.id for e in project.environments]
        events = db.query(AuditEvent).filter(AuditEvent.project_id == pid)
        if env_ids:
            events = db.query(AuditEvent).filter(
                (AuditEvent.project_id == pid) | (AuditEvent.environment_id.in_(env_ids)))
        for ev in events.all():
            ev.context = {**(ev.context or {}), "project_slug": slug}
            ev.project_id = None
            ev.environment_id = None
        # токены, ограниченные этим проектом, отзываются и отвязываются (внешний ключ)
        for tok in db.query(ApiToken).filter(ApiToken.project_id == pid).all():
            tok.revoked_at = datetime.now(timezone.utc)
            tok.project_id = None
        db.query(SecretVersion).filter(SecretVersion.project_id == pid).delete()
        db.query(NotificationChannel).filter(NotificationChannel.project_id == pid).delete()
        db.flush()
        db.delete(project)
        db.commit()
        audit.log_event(db, principal.name, "project_delete", None, detail={"slug": slug, "project_id": str(pid)})
        return {"deleted": slug}
    finally:
        db.close()


@router.get("")
async def list_projects(principal: Principal = Depends(require_scoped("read"))):
    db = SessionLocal()
    try:
        result = []
        allowed = principal.allowed_project_ids()
        for project in project_repo.list_projects(db):
            if allowed is not None and str(project.id) not in allowed:
                continue
            for environment in project.environments:
                try:
                    status = envstatus.environment_status(db, environment)
                    error = status["error"]
                except Exception as e:  # нет доступа к namespace, кластер недоступен и т.п. — список всё равно отдаём
                    status = {"ready": None, "latest_image": None, "state": "unknown"}
                    error = f"{type(e).__name__}: {e}"[:200]
                result.append({
                    "slug": project.slug,
                    "repo": project.repo_full_name,
                    "provider": project.provider,
                    "environment": environment.name,
                    "namespace": environment.namespace,
                    "ready": status["ready"],
                    "latest_image": status["latest_image"],
                    "state": status["state"],
                    "error": error,
                })
        return result
    finally:
        db.close()


def _get_project_and_env(db, slug: str, env_name: str = "prod"):
    project = project_repo.get_project_by_slug(db, slug)
    if not project:
        raise HTTPException(status_code=404, detail=f"unknown project {slug}")
    environment = project_repo.get_environment(db, project, env_name)
    if not environment:
        raise HTTPException(status_code=404, detail=f"unknown environment {env_name} for project {slug}")
    return project, environment


@router.get("/{slug}/source")
async def get_source(slug: str, principal: Principal = Depends(require("read"))):
    """Где лежит код проекта (токен не отдаётся, только факт его наличия)."""
    db = SessionLocal()
    try:
        project = project_repo.get_project_by_slug(db, slug)
        if not project:
            raise HTTPException(status_code=404, detail=f"unknown project {slug}")
        return {"provider": project.provider, "repo": project.repo_full_name, "clone_url": project.clone_url,
                "has_git_token": bool(project.git_token_enc), "git_username": project.git_username,
                "build_method": project.build_method, "dockerfile_path": project.dockerfile_path, "sub_path": project.sub_path,
                "dockerfile_generated": bool(project.dockerfile_content)}
    finally:
        db.close()


@router.get("/{slug}/dockerfile")
async def get_dockerfile(slug: str, principal: Principal = Depends(require("projects:manage"))):
    """Dockerfile, который платформа хранит для проекта (None — используется файл из репозитория или buildpacks)."""
    db = SessionLocal()
    try:
        project = project_repo.get_project_by_slug(db, slug)
        if not project:
            raise HTTPException(status_code=404, detail=f"unknown project {slug}")
        return {"content": project.dockerfile_content}
    finally:
        db.close()


class GitTokenIn(BaseModel):
    token: str | None = Field(default=None, max_length=500)    # null — удалить токен
    username: str | None = Field(default=None, max_length=100)


@router.put("/{slug}/git-token")
async def put_git_token(slug: str, body: GitTokenIn, principal: Principal = Depends(require("projects:manage"))):
    db = SessionLocal()
    try:
        project = project_repo.get_project_by_slug(db, slug)
        if not project:
            raise HTTPException(status_code=404, detail=f"unknown project {slug}")
        project.git_token_enc = crypto.encrypt_value(body.token) if body.token else None
        project.git_username = body.username
        db.commit()
        audit.log_event(db, principal.name, "git_token_set" if body.token else "git_token_clear", project.id)
        return {"has_git_token": bool(body.token)}
    finally:
        db.close()


def webhook_info(project) -> dict:
    """Что указать в настройках вебхука репозитория."""
    if project.provider == "github":
        return {"provider": "github", "path": "/webhook/github", "secret": None, "content_type": "application/json",
                "events": ["push", "pull_request"], "note": "github_shared_secret"}
    events = {"gitlab": ["Push events", "Merge request events"], "bitbucket": ["repo:push", "pullrequest:created", "pullrequest:updated", "pullrequest:fulfilled", "pullrequest:rejected"],
              "gitea": ["push", "pull_request"]}[project.provider]
    return {"provider": project.provider, "path": f"/webhook/{project.provider}/{project.slug}",
            "secret": scm.webhook_secret(project.slug), "content_type": "application/json", "events": events}


@router.get("/{slug}/webhook")
async def get_webhook(slug: str, principal: Principal = Depends(require("projects:manage"))):
    """Что указать в настройках вебхука репозитория. Секрет выдаётся только управляющим проектом."""
    db = SessionLocal()
    try:
        project = project_repo.get_project_by_slug(db, slug)
        if not project:
            raise HTTPException(status_code=404, detail=f"unknown project {slug}")
        return webhook_info(project)
    finally:
        db.close()


@router.get("/{slug}/status")
async def project_status(slug: str, environment: str = "prod", principal: Principal = Depends(require("read"))):
    db = SessionLocal()
    try:
        project, env = _get_project_and_env(db, slug, environment)
        status = envstatus.environment_status(db, env)
        return {"slug": slug, "environment": env.name, **status}
    finally:
        db.close()


@router.get("/{slug}/releases")
async def list_releases(slug: str, environment: str = "prod", principal: Principal = Depends(require("read"))):
    db = SessionLocal()
    try:
        project, env = _get_project_and_env(db, slug, environment)
        releases = (
            db.query(Release)
            .filter_by(environment_id=env.id)
            .order_by(Release.created_at.desc())
            .limit(50)
            .all()
        )
        can_see_errors = principal.can("deploy", project.id)    # текст ошибки сборки может содержать вывод сборочных команд
        return [
            {
                "id": str(r.id),
                "error_message": (r.error_message or "")[:1500] if can_see_errors and r.status == "failed" else None,
                "image_digest": r.image_digest,
                "git_revision": r.git_revision,
                "status": r.status,
                "triggered_by": r.triggered_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "deployed_at": r.deployed_at.isoformat() if r.deployed_at else None,
            }
            for r in releases
        ]
    finally:
        db.close()


def _break_glass(db, principal: Principal, project, env, action: str, reason) -> None:
    """Аварийный обход согласования: только администратор и только с письменной причиной; фиксируется отдельным событием аудита."""
    if not principal.can("users:manage"):
        raise HTTPException(status_code=403, detail="only an administrator can bypass approval")
    if not reason or len(reason.strip()) < 10:
        raise HTTPException(status_code=422, detail="break-glass requires a reason (at least 10 characters)")
    audit.log_event(db, principal.name, "break_glass", project.id, env.id, {"action": action, "reason": reason.strip()})


def _needs_approval(response_body: dict):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=202, content=response_body)


@router.post("/{slug}/redeploy")
async def redeploy(slug: str, background_tasks: BackgroundTasks, environment: str = "prod",
                   revision: str | None = Query(default=None, pattern=r"^[0-9a-f]{7,40}$"),
                   break_glass: bool = False, reason: str | None = Query(default=None, max_length=300),
                   principal: Principal = Depends(require("deploy"))):
    """revision — конкретный коммит (SHA). Без него берётся ветка среды: сборка стартует, когда kpack
    заметит новый коммит, поэтому для точного результата указывайте SHA.
    Если у среды включено согласование, вместо выката создаётся заявка (ответ 202)."""
    db = SessionLocal()
    try:
        project, env = _get_project_and_env(db, slug, environment)
        if env.require_approval:
            if break_glass:
                _break_glass(db, principal, project, env, "redeploy", reason)
            else:
                a = approvals_svc.create(db, project, env, "redeploy",
                                         {"revision": revision or env.branch, "explicit_revision": bool(revision)},
                                         principal.name, principal.id)
                return _needs_approval({"requires_approval": True, "approval_id": str(a.id), "status": "pending"})
        return deps.schedule_redeploy(background_tasks, db, project, env, revision, principal.name)
    finally:
        db.close()


@router.post("/{slug}/rollback")
async def rollback(slug: str, environment: str = "prod", to: str | None = None,
                   break_glass: bool = False, reason: str | None = Query(default=None, max_length=300),
                   principal: Principal = Depends(require("rollback"))):
    db = SessionLocal()
    try:
        project, env = _get_project_and_env(db, slug, environment)
        target = deps.resolve_rollback_target(db, env, to)
        if env.require_approval:
            if break_glass:
                _break_glass(db, principal, project, env, "rollback", reason)
            else:
                a = approvals_svc.create(db, project, env, "rollback",
                                         {"to": str(target.id), "image": target.image_digest, "git_revision": target.git_revision},
                                         principal.name, principal.id)
                return _needs_approval({"requires_approval": True, "approval_id": str(a.id), "status": "pending"})
        return await run_in_threadpool(deps.execute_rollback, db, project, env, target, principal.name)
    finally:
        db.close()
