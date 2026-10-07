import logging
import time

from app.config import settings
from app.services.kubeclient import apps_v1_api, core_v1_api

logger = logging.getLogger(__name__)

# Причины, по которым под нового выката заведомо не поднимется: останавливаем ожидание сразу или после короткой паузы.
FATAL_NOW = {"CrashLoopBackOff", "CreateContainerConfigError", "InvalidImageName", "RunContainerError"}
FATAL_AFTER_GRACE = {"ImagePullBackOff", "ErrImagePull", "CreateContainerError"}   # реестр бывает медленным: даём время
PULL_GRACE_SECONDS = 45


class RolloutError(RuntimeError):
    """Образ выкачен в кластер, но приложение не поднялось. reason: rollout_failed | rollout_timeout."""

    def __init__(self, message: str, reason: str = "rollout_failed"):
        super().__init__(message)
        self.reason = reason


def deploy_image(deployment_name: str, container_name: str, image: str, namespace: str = None, cluster: str = None) -> None:
    namespace = namespace or settings.default_namespace
    api = apps_v1_api(cluster) if cluster else apps_v1_api()
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": container_name, "image": image}]
                }
            }
        }
    }
    api.patch_namespaced_deployment(deployment_name, namespace, patch)
    logger.info("Deployment %s/%s updated to image %s", namespace, deployment_name, image)


def _mask(text: str, values) -> str:
    for v in sorted({x for x in values if x and len(x) >= 6}, key=len, reverse=True):
        text = text.replace(v, "***")      # значения секретов не должны попасть в сообщение релиза
    return text


def _new_pods(core, namespace: str, dep, container_name: str, image: str):
    selector = ",".join(f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items())
    pods = core.list_namespaced_pod(namespace, label_selector=selector).items
    return [p for p in pods if any(c.name == container_name and c.image == image for c in (p.spec.containers or []))]


def _pod_problem(pod, container_name: str):
    """(reason, подробности, перезапусков) для контейнера нового пода, если он в проблемном состоянии."""
    for cs in (pod.status.container_statuses or []):
        if cs.name != container_name:
            continue
        waiting = cs.state.waiting if cs.state else None
        last = cs.last_state.terminated if cs.last_state else None
        if waiting and waiting.reason:
            detail = (waiting.message or "")[:400]
            if last:
                detail = f"exit code {last.exit_code}, {last.reason or ''} {last.message or ''}".strip()[:400] or detail
            return waiting.reason, detail, cs.restart_count or 0
    return None


def _diagnostics(core, namespace: str, pod, container_name: str, reason: str, detail: str, restarts: int, mask_values) -> str:
    text = f"{reason}: pod {pod.metadata.name}, container {container_name}, restarts {restarts}" + (f", {detail}" if detail else "")
    try:
        log = core.read_namespaced_pod_log(pod.metadata.name, namespace, container=container_name, tail_lines=25,
                                           previous=restarts > 0)
        if log:
            text += "\n--- last log lines ---\n" + log.strip()[-1200:]
    except Exception:       # лога может не быть (образ не скачан, контейнер не стартовал): причина уже указана выше
        pass
    return _mask(text, mask_values)[:2000]


def wait_rollout(deployment_name: str, container_name: str, image: str, namespace: str = None, cluster: str = None,
                 timeout: int = None, poll: float = None, mask_values=(), sleep=time.sleep, clock=time.monotonic) -> None:
    """Ждёт, пока новый выкат реально заработает: все реплики обновлены и готовы, старые поды ушли.
    Раньше релиз считался «deployed» сразу после смены образа, даже если приложение падало в цикле перезапуска.
    Исключение RolloutError: под падает (CrashLoopBackOff), не скачивается образ, не хватает секрета/ключа, истёк срок."""
    namespace = namespace or settings.default_namespace
    timeout = timeout if timeout is not None else settings.rollout_timeout_seconds
    poll = poll if poll is not None else settings.rollout_poll_seconds
    apps = apps_v1_api(cluster) if cluster else apps_v1_api()
    core = core_v1_api(cluster) if cluster else core_v1_api()
    start = clock()
    deadline = start + timeout
    first_seen: dict = {}
    summary = ""
    while True:
        dep = apps.read_namespaced_deployment(deployment_name, namespace)
        st = dep.status
        want = 1 if dep.spec.replicas is None else dep.spec.replicas
        if want == 0:
            return                                        # приложение намеренно остановлено
        observed_ok = ((st.observed_generation or 0) >= (dep.metadata.generation or 0)) if st and dep.metadata else True
        updated, available, total = ((st.updated_replicas or 0), (st.available_replicas or 0), (st.replicas or 0)) if st else (0, 0, 0)
        summary = f"updated {updated}/{want}, available {available}/{want}"
        if observed_ok and updated >= want and available >= want and total <= want:
            return
        for c in ((st.conditions or []) if st else []):
            if c.type == "Progressing" and c.reason == "ProgressDeadlineExceeded":
                raise RolloutError(f"ProgressDeadlineExceeded: {c.message or summary}")
        pods = _new_pods(core, namespace, dep, container_name, image)
        for pod in pods:
            problem = _pod_problem(pod, container_name)
            if not problem:
                first_seen.pop(pod.metadata.name, None)
                continue
            reason, detail, restarts = problem
            since = first_seen.setdefault(pod.metadata.name, clock())
            if reason in FATAL_NOW or (reason in FATAL_AFTER_GRACE and clock() - since >= PULL_GRACE_SECONDS):
                raise RolloutError(_diagnostics(core, namespace, pod, container_name, reason, detail, restarts, mask_values))
        if clock() >= deadline:
            states = ", ".join(f"{p.metadata.name}({(_pod_problem(p, container_name) or ('starting',))[0]})" for p in pods) or "no pods of the new revision"
            raise RolloutError(f"rollout did not complete in {timeout}s: {summary}; pods: {states}", reason="rollout_timeout")
        sleep(poll)
