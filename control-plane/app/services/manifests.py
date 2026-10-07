"""Манифесты приложения для мастера подключения: namespace, доступ платформы, Deployment, Service и (по желанию) Ingress.

Строятся одной функцией и используются двумя путями: платформа создаёт их сама (если оператор выдал ей права, см.
deploy/platform/provisioner-rbac.yaml) или показывает администратору как YAML для `kubectl apply`. Результат одинаков.
Секреты в манифесты не попадают никогда."""
import re
from typing import Optional

import json

import yaml

from app.config import settings

DNS_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
HOST_NAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*$")
RESERVED = {"kube-system", "kube-public", "kube-node-lease"}
# Минимум, без которого не запускаются обычные образы: веб-серверы (nginx, apache) стартуют от root, меняют владельца своих каталогов
# (CHOWN/FOWNER), сбрасывают права рабочим процессам (SETUID/SETGID) и слушают порт 80 (NET_BIND_SERVICE). Остальное сброшено.
APP_CAPABILITIES = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID", "NET_BIND_SERVICE")
LABEL_MANAGED = {"app.kubernetes.io/managed-by": "splitwave"}


def platform_namespace() -> str:
    """Где работает платформа: настройка, иначе namespace её ServiceAccount, иначе default_namespace."""
    if settings.platform_namespace:
        return settings.platform_namespace
    try:
        return open("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read().strip() or settings.default_namespace
    except OSError:
        return settings.default_namespace


def check_names(name: str, namespace: str, container: str, port: int, host: Optional[str]) -> None:
    """ValueError с понятным текстом, если параметры недопустимы."""
    for label, v in (("name", name), ("namespace", namespace), ("container", container)):
        if not DNS_NAME.match(v or ""):
            raise ValueError(f"{label}: lowercase letters, digits and '-', up to 63 characters")
    if namespace in RESERVED or namespace.startswith("kube-"):
        raise ValueError(f"namespace {namespace} is reserved for Kubernetes")
    if namespace in {platform_namespace(), settings.build_namespace or settings.default_namespace}:
        raise ValueError(f"namespace {namespace} is used by the platform itself: choose another one for the application")
    if not (1 <= int(port) <= 65535):
        raise ValueError("port must be 1-65535")
    if host and (not HOST_NAME.match(host) or host.startswith("*")):
        raise ValueError("host must be a plain DNS name like app.example.com")


def _extra_annotations() -> dict:
    raw = (settings.ingress_annotations or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def ingress_doc(*, name: str, namespace: str, host: str, labels: Optional[dict] = None, service_port: Optional[dict] = None,
                tls_secret: Optional[str] = None) -> dict:
    """Ingress приложения. Параметры HTTPS берутся из настроек платформы: auto — сертификат Let's Encrypt (резолвер Traefik), custom — свой сертификат."""
    if not HOST_NAME.match(host) or host.startswith("*"):
        raise ValueError("host must be a plain DNS name like app.example.com")
    mode = "custom" if tls_secret else settings.ingress_tls        # свой сертификат домена (Secret kubernetes.io/tls): без резолвера ACME
    ann: dict = {}
    if mode in ("auto", "custom"):
        ann.update({"traefik.ingress.kubernetes.io/router.entrypoints": "websecure", "traefik.ingress.kubernetes.io/router.tls": "true"})
        if mode == "auto":
            ann["traefik.ingress.kubernetes.io/router.tls.certresolver"] = settings.ingress_cert_resolver
    ann.update(_extra_annotations())
    meta = {"name": name, "namespace": namespace, "labels": labels or {"app.kubernetes.io/name": name, **LABEL_MANAGED}}
    if ann:
        meta["annotations"] = ann
    ing = {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": meta,
           "spec": {"rules": [{"host": host, "http": {"paths": [{"path": "/", "pathType": "Prefix",
                                                                "backend": {"service": {"name": name, "port": service_port or {"name": "http"}}}}]}}]}}
    if settings.ingress_class:
        ing["spec"]["ingressClassName"] = settings.ingress_class
    if mode in ("auto", "custom"):
        tls = {"hosts": [host]}
        if tls_secret or settings.ingress_tls_secret:
            tls["secretName"] = tls_secret or settings.ingress_tls_secret
        ing["spec"]["tls"] = [tls]
    return ing


def build(*, name: str, namespace: str, container: str, port: int, host: Optional[str] = None,
          image: Optional[str] = None) -> list[dict]:
    check_names(name, namespace, container, port, host)
    labels = {"app.kubernetes.io/name": name, **LABEL_MANAGED}
    docs = [
        {"apiVersion": "v1", "kind": "Namespace",
         "metadata": {"name": namespace, "labels": {**LABEL_MANAGED, "pod-security.kubernetes.io/enforce": "baseline"}}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
         "metadata": {"name": settings.deployer_cluster_role, "namespace": namespace, "labels": LABEL_MANAGED},
         "subjects": [{"kind": "ServiceAccount", "name": settings.platform_service_account, "namespace": platform_namespace()}],
         "roleRef": {"kind": "ClusterRole", "name": settings.deployer_cluster_role, "apiGroup": "rbac.authorization.k8s.io"}},
    ]
    pod = {
        "labels": labels,
        "spec": {
            "automountServiceAccountToken": False,
            "containers": [{
                "name": container, "image": image or settings.placeholder_image,
                "ports": [{"name": "http", "containerPort": int(port)}],
                "env": [{"name": "PORT", "value": str(port)}],
                "readinessProbe": {"tcpSocket": {"port": "http"}, "initialDelaySeconds": 5, "periodSeconds": 10},
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "512Mi"}},
                "securityContext": {"allowPrivilegeEscalation": False, "seccompProfile": {"type": "RuntimeDefault"},
                                    "capabilities": {"drop": ["ALL"], "add": list(APP_CAPABILITIES)}},
            }],
        },
    }
    if settings.app_pull_secret:
        pod["spec"]["imagePullSecrets"] = [{"name": settings.app_pull_secret}]
    docs.append({"apiVersion": "apps/v1", "kind": "Deployment",
                 "metadata": {"name": name, "namespace": namespace, "labels": labels},
                 "spec": {"replicas": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": name}}, "template": {"metadata": {"labels": labels}, "spec": pod["spec"]}}})
    docs.append({"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": namespace, "labels": labels},
                 "spec": {"selector": {"app.kubernetes.io/name": name}, "ports": [{"name": "http", "port": int(port), "targetPort": "http"}]}})
    if host:
        docs.append(ingress_doc(name=name, namespace=namespace, host=host, labels=labels))
    return docs


def to_yaml(docs: list[dict]) -> str:
    return yaml.safe_dump_all(docs, sort_keys=False, allow_unicode=True)
