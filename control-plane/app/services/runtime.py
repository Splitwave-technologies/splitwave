import logging
"""Состояние приложения в кластере и сборки kpack: только чтение."""
import re
from typing import Optional

from kubernetes.client.exceptions import ApiException

from app.services import kubeclient

logger = logging.getLogger(__name__)

GROUP, VERSION = "kpack.io", "v1alpha2"
BUILD_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
MAX_LOG_BYTES = 200_000


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def deployment_runtime(namespace: str, deployment_name: str, container_name: str, cluster: Optional[str] = None) -> dict:
    apps, core = (kubeclient.apps_v1_api(cluster), kubeclient.core_v1_api(cluster)) if cluster else (kubeclient.apps_v1_api(), kubeclient.core_v1_api())
    dep = apps.read_namespaced_deployment(deployment_name, namespace)
    st = dep.status
    image = next((c.image for c in dep.spec.template.spec.containers if c.name == container_name), None)
    selector = dep.spec.selector.match_labels or {}
    pods = core.list_namespaced_pod(namespace, label_selector=",".join(f"{k}={v}" for k, v in selector.items())).items
    out = []
    for p in pods:
        cs = p.status.container_statuses or []
        waiting = next((c.state.waiting.reason for c in cs if c.state and c.state.waiting), None)
        out.append({"name": p.metadata.name, "phase": p.status.phase, "ready": bool(cs) and all(c.ready for c in cs),
                    "restarts": sum(c.restart_count for c in cs), "started": _iso(p.status.start_time), "reason": waiting})
    return {
        "replicas": dep.spec.replicas or 0, "ready": (st.ready_replicas or 0) if st else 0,
        "updated": (st.updated_replicas or 0) if st else 0, "available_replicas": (st.available_replicas or 0) if st else 0,
        "image": image,
        "conditions": [{"type": c.type, "status": c.status, "reason": c.reason} for c in ((st.conditions or []) if st else [])],
        "pods": sorted(out, key=lambda x: x["name"]),
    }


def list_builds(project_slug: str, namespace: str, limit: int = 15) -> list[dict]:
    merged = _list_kpack_builds(project_slug, namespace, limit)
    try:
        from app.services import kaniko
        merged += kaniko.list_builds(project_slug, namespace, limit)
    except ApiException:
        pass                 # нет прав на jobs или задания не используются — показываем только kpack
    except Exception:        # нет доступа к кластеру для заданий и т. п.: сборки kpack всё равно показываем
        logger.warning("kaniko builds are unavailable, showing kpack builds only", exc_info=True)
    merged.sort(key=lambda b: b["created_at"] or "", reverse=True)
    return merged[:limit]


def _list_kpack_builds(project_slug: str, namespace: str, limit: int = 15) -> list[dict]:
    api = kubeclient.custom_objects_api()
    items = api.list_namespaced_custom_object(GROUP, VERSION, namespace, "builds",
                                              label_selector=f"image.kpack.io/image={project_slug}").get("items", [])
    items.sort(key=lambda b: b["metadata"].get("creationTimestamp", ""), reverse=True)
    result = []
    for b in items[:limit]:
        conds = {c["type"]: c for c in (b.get("status") or {}).get("conditions", [])}
        succeeded = conds.get("Succeeded", {})
        status = {"True": "succeeded", "False": "failed"}.get(succeeded.get("status"), "building")
        result.append({
            "name": b["metadata"]["name"], "created_at": b["metadata"].get("creationTimestamp"), "status": status,
            "reason": succeeded.get("reason"), "message": (succeeded.get("message") or "")[:300],
            "revision": (((b.get("spec") or {}).get("source") or {}).get("git") or {}).get("revision"),
            "latest_image": (b.get("status") or {}).get("latestImage"),
        })
    return result


def build_logs(project_slug: str, namespace: str, build_name: str, tail_lines: int = 400) -> str:
    """Логи всех контейнеров сборочного пода kpack (prepare, analyze, detect, restore, build, export...)."""
    if "-kbuild-" in build_name:
        from app.services import kaniko
        return kaniko.build_logs(project_slug, namespace, build_name, tail_lines)
    if not BUILD_NAME.match(build_name) or not build_name.startswith(f"{project_slug}-build-"):
        raise ValueError("invalid build name")
    core = kubeclient.core_v1_api()
    pod_name = f"{build_name}-build-pod"
    pod = core.read_namespaced_pod(pod_name, namespace)
    names = [c.name for c in (pod.spec.init_containers or [])] + [c.name for c in pod.spec.containers]
    parts, size = [], 0
    for name in names:
        try:
            text = core.read_namespaced_pod_log(pod_name, namespace, container=name, tail_lines=tail_lines)
        except ApiException as e:
            text = f"<{name}: unavailable ({e.status})>"
        block = f"=== {name} ===\n{text}\n"
        size += len(block.encode())
        parts.append(block)
        if size > MAX_LOG_BYTES:
            parts.append("... (truncated)\n")
            break
    return "".join(parts)


def pod_logs(namespace: str, deployment_name: str, container_name: str, pod_name: str, tail_lines: int = 300, previous: bool = False,
             cluster: Optional[str] = None) -> str:
    """Лог контейнера пода этого развёртывания (чужие поды не отдаются). previous — прошлый запуск (после падения)."""
    apps, core = (kubeclient.apps_v1_api(cluster), kubeclient.core_v1_api(cluster)) if cluster else (kubeclient.apps_v1_api(), kubeclient.core_v1_api())
    dep = apps.read_namespaced_deployment(deployment_name, namespace)
    selector = dep.spec.selector.match_labels or {}
    names = {p.metadata.name for p in core.list_namespaced_pod(namespace, label_selector=",".join(f"{k}={v}" for k, v in selector.items())).items}
    if pod_name not in names:
        raise LookupError(f"pod {pod_name} does not belong to this environment")
    try:
        text = core.read_namespaced_pod_log(pod_name, namespace, container=container_name, tail_lines=tail_lines, previous=previous)
    except ApiException as e:
        if e.status == 400:                                   # контейнер ещё не запускался или прошлого запуска нет
            return ""
        raise
    return text[-MAX_LOG_BYTES:] if len(text) > MAX_LOG_BYTES else text


def mask_secrets(text: str, values) -> str:
    """Значения секретов в логах заменяются на ***: разработчик не читает значения, и лог не должен стать обходным путём (значения короче 6 символов не трогаем — ложные срабатывания)."""
    for v in sorted({x for x in values if x and len(x) >= 6}, key=len, reverse=True):
        text = text.replace(v, "***")
    return text
