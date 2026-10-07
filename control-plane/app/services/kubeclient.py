from kubernetes import client, config

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


def batch_v1_api() -> client.BatchV1Api:
    """Задания сборки по Dockerfile (kaniko) всегда в домашнем кластере."""
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
