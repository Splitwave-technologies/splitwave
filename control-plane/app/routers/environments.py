from typing import Optional

from kubernetes.client.exceptions import ApiException
from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app import plugins
from app.auth import Principal, require
from app.config import settings
from app.db.base import SessionLocal
from app.db.models import AuditEvent, Environment
from app.routers.projects import DNS_NAME, EnvironmentIn, _target_fields
from app.services import audit, manifests, project_repo, provision, targets, certs
from app.services import limits as limits_svc
from app.services import environments as envs_svc
from app.services import clusters as clusters_svc
from app.services import secrets as secrets_svc

router = APIRouter(prefix="/api/projects/{slug}/environments", tags=["environments"])


class EnvironmentPatch(BaseModel):
    namespace: Optional[str] = Field(default=None, pattern=DNS_NAME)
    deployment_name: Optional[str] = Field(default=None, pattern=DNS_NAME)
    container_name: Optional[str] = Field(default=None, pattern=DNS_NAME)
    branch: Optional[str] = Field(default=None, min_length=1, max_length=100)
    auto_deploy: Optional[bool] = None
    require_approval: Optional[bool] = None
    cluster: Optional[str] = Field(default=None, pattern=DNS_NAME)   # "local" — вернуть в кластер платформы
    server: Optional[str] = Field(default=None, pattern=DNS_NAME)    # сервер с агентом; "local" — вернуть в кластер
    runtime: Optional[dict] = None


def _view(e: Environment) -> dict:
    return {"name": e.name, "namespace": e.namespace, "deployment_name": e.deployment_name,
            "container_name": e.container_name, "branch": e.branch, "auto_deploy": e.auto_deploy,
            "require_approval": e.require_approval, "cluster": e.cluster, "server": e.server, "runtime": e.runtime, "preview_of": e.preview_of,
            "created_at": e.created_at.isoformat() if e.created_at else None}


def _project(db, slug):
    project = project_repo.get_project_by_slug(db, slug)
    if not project:
        raise HTTPException(status_code=404, detail=f"unknown project {slug}")
    return project


@router.get("")
async def list_environments(slug: str, principal: Principal = Depends(require("read"))):
    db = SessionLocal()
    try:
        return [_view(e) for e in sorted(_project(db, slug).environments, key=lambda e: e.name)]
    finally:
        db.close()


@router.post("", status_code=201)
async def create_environment(slug: str, body: EnvironmentIn, principal: Principal = Depends(require("projects:manage"))):
    if not plugins.ENVIRONMENTS:
        raise HTTPException(status_code=403, detail={"error": "feature_not_licensed", "feature": "environments",
                                                      "message": "Several environments per project are part of the paid Environments module."})
    db = SessionLocal()
    try:
        env = plugins.ENVIRONMENTS.create(db, _project(db, slug), body, principal)
        return _view(env)
    finally:
        db.close()


@router.patch("/{name}")
async def update_environment(slug: str, name: str, body: EnvironmentPatch, principal: Principal = Depends(require("projects:manage"))):
    db = SessionLocal()
    try:
        project = _project(db, slug)
        env = project_repo.get_environment(db, project, name)
        if not env:
            raise HTTPException(status_code=404, detail=f"unknown environment {name}")
        changes = body.model_dump(exclude_none=True)
        if not changes:
            raise HTTPException(status_code=422, detail="nothing to update")
        if "cluster" in changes or "server" in changes or "runtime" in changes:
            cluster = clusters_svc.normalize(changes["cluster"]) if "cluster" in changes else (None if changes.get("server") else env.cluster)
            want_server = changes["server"] if "server" in changes else env.server
            if "cluster" in changes and cluster and "server" not in changes:
                want_server = "local"                 # переход на кластер снимает сервер
            server, runtime = targets.normalize_server(want_server, cluster, changes.get("runtime", env.runtime if want_server and want_server != "local" else None))
            changes.update({"cluster": cluster, "server": server, "runtime": runtime})
        for k, v in changes.items():
            setattr(env, k, v)
        db.commit()
        audit.log_event(db, principal.name, "env_update", project.id, env.id, {"name": name, "changes": changes})
        return _view(env)
    finally:
        db.close()


@router.delete("/{name}")
async def delete_environment(slug: str, name: str, principal: Principal = Depends(require("projects:manage"))):
    """Удаляет среду, её релизы и секреты этой среды из платформы (ресурсы в кластере не трогает). Аудит сохраняется."""
    db = SessionLocal()
    try:
        project = _project(db, slug)
        env = project_repo.get_environment(db, project, name)
        if not env:
            raise HTTPException(status_code=404, detail=f"unknown environment {name}")
        if len(project.environments) <= 1:
            raise HTTPException(status_code=409, detail="cannot delete the last environment of a project (delete the project instead)")
        envs_svc.remove_environment(db, project, env)
        audit.log_event(db, principal.name, "env_delete", project.id, detail={"name": name})
        return {"deleted": name}
    finally:
        db.close()


class DomainBody(BaseModel):
    host: Optional[str] = Field(default=None, max_length=253)           # пусто или null — убрать домен


def _kube_env(db, slug: str, name: str):
    project = project_repo.get_project_by_slug(db, slug)
    if not project:
        raise HTTPException(status_code=404, detail=f"unknown project {slug}")
    env = project_repo.get_environment(db, project, name)
    if not env:
        raise HTTPException(status_code=404, detail=f"unknown environment {name} for project {slug}")
    if env.server:
        raise HTTPException(status_code=422, detail="domain of a server target is configured on the server itself")
    return project, env


@router.get("/{name}/domain")
async def get_domain(slug: str, name: str, principal: Principal = Depends(require("read"))):
    db = SessionLocal()
    try:
        _, env = _kube_env(db, slug, name)
        try:
            host = await run_in_threadpool(provision.get_ingress_host, env.namespace, env.deployment_name, env.cluster)
            err = None
        except Exception as e:                                           # кластер недоступен/нет права — интерфейс покажет причину
            host, err = None, f"{type(e).__name__}"
        return {"host": host, "tls": settings.ingress_tls, "error": err}
    finally:
        db.close()


@router.put("/{name}/domain")
async def set_domain(slug: str, name: str, body: DomainBody, principal: Principal = Depends(require("projects:manage"))):
    """Привязывает приложение к домену: создаёт, меняет или убирает Ingress (HTTPS по настройке платформы)."""
    host = (body.host or "").strip().lower() or None
    if host and (not manifests.HOST_NAME.match(host) or host.startswith("*")):
        raise HTTPException(status_code=422, detail="host must be a plain DNS name like app.example.com")
    db = SessionLocal()
    try:
        project, env = _kube_env(db, slug, name)
        try:
            cur_host, cur_secret = await run_in_threadpool(provision.get_ingress_tls, env.namespace, env.deployment_name, env.cluster)
            mine = certs.secret_name(env)
            keep = cur_secret if (host and cur_host == host and cur_secret == mine) else None      # тот же домен: свой сертификат не сбрасываем
            res = await run_in_threadpool(provision.set_ingress, env.namespace, env.deployment_name, host, env.cluster, keep)
            if cur_secret == mine and not keep:
                await run_in_threadpool(certs.delete_secret, env.namespace, mine, env.cluster)      # домен сменён или убран: старый ключ из кластера удаляем
                res["custom_certificate_reset"] = True
        except provision.ProvisionDenied as e:
            raise HTTPException(status_code=403, detail=f"the platform has no permission to manage {e.what}: enable provisioner.enabled (or rbac.manageIngress) in the Helm values")
        except provision.ProvisionError as e:
            raise HTTPException(status_code=502, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"cluster is unavailable ({type(e).__name__})")
        audit.log_event(db, principal.name, "domain_set", project.id, env.id, detail={"host": host, "result": res["result"]})
        scheme = "https" if settings.ingress_tls in ("auto", "custom") else "http"
        out = {"host": host, "result": res["result"], "tls": settings.ingress_tls, "url": f"{scheme}://{host}" if host else None}
        if res.get("custom_certificate_reset"):
            out["custom_certificate_reset"] = True          # поле только при сбросе: прежний вид ответа не меняется
        return out
    finally:
        db.close()


class CertBody(BaseModel):
    certificate: str = Field(min_length=1, max_length=70000)          # цепочка PEM: сначала сертификат сервера, затем промежуточные
    private_key: str = Field(min_length=1, max_length=20000)          # закрытый ключ PEM без пароля


def _cert_errors(e: Exception):
    if isinstance(e, certs.CertError):
        status = 409 if e.code == "secret_not_managed" else 422
        return HTTPException(status_code=status, detail={"error": e.code, "message": str(e)})
    if isinstance(e, provision.ProvisionDenied):
        return HTTPException(status_code=403, detail=f"the platform has no permission to manage {e.what}: enable provisioner.enabled (or rbac.manageIngress) in the Helm values")
    if isinstance(e, ApiException):
        if e.status == 403:
            return HTTPException(status_code=403, detail="the platform has no permission to manage secrets of the application namespace")
        return HTTPException(status_code=502, detail=f"kubernetes {e.status}: {e.reason}")
    if isinstance(e, provision.ProvisionError):
        return HTTPException(status_code=502, detail=str(e))
    return HTTPException(status_code=502, detail=f"cluster is unavailable ({type(e).__name__})")


@router.get("/{name}/domain/certificate")
async def get_domain_certificate(slug: str, name: str, principal: Principal = Depends(require("read"))):
    """Какой сертификат у домена: свой (сведения о нём, ключ не отдаётся), платформы (Let's Encrypt / общий) или HTTPS нет."""
    db = SessionLocal()
    try:
        _, env = _kube_env(db, slug, name)
        try:
            host, secret = await run_in_threadpool(provision.get_ingress_tls, env.namespace, env.deployment_name, env.cluster)
            if host and secret and secret == certs.secret_name(env):
                info = await run_in_threadpool(certs.read_info, env.namespace, secret, host, env.cluster)
                if info:
                    return {"host": host, "source": "custom", "secret": secret, **info}
        except Exception as e:
            return {"host": None, "source": "unknown", "error": f"{type(e).__name__}"}
        return {"host": host, "source": "platform" if host and settings.ingress_tls in ("auto", "custom") else "none", "mode": settings.ingress_tls}
    finally:
        db.close()


@router.put("/{name}/domain/certificate")
async def put_domain_certificate(slug: str, name: str, body: CertBody, principal: Principal = Depends(require("projects:manage"))):
    """Свой сертификат для домена приложения. Цепочка и ключ проверяются (пара, имя, срок, тип ключа), ключ хранится только в кластере."""
    db = SessionLocal()
    try:
        project, env = _kube_env(db, slug, name)
        try:
            host, _ = await run_in_threadpool(provision.get_ingress_tls, env.namespace, env.deployment_name, env.cluster)
            if not host:
                raise HTTPException(status_code=409, detail="set the domain of the environment first, then upload its certificate")
            info, chain_pem, key_pem = certs.validate(body.certificate, body.private_key, host)
            sname = certs.secret_name(env)
            state = await run_in_threadpool(certs.apply_secret, env.namespace, sname, chain_pem, key_pem, env.cluster)
            await run_in_threadpool(provision.set_ingress, env.namespace, env.deployment_name, host, env.cluster, sname)
        except HTTPException:
            raise
        except Exception as e:
            raise _cert_errors(e)
        audit.log_event(db, principal.name, "domain_certificate_set", project.id, env.id,
                        detail={"host": host, "sha256": info["sha256"], "not_after": info["not_after"], "secret": state})
        return {"host": host, "source": "custom", "secret": sname, "result": state, **info}
    finally:
        db.close()


@router.delete("/{name}/domain/certificate")
async def delete_domain_certificate(slug: str, name: str, principal: Principal = Depends(require("projects:manage"))):
    """Возвращает домену сертификат платформы (по настройке) и удаляет свой ключ из кластера."""
    db = SessionLocal()
    try:
        project, env = _kube_env(db, slug, name)
        try:
            host, secret = await run_in_threadpool(provision.get_ingress_tls, env.namespace, env.deployment_name, env.cluster)
            if not host or secret != certs.secret_name(env):
                return {"host": host, "result": "none"}
            await run_in_threadpool(provision.set_ingress, env.namespace, env.deployment_name, host, env.cluster)
            await run_in_threadpool(certs.delete_secret, env.namespace, secret, env.cluster)
        except Exception as e:
            raise _cert_errors(e)
        audit.log_event(db, principal.name, "domain_certificate_removed", project.id, env.id, detail={"host": host})
        return {"host": host, "result": "removed"}
    finally:
        db.close()
