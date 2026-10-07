"""Сборка образа по Dockerfile из репозитория: одноразовое задание Kubernetes (kaniko) в домашнем кластере.

Код клонируется init-контейнером (токен репозитория — только из Secret через заголовок, в аргументах и логах его нет),
kaniko собирает образ без демона Docker и публикует его в реестр; дайджест берётся из termination message.
Образ выкатывается по дайджесту, поэтому ревизия и образ связаны однозначно."""
import logging
import re
import secrets as pysecrets
import time
from typing import Optional

from kubernetes.client.exceptions import ApiException

from app.config import settings
from app.services import kubeclient

logger = logging.getLogger(__name__)

PATH_RE = re.compile(r"^[A-Za-z0-9._/-]*$")
REV_RE = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
LABEL_IMAGE = "platform.split-wave.com/image"
LABEL_REV = "platform.split-wave.com/revision"
KB = "-kbuild-"
PLATFORM_DOCKERFILE = "Dockerfile.platform"      # имя файла с Dockerfile платформы внутри контекста сборки
DEADLINE_GRACE = 60          # запас ожидания сверх срока задания (время на запуск пода и подтягивание образов)


def safe_path(p: str) -> bool:
    return bool(PATH_RE.match(p)) and ".." not in p.split("/") and not p.startswith("/")


def registry_tag(project, image_name: str) -> str:
    return f"{(project.registry_prefix or settings.registry_prefix).rstrip('/')}/{image_name}"


def job_body(project, image_name: str, namespace: str, git_url: str, revision: str, name: str) -> dict:
    if not safe_path(project.sub_path or "") or not safe_path(project.dockerfile_path or "Dockerfile"):
        raise ValueError("sub_path and dockerfile_path must be relative paths without '..'")
    if not REV_RE.match(revision):
        raise ValueError("invalid revision")
    rev12 = re.sub(r"[^A-Za-z0-9._-]", "-", revision)[:12]
    dest = f"{registry_tag(project, image_name)}:{rev12}"
    platform_dockerfile = bool(getattr(project, "dockerfile_content", None))
    dockerfile_name = PLATFORM_DOCKERFILE if platform_dockerfile else (project.dockerfile_path or "Dockerfile")
    args = ["--context=dir:///workspace", f"--dockerfile={dockerfile_name}", f"--destination={dest}",
            "--digest-file=/dev/termination-log", "--snapshot-mode=redo", "--use-new-run"]
    if project.sub_path:
        args.append(f"--context-sub-path={project.sub_path}")
    for reg in [r.strip() for r in settings.kaniko_insecure_registries.split(",") if r.strip()]:
        args += [f"--insecure-registry={reg}", f"--skip-tls-verify-registry={reg}"]
    secret = f"git-{project.slug}"[:63]
    script = ('set -eu\n'
              'git init -q /workspace && cd /workspace\n'
              'git remote add origin "$GIT_URL"\n'
              'if [ -n "${GIT_TOKEN:-}" ]; then\n'
              '  AUTH=$(printf "%s:%s" "${GIT_USER:-git}" "$GIT_TOKEN" | base64 | tr -d "\\n")\n'
              '  git -c "http.extraHeader=Authorization: Basic $AUTH" fetch -q --depth 1 origin "$REVISION"\n'
              'else\n'
              '  git fetch -q --depth 1 origin "$REVISION"\n'
              'fi\n'
              'git checkout -q FETCH_HEAD && rm -rf .git\n'
              # Dockerfile платформы кладётся рядом с контекстом сборки из переменной окружения (без подстановок в команду)
              'if [ -n "${DSP_DOCKERFILE:-}" ]; then\n'
              '  mkdir -p "/workspace/${DSP_SUB}" && printf "%s\\n" "$DSP_DOCKERFILE" > "/workspace/${DSP_SUB}/' + PLATFORM_DOCKERFILE + '"\n'
              'fi\n')
    init_env = [{"name": "GIT_URL", "value": git_url}, {"name": "REVISION", "value": revision},
                {"name": "GIT_USER", "valueFrom": {"secretKeyRef": {"name": secret, "key": "username", "optional": True}}},
                {"name": "GIT_TOKEN", "valueFrom": {"secretKeyRef": {"name": secret, "key": "password", "optional": True}}}]
    if platform_dockerfile:
        init_env += [{"name": "DSP_DOCKERFILE", "value": project.dockerfile_content}, {"name": "DSP_SUB", "value": project.sub_path or "."}]
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {"app.kubernetes.io/managed-by": "splitwave", LABEL_IMAGE: image_name, LABEL_REV: rev12}},
        "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 86400, "activeDeadlineSeconds": settings.build_timeout_seconds,
                 "template": {"metadata": {"labels": {LABEL_IMAGE: image_name}}, "spec": {
                     "restartPolicy": "Never",
                     "initContainers": [{
                         "name": "fetch", "image": settings.kaniko_git_image, "command": ["sh", "-c", script],
                         "env": init_env,
                         "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"}]}],
                     "containers": [{
                         "name": "kaniko", "image": settings.kaniko_image, "args": args,
                         "env": [{"name": "DOCKER_CONFIG", "value": "/kaniko/.docker/"}],
                         "resources": {"requests": {"cpu": settings.kaniko_cpu, "memory": settings.kaniko_memory},
                                       "limits": {"memory": settings.kaniko_memory_limit}},
                         "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"}, {"name": "registry", "mountPath": "/kaniko/.docker"}]}],
                     "volumes": [{"name": "workspace", "emptyDir": {}},
                                 {"name": "registry", "secret": {"secretName": settings.kaniko_registry_secret, "optional": True,
                                                                "items": [{"key": ".dockerconfigjson", "path": "config.json"}]}}]}}},
    }


def _tail(core, namespace: str, job: str, lines: int = 25) -> str:
    out = []
    try:
        pods = core.list_namespaced_pod(namespace, label_selector=f"job-name={job}").items
    except ApiException:
        return ""
    for pod in pods[:1]:
        for cname in ("fetch", "kaniko"):
            try:
                text = core.read_namespaced_pod_log(pod.metadata.name, namespace, container=cname, tail_lines=lines)
            except ApiException:
                continue
            if text and text.strip():
                out.append(f"[{cname}] " + text.strip()[-1500:])
    return "\n".join(out)[-3000:]


def build_image(project, env, git_url: str, revision: str, poll_seconds: float = 5.0) -> str:
    """Собирает образ и возвращает ссылку `реестр/имя@sha256:...`. Исключения: TimeoutError, RuntimeError (с хвостом логов)."""
    namespace, image_name = env.build_namespace, env.image_name
    name = f"{image_name}{KB}{pysecrets.token_hex(3)}"[:63]
    batch, core = kubeclient.batch_v1_api(), kubeclient.core_v1_api()
    batch.create_namespaced_job(namespace, job_body(project, image_name, namespace, git_url, revision, name))
    logger.info("kaniko job %s/%s started for %s@%s", namespace, name, image_name, revision[:12])
    deadline = time.monotonic() + settings.build_timeout_seconds + DEADLINE_GRACE
    while time.monotonic() < deadline:
        st = batch.read_namespaced_job_status(name, namespace).status
        if st.succeeded:
            for pod in core.list_namespaced_pod(namespace, label_selector=f"job-name={name}").items:
                for cs in pod.status.container_statuses or []:
                    if cs.name == "kaniko" and cs.state and cs.state.terminated and (cs.state.terminated.message or "").strip().startswith("sha256:"):
                        return f"{registry_tag(project, image_name)}@{cs.state.terminated.message.strip()}"
            raise RuntimeError("build finished but the image digest was not reported")
        if st.failed:
            cond = next((c for c in (st.conditions or []) if c.type == "Failed"), None)
            if cond and cond.reason == "DeadlineExceeded":
                raise TimeoutError(f"docker build exceeded {settings.build_timeout_seconds}s")
            raise RuntimeError("docker build failed:\n" + _tail(core, namespace, name))
        time.sleep(poll_seconds)
    raise TimeoutError(f"docker build {name} did not finish in time")


# ───────────── для страницы «Сборки» ─────────────
def list_builds(image_name: str, namespace: str, limit: int = 15) -> list[dict]:
    batch = kubeclient.batch_v1_api()
    jobs = batch.list_namespaced_job(namespace, label_selector=f"{LABEL_IMAGE}={image_name}").items
    jobs.sort(key=lambda j: j.metadata.creation_timestamp, reverse=True)
    out = []
    for j in jobs[:limit]:
        st = j.status
        status = "succeeded" if st.succeeded else "failed" if st.failed else "building"
        out.append({"name": j.metadata.name, "created_at": j.metadata.creation_timestamp.isoformat(), "status": status, "reason": None,
                    "message": "", "revision": (j.metadata.labels or {}).get(LABEL_REV), "latest_image": None, "method": "dockerfile"})
    return out


def build_logs(image_name: str, namespace: str, build_name: str, tail_lines: int = 400) -> str:
    if not re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", build_name) or not build_name.startswith(f"{image_name}{KB}"):
        raise ValueError("invalid build name")
    core = kubeclient.core_v1_api()
    pods = core.list_namespaced_pod(namespace, label_selector=f"job-name={build_name}").items
    if not pods:
        raise ApiException(status=404, reason="build pod not found")
    parts = []
    for cname in ("fetch", "kaniko"):
        try:
            text = core.read_namespaced_pod_log(pods[0].metadata.name, namespace, container=cname, tail_lines=tail_lines)
        except ApiException as e:
            text = f"<{cname}: unavailable ({e.status})>"
        parts.append(f"=== {cname} ===\n{text}\n")
    return "".join(parts)
