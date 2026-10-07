import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import PlainTextResponse
from kubernetes.client.exceptions import ApiException

from app.auth import Principal, require
from app.db.base import SessionLocal
from app.services import project_repo, runtime, targets
from app.services import secrets as secrets_svc

router = APIRouter(prefix="/api/projects/{slug}", tags=["runtime"])


def _env(db, slug: str, environment: str):
    project = project_repo.get_project_by_slug(db, slug)
    if not project:
        raise HTTPException(status_code=404, detail=f"unknown project {slug}")
    env = project_repo.get_environment(db, project, environment)
    if not env:
        raise HTTPException(status_code=404, detail=f"unknown environment {environment} for project {slug}")
    return project, env


def _cluster_error(e: Exception) -> dict:
    return {"available": False, "error": f"{type(e).__name__}: {e}"[:300]}


@router.get("/runtime")
async def runtime_status(slug: str, environment: str = "prod", principal: Principal = Depends(require("read"))):
    db = SessionLocal()
    try:
        _, env = _env(db, slug, environment)
        if env.server:
            try:
                return {"available": True, **targets.server_runtime(env)}
            except Exception as e:
                return _cluster_error(e)
        try:
            return {"available": True, **runtime.deployment_runtime(env.namespace, env.deployment_name, env.container_name, env.cluster)}
        except ApiException as e:
            return {"available": False, "error": f"kubernetes {e.status}: {e.reason}"}
        except Exception as e:  # кластер недоступен и т.п. — интерфейс покажет причину
            return _cluster_error(e)
    finally:
        db.close()


@router.get("/builds")
async def builds(slug: str, environment: str = "prod", principal: Principal = Depends(require("read"))):
    db = SessionLocal()
    try:
        project, env = _env(db, slug, environment)
        try:
            return {"available": True, "builds": runtime.list_builds(env.image_name, env.build_namespace)}
        except ApiException as e:
            return {"available": False, "builds": [], "error": f"kubernetes {e.status}: {e.reason}"}
        except Exception as e:
            return {**_cluster_error(e), "builds": []}
    finally:
        db.close()


@router.get("/builds/{build}/logs", response_class=PlainTextResponse)
async def build_logs(slug: str, build: str, environment: str = "prod", principal: Principal = Depends(require("deploy"))):
    db = SessionLocal()
    try:
        project, env = _env(db, slug, environment)
        try:
            return PlainTextResponse(runtime.build_logs(env.image_name, env.build_namespace, build), headers={"Cache-Control": "no-store"})
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid build name")
        except ApiException as e:
            if e.status == 404:
                raise HTTPException(status_code=404, detail="build pod not found (logs are kept only while the pod exists)")
            raise HTTPException(status_code=502, detail=f"kubernetes {e.status}: {e.reason}")
    finally:
        db.close()


POD_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")


@router.get("/pods/{pod}/logs", response_class=PlainTextResponse)
async def pod_logs(slug: str, pod: str, response: Response, environment: str = "prod", tail: int = 300, previous: bool = False,
                   principal: Principal = Depends(require("deploy"))):
    """Лог пода приложения (для разбора «не запускается»). Значения секретов проекта в тексте скрываются."""
    if not POD_NAME.match(pod) or not 1 <= tail <= 2000:
        raise HTTPException(status_code=422, detail="invalid pod name or tail (1..2000)")
    db = SessionLocal()
    try:
        project, env = _env(db, slug, environment)
        if env.server:
            raise HTTPException(status_code=422, detail="logs of server targets are not available yet: use docker logs on the server")
        values = secrets_svc.decrypt_all(db, project, env.name).values()
        try:
            text = runtime.pod_logs(env.namespace, env.deployment_name, env.container_name, pod, tail, previous, env.cluster)
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ApiException as e:
            raise HTTPException(status_code=502, detail=f"kubernetes {e.status}: {e.reason}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"cluster is unavailable ({type(e).__name__})")
        response.headers["Cache-Control"] = "no-store"
        return runtime.mask_secrets(text, values)
    finally:
        db.close()
