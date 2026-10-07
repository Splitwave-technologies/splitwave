"""Куда и как выкатывается среда: кластер Kubernetes (по умолчанию) или сервер с агентом. Единая точка для выката, отката, секретов и состояния."""
from typing import Optional

from fastapi import HTTPException

from app import plugins
from app.config import settings
from app.licensing import current_license
from app.services import deploy as deploy_mod
from app.services import runtime_spec
from app.services import secrets as secrets_svc


def _server_target():
    if plugins.SERVER_TARGET is None:
        raise RuntimeError("servers are unavailable: the feature is not licensed or not installed")
    return plugins.SERVER_TARGET


def normalize_server(name: Optional[str], cluster: Optional[str], spec: Optional[dict]) -> tuple[Optional[str], Optional[dict]]:
    """Проверка при создании/изменении среды: сервер должен существовать и быть разрешён лицензией; параметры контейнера — строгая схема."""
    if not name or name == "local":
        if spec:
            raise HTTPException(status_code=422, detail="runtime applies to server environments only")
        return None, None
    if "servers" not in current_license().features:
        raise HTTPException(status_code=403, detail={"error": "feature_not_licensed", "feature": "servers"})
    if cluster:
        raise HTTPException(status_code=422, detail="an environment targets either a cluster or a server, not both")
    if plugins.SERVER_TARGET is None or name not in plugins.SERVER_TARGET.names():
        raise HTTPException(status_code=422, detail=f"unknown server {name}")
    try:
        return name, runtime_spec.validate(spec)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


def deploy(db, project, env, image: str, sync_secrets: bool = True) -> None:
    """Выкатывает образ в среду. Для сервера ждёт отчёта агента (исключение — выкат не удался)."""
    if env.server:
        _server_target().deploy(db, project, env, image)
        return
    if sync_secrets:
        secrets_svc.sync_to_cluster(db, project, env)   # секреты сначала: под с новым образом уже получает актуальные значения
    deploy_mod.deploy_image(env.deployment_name, env.container_name, image, env.namespace, env.cluster)
    if settings.rollout_wait_enabled:       # релиз считается успешным, только когда приложение поднялось (иначе «deployed» при CrashLoop)
        values = secrets_svc.decrypt_all(db, project, env.name).values()
        deploy_mod.wait_rollout(env.deployment_name, env.container_name, image, env.namespace, env.cluster, mask_values=values)


def sync_secrets(db, project, env) -> int:
    if env.server:
        return _server_target().sync_secrets(db, project, env)
    return secrets_svc.sync_to_cluster(db, project, env)


def server_runtime(env) -> dict:
    return _server_target().runtime(env)
