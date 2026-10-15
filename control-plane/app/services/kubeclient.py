import logging
import time

import urllib3
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

_loaded = False


def _ensure_loaded():
    global _loaded
    if _loaded:
        return
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    _loaded = True


def _remote(cluster: str) -> client.ApiClient:
    from app import plugins
    if plugins.CLUSTER_PROVIDER is None:
        raise RuntimeError(f"cluster {cluster} is unavailable: multi-cluster support is not licensed or not installed")
    return plugins.CLUSTER_PROVIDER(cluster)


def custom_objects_api() -> client.CustomObjectsApi:
    """Сборки kpack всегда в домашнем кластере."""
    _ensure_loaded()
    return client.CustomObjectsApi()


def apps_v1_api(cluster=None) -> client.AppsV1Api:
    if cluster:
        return client.AppsV1Api(_remote(cluster))
    _ensure_loaded()
    return client.AppsV1Api()


def core_v1_api(cluster=None) -> client.CoreV1Api:
    if cluster:
        return client.CoreV1Api(_remote(cluster))
    _ensure_loaded()
    return client.CoreV1Api()


def serialize(obj) -> dict:
    return client.ApiClient().sanitize_for_serialization(obj)


def batch_v1_api(cluster=None) -> client.BatchV1Api:
    """Задания сборки по Dockerfile (kaniko) идут в домашнем кластере; задания сервисов данных — в кластере среды."""
    if cluster:
        return client.BatchV1Api(_remote(cluster))
    _ensure_loaded()
    return client.BatchV1Api()


def rbac_v1_api(cluster=None) -> client.RbacAuthorizationV1Api:
    if cluster:
        return client.RbacAuthorizationV1Api(_remote(cluster))
    _ensure_loaded()
    return client.RbacAuthorizationV1Api()


def networking_v1_api(cluster=None) -> client.NetworkingV1Api:
    if cluster:
        return client.NetworkingV1Api(_remote(cluster))
    _ensure_loaded()
    return client.NetworkingV1Api()


def authorization_v1_api(cluster=None) -> client.AuthorizationV1Api:
    if cluster:
        return client.AuthorizationV1Api(_remote(cluster))
    _ensure_loaded()
    return client.AuthorizationV1Api()


logger = logging.getLogger(__name__)
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
API_RETRIES = 12   # подряд идущих сбоев API (~минута при опросе раз в 5 с), после которых ошибка пробрасывается


def is_transient(e: Exception) -> bool:
    """Кратковременный сбой API кластера: перегрузка или таймаут шлюза, обрыв соединения (k3s на одном слабом узле)."""
    if isinstance(e, ApiException):
        return e.status in TRANSIENT_STATUSES
    return isinstance(e, urllib3.exceptions.HTTPError)


def call_retrying(call, wait: float = 5.0, retries: int = None, sleep=time.sleep):
    """Вызов API кластера с повтором при кратковременных сбоях; постоянная ошибка (403, 404…) пробрасывается сразу.
    Для циклов ожидания (сборка, выкат): один 504 не должен ронять длинную операцию."""
    retries = API_RETRIES if retries is None else retries
    for attempt in range(retries):
        try:
            return call()
        except Exception as e:  # noqa: BLE001
            if not is_transient(e) or attempt == retries - 1:
                raise
            logger.warning("transient cluster API error (%s), retry %d/%d", getattr(e, "status", type(e).__name__), attempt + 1, retries)
            sleep(wait)
