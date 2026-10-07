"""Создание ресурсов приложения в кластере (если у платформы есть права) и проверка этих прав.

Платформа по умолчанию работает с минимальными правами и ничего не создаёт: оператор включает создание отдельным манифестом
deploy/platform/provisioner-rbac.yaml. Пока прав нет, мастер отдаёт YAML для ручного применения. Существующие объекты
никогда не перезаписываются: платформа только создаёт то, чего ещё нет."""
import logging
from typing import Optional

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from app.config import settings
from app.services import kubeclient, manifests

logger = logging.getLogger(__name__)

def _checks() -> list:
    return [("", "namespaces", "create", None), ("rbac.authorization.k8s.io", "rolebindings", "create", None),
            ("apps", "deployments", "create", None), ("", "services", "create", None),
            ("rbac.authorization.k8s.io", "clusterroles", "bind", settings.deployer_cluster_role)]


INGRESS_CHECK = ("networking.k8s.io", "ingresses", "create", None)


class ProvisionDenied(Exception):
    """У платформы нет права создать объект: нужен provisioner-rbac.yaml или ручное применение манифеста."""

    def __init__(self, what: str):
        super().__init__(f"no permission to create {what}")
        self.what = what


class ProvisionError(Exception):
    pass


def check_access(cluster: Optional[str] = None, ingress: bool = False) -> dict:
    """{allowed, missing[]}: SelfSubjectAccessReview по каждому нужному праву (без побочных эффектов)."""
    missing = []
    try:
        api = kubeclient.authorization_v1_api(cluster)
        for group, resource, verb, name in _checks() + ([INGRESS_CHECK] if ingress else []):
            attrs = client.V1ResourceAttributes(group=group, resource=resource, verb=verb, name=name)
            r = api.create_self_subject_access_review(client.V1SelfSubjectAccessReview(spec=client.V1SelfSubjectAccessReviewSpec(resource_attributes=attrs)))
            if not (r.status and r.status.allowed):
                missing.append(f"{verb} {resource}" + (f"/{name}" if name else ""))
    except Exception as e:                       # кластер недоступен или нет права даже на проверку
        logger.warning("access review failed: %s", e)
        return {"allowed": False, "missing": ["access review unavailable"]}
    return {"allowed": not missing, "missing": missing}


def _create(kind: str, name: str, call) -> dict:
    try:
        call()
        return {"kind": kind, "name": name, "result": "created"}
    except ApiException as e:
        if e.status == 409:
            return {"kind": kind, "name": name, "result": "exists"}
        if e.status == 403:
            raise ProvisionDenied(f"{kind} {name}")
        raise ProvisionError(f"{kind} {name}: {e.reason or e.status}")


def copy_pull_secret(namespace: str, cluster: Optional[str]) -> Optional[dict]:
    """Копирует секрет доступа к реестру (settings.app_pull_secret) из namespace платформы в namespace приложения."""
    if not settings.app_pull_secret:
        return None
    src = kubeclient.core_v1_api().read_namespaced_secret(settings.app_pull_secret, manifests.platform_namespace())
    body = client.V1Secret(metadata=client.V1ObjectMeta(name=settings.app_pull_secret, namespace=namespace, labels=manifests.LABEL_MANAGED),
                           type=src.type, data=src.data)
    return _create("Secret", settings.app_pull_secret, lambda: kubeclient.core_v1_api(cluster).create_namespaced_secret(namespace, body))


def apply(docs: list[dict], cluster: Optional[str] = None) -> list[dict]:
    """Создаёт объекты по порядку. ProvisionDenied — нет прав; ProvisionError — другая ошибка кластера."""
    core, apps = kubeclient.core_v1_api(cluster), kubeclient.apps_v1_api(cluster)
    out = []
    for d in docs:
        kind, meta = d["kind"], d["metadata"]
        ns, name = meta.get("namespace"), meta["name"]
        if kind == "Namespace":
            out.append(_create(kind, name, lambda d=d: core.create_namespace(d)))
        elif kind == "RoleBinding":
            out.append(_create(kind, name, lambda d=d, ns=ns: kubeclient.rbac_v1_api(cluster).create_namespaced_role_binding(ns, d)))
            copied = copy_pull_secret(ns, cluster)
            if copied:
                out.append(copied)
        elif kind == "Deployment":
            out.append(_create(kind, name, lambda d=d, ns=ns: apps.create_namespaced_deployment(ns, d)))
        elif kind == "Service":
            out.append(_create(kind, name, lambda d=d, ns=ns: core.create_namespaced_service(ns, d)))
        elif kind == "Ingress":
            out.append(_create(kind, name, lambda d=d, ns=ns: kubeclient.networking_v1_api(cluster).create_namespaced_ingress(ns, d)))
        else:
            raise ProvisionError(f"unsupported kind {kind}")
    return out


def _service_port(core, namespace: str, name: str) -> dict:
    """Порт Service для backend Ingress: по имени (если задано) или по номеру первого порта."""
    try:
        svc = core.read_namespaced_service(name, namespace)
    except ApiException as e:
        if e.status == 404:
            raise ProvisionError(f"service {name} not found in namespace {namespace}: create the application first")
        if e.status == 403:
            raise ProvisionDenied(f"service {name}")
        raise ProvisionError(f"service {name}: {e.reason or e.status}")
    ports = svc.spec.ports or []
    if not ports:
        raise ProvisionError(f"service {name} has no ports")
    return {"name": ports[0].name} if ports[0].name else {"number": ports[0].port}


def get_ingress_host(namespace: str, name: str, cluster: Optional[str] = None) -> Optional[str]:
    try:
        ing = kubeclient.networking_v1_api(cluster).read_namespaced_ingress(name, namespace)
    except ApiException as e:
        if e.status == 404:
            return None
        raise
    rules = (ing.spec.rules or []) if ing.spec else []
    return rules[0].host if rules else None


def get_ingress_tls(namespace: str, name: str, cluster: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """(домен, имя Secret с сертификатом) Ingress приложения; (None, None) — Ingress нет."""
    try:
        ing = kubeclient.networking_v1_api(cluster).read_namespaced_ingress(name, namespace)
    except ApiException as e:
        if e.status == 404:
            return None, None
        if e.status == 403:
            raise ProvisionDenied(f"ingress {name}")
        raise ProvisionError(f"ingress {name}: {e.reason or e.status}")
    rules = (ing.spec.rules or []) if ing.spec else []
    tls = (getattr(ing.spec, "tls", None) or []) if ing.spec else []
    return (rules[0].host if rules else None), (getattr(tls[0], "secret_name", None) if tls else None)


def set_ingress(namespace: str, service_name: str, host: Optional[str], cluster: Optional[str] = None, tls_secret: Optional[str] = None) -> dict:
    """Создаёт, заменяет или удаляет Ingress приложения (имя = имя Service). host=None — убрать домен. Для чужих Ingress с другим именем ничего не трогает."""
    from app.services import manifests
    net = kubeclient.networking_v1_api(cluster)
    try:
        existing = net.read_namespaced_ingress(service_name, namespace)
    except ApiException as e:
        if e.status == 404:
            existing = None
        elif e.status == 403:
            raise ProvisionDenied(f"ingress {service_name}")
        else:
            raise ProvisionError(f"ingress {service_name}: {e.reason or e.status}")
    try:
        if host is None:
            if existing is None:
                return {"result": "none"}
            net.delete_namespaced_ingress(service_name, namespace)
            return {"result": "removed"}
        core = kubeclient.core_v1_api(cluster)
        doc = manifests.ingress_doc(name=service_name, namespace=namespace, host=host, service_port=_service_port(core, namespace, service_name), tls_secret=tls_secret)
        if existing is None:
            net.create_namespaced_ingress(namespace, doc)
            return {"result": "created"}
        doc["metadata"]["resourceVersion"] = existing.metadata.resource_version
        doc["metadata"]["labels"] = {**(existing.metadata.labels or {}), **doc["metadata"]["labels"]}
        net.replace_namespaced_ingress(service_name, namespace, doc)
        return {"result": "updated"}
    except ApiException as e:
        if e.status == 403:
            raise ProvisionDenied(f"ingress {service_name}")
        raise ProvisionError(f"ingress {service_name}: {e.reason or e.status}")
