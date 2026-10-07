import logging
import time
from typing import Optional

from kubernetes.client.exceptions import ApiException

from app.config import settings
from app.services.kubeclient import custom_objects_api

logger = logging.getLogger(__name__)

GROUP = "kpack.io"
VERSION = "v1alpha2"
PLURAL = "images"


def _image_body(project_slug: str, git_url: str, revision: str, sub_path: str, namespace: str) -> dict:
    tag = f"{settings.registry_prefix}/{project_slug}"
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "Image",
        "metadata": {"name": project_slug, "namespace": namespace},
        "spec": {
            "tag": tag,
            "serviceAccountName": "default",
            "builder": {"name": settings.default_builder, "kind": "ClusterBuilder"},
            "source": {
                "git": {"url": git_url, "revision": revision},
                "subPath": sub_path,
            },
        },
    }


def ensure_git_credentials(project_slug: str, git_url: str, username: str, token: str, namespace: str = None) -> None:
    """Секрет basic-auth с аннотацией kpack.io/git, подключённый к ServiceAccount сборки: kpack берёт из него доступ к приватному репозиторию."""
    from urllib.parse import urlparse
    from kubernetes import client
    from app.services import kubeclient
    namespace = namespace or settings.default_namespace
    u = urlparse(git_url)
    name = f"git-{project_slug}"[:63]
    body = client.V1Secret(metadata=client.V1ObjectMeta(name=name, namespace=namespace, annotations={"kpack.io/git": f"{u.scheme}://{u.hostname}"},
                                                        labels={"app.kubernetes.io/managed-by": "splitwave"}),
                           type="kubernetes.io/basic-auth", string_data={"username": username, "password": token})
    core = kubeclient.core_v1_api()
    try:
        core.create_namespaced_secret(namespace, body)
    except ApiException as e:
        if e.status != 409:
            raise
        core.replace_namespaced_secret(name, namespace, body)
    sa = core.read_namespaced_service_account("default", namespace)
    if not any(s.name == name for s in (sa.secrets or [])):
        core.patch_namespaced_service_account("default", namespace, {"secrets": [{"name": s.name} for s in (sa.secrets or [])] + [{"name": name}]})


def trigger_build(project_slug: str, git_url: str, revision: str, sub_path: str, namespace: str = None) -> bool:
    """Возвращает True, если объект kpack изменился (будет новая сборка); False — та же ревизия и настройки, сборки не будет."""
    namespace = namespace or settings.default_namespace
    api = custom_objects_api()
    body = _image_body(project_slug, git_url, revision, sub_path, namespace)
    try:
        existing = api.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, project_slug)
    except ApiException as e:
        if e.status != 404:
            raise
        api.create_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, body)
        logger.info("Created Image %s/%s", namespace, project_slug)
        return True

    if existing["spec"]["tag"] != body["spec"]["tag"]:
        # kpack запрещает менять spec.tag (например, при переезде на другой реестр): пересоздаём Image.
        # Кэш сборки теряется, зато следующая сборка сразу публикуется по новому адресу.
        logger.info("Image %s/%s: tag changed %s -> %s, recreating", namespace, project_slug,
                    existing["spec"]["tag"], body["spec"]["tag"])
        api.delete_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, project_slug)
        _wait_deleted(api, project_slug, namespace)
        api.create_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, body)
        return True

    same = (existing["spec"].get("source", {}).get("git", {}).get("revision") == revision
            and existing["spec"].get("source", {}).get("subPath", "") == (sub_path or "")
            and (existing["spec"].get("builder") or {}).get("name") == body["spec"]["builder"]["name"])
    if same:
        logger.info("Image %s/%s already targets revision %s — no new build", namespace, project_slug, revision)
        return False

    body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
    api.replace_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, project_slug, body)
    logger.info("Updated Image %s/%s", namespace, project_slug)
    return True


def _wait_deleted(api, project_slug: str, namespace: str, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            api.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, project_slug)
        except ApiException as e:
            if e.status == 404:
                return
            raise
        time.sleep(1)
    raise TimeoutError(f"Image {namespace}/{project_slug} was not deleted within {timeout}s")


def get_status(project_slug: str, namespace: str = None) -> dict:
    namespace = namespace or settings.default_namespace
    api = custom_objects_api()
    try:
        obj = api.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, project_slug)
    except ApiException as e:
        if e.status == 404:
            return {"latest_image": None, "ready": False}
        raise
    status = obj.get("status", {})
    conditions = {c["type"]: c for c in status.get("conditions", [])}
    return {
        "latest_image": status.get("latestImage"),
        "ready": conditions.get("Ready", {}).get("status") == "True",
    }


def wait_for_new_build(project_slug: str, previous_image: Optional[str], namespace: str = None,
                        timeout: int = None, interval: int = None) -> str:
    namespace = namespace or settings.default_namespace
    timeout = timeout or settings.build_timeout_seconds
    interval = interval or settings.build_poll_interval_seconds
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = get_status(project_slug, namespace)
        if st["ready"] and st["latest_image"] and st["latest_image"] != previous_image:
            return st["latest_image"]
        time.sleep(interval)
    raise TimeoutError(f"Build for {project_slug} did not complete within {timeout}s")


def available() -> bool:
    """Установлен ли в кластере платформы kpack (сборка через buildpacks). Не удалось проверить — считаем, что установлен, чтобы не блокировать зря."""
    try:
        custom_objects_api().list_cluster_custom_object(GROUP, VERSION, PLURAL, limit=1)
        return True
    except ApiException as e:
        return e.status != 404
    except Exception:
        return True
