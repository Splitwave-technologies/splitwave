#!/usr/bin/env python3
"""Сквозная проверка сборки по Dockerfile на настоящем Kubernetes (kaniko) без внешних сервисов:
в кластере поднимается Git-сервер (git://) и используется реестр кластера; платформа работает локально (SQLite, kubeconfig).
Проверяет: сборку из корня и подпапки, образ по дайджесту в реестре, выкат, провал при ошибке в Dockerfile и отсутствие токена в задании.
Запуск на dev: source control-plane/.venv/bin/activate && python scripts/e2e_dockerfile.py"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control-plane"))
from cryptography.fernet import Fernet  # noqa: E402

NS = "e2e-df"
REG = "registry.registry.svc.cluster.local:5000"
TOKEN = "e2e-admin-token-0123456789abcdef"
os.environ.update(SPLITWAVE_TEST_MODE="1", TIER_LIMITS_ENFORCE="false", DATABASE_URL=f"sqlite:///{tempfile.mkdtemp()}/df.db", SECRET_ENCRYPTION_KEY=Fernet.generate_key().decode(), ADMIN_BOOTSTRAP_TOKEN=TOKEN,
                  GITHUB_WEBHOOK_SECRET="x", REGISTRY_PREFIX=f"{REG}/e2e", KANIKO_INSECURE_REGISTRIES=REG, BUILD_TIMEOUT_SECONDS="600",
                  KANIKO_REGISTRY_SECRET="absent-secret", KANIKO_CPU="250m", KANIKO_MEMORY="512Mi", KANIKO_MEMORY_LIMIT="2Gi", CHECKS_INTERVAL_MINUTES="0")

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from app import auth  # noqa: E402
from app.db.base import Base, SessionLocal, engine  # noqa: E402
import app.main as main  # noqa: E402
from app.routers import projects  # noqa: E402

Base.metadata.create_all(engine)
d = SessionLocal(); auth.bootstrap_admin(d); d.close()
main.run_migrations = lambda: None
projects._check_git_url = lambda provider, url: url          # в кластере тестовый Git-сервер по git://, в продукте разрешён только https

results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{extra}]" if not cond and extra else ""))


def sh(*args, stdin=None, timeout=120):
    return subprocess.run(list(args), input=stdin, capture_output=True, text=True, timeout=timeout)


def kc(*a, **k):
    return sh("kubectl", *a, **k)


threading.Thread(target=lambda: uvicorn.run(main.app, host="127.0.0.1", port=8296, log_level="warning"), daemon=True).start()
api = httpx.Client(base_url="http://127.0.0.1:8296", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120)
for _ in range(40):
    try:
        if api.get("/health").status_code == 200:
            break
    except httpx.HTTPError:
        time.sleep(0.5)

try:
    kc("delete", "ns", NS, "--ignore-not-found", "--wait=true")
    kc("create", "ns", NS)
    kc("-n", NS, "apply", "-f", "-", stdin=f"""
apiVersion: v1
kind: Pod
metadata: {{name: gitd, labels: {{app: gitd}}}}
spec:
  containers:
  - name: gitd
    image: alpine:3.20
    command: ["sh", "-c"]
    args:
    - |
      set -e
      apk add --no-cache git git-daemon >/dev/null
      git config --global user.email e2e@example.com && git config --global user.name e2e && git config --global init.defaultBranch main
      mkdir -p /repos && cd /repos && git init -q app && cd app
      git config uploadpack.allowAnySHA1InWant true
      printf 'FROM busybox:1.36\\nRUN echo built-at-root > /marker.txt\\nCMD ["sleep","100000"]\\n' > Dockerfile
      mkdir svc && printf 'FROM busybox:1.36\\nRUN echo built-in-subdir > /marker.txt\\nCMD ["sleep","100000"]\\n' > svc/Dockerfile
      git add -A && git commit -q -m root && git rev-parse HEAD > /tmp/sha_good
      git checkout -q -b bad && printf 'FROM busybox:1.36\\nRUN exit 7\\n' > Dockerfile && git commit -qam bad
      git checkout -q main
      touch .git/git-daemon-export-ok
      exec git daemon --reuseaddr --base-path=/repos --export-all --port=9418 --verbose
    readinessProbe: {{tcpSocket: {{port: 9418}}, initialDelaySeconds: 8, periodSeconds: 3}}
---
apiVersion: v1
kind: Service
metadata: {{name: gitd}}
spec: {{selector: {{app: gitd}}, ports: [{{port: 9418}}]}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {{name: app}}
spec:
  replicas: 0
  selector: {{matchLabels: {{app: app}}}}
  template:
    metadata: {{labels: {{app: app}}}}
    spec: {{containers: [{{name: app, image: "busybox:1.36", command: ["sleep", "100000"]}}]}}
""")
    kc("-n", NS, "wait", "--for=condition=Ready", "pod/gitd", "--timeout=180s")
    sha = kc("-n", NS, "exec", "gitd", "--", "cat", "/tmp/sha_good").stdout.strip()
    check("тестовый Git-сервер поднят", len(sha) == 40, sha)

    def project(slug, **extra):
        body = {"slug": slug, "repo_full_name": f"acme/{slug}", "provider": "gitea", "git_url": f"git://gitd.{NS}.svc.cluster.local/app", "build_method": "dockerfile",
                "environments": [{"name": "prod", "namespace": NS, "deployment_name": "app", "container_name": "app", "branch": "main"}], **extra}
        return api.post("/api/projects", json=body)

    def wait_release(slug, revision, timeout=420):
        api.post(f"/api/projects/{slug}/redeploy?revision={revision}")
        end = time.time() + timeout
        while time.time() < end:
            rel = api.get(f"/api/projects/{slug}/releases").json()
            if rel and rel[0]["status"] in ("deployed", "failed"):
                return rel[0]
            time.sleep(5)
        print(kc("-n", NS, "get", "jobs,pods", "-o", "wide").stdout[-1500:])
        return {"status": "timeout", "releases": api.get(f"/api/projects/{slug}/releases").json()}

    if os.environ.get("E2E_ONLY") != "bad":      # E2E_ONLY=bad — только сценарий с ошибкой в Dockerfile (быстрая отладка)
        check("проект с Dockerfile создан", project("root-app").status_code == 201)
        rel = wait_release("root-app", sha)
        check("сборка из корня репозитория завершена", rel["status"] == "deployed", json.dumps(rel)[:300])
        img = rel.get("image_digest") or ""
        check("образ закреплён по дайджесту", "@sha256:" in img and img.startswith(f"{REG}/e2e/root-app@"), img)
        digest = img.split("@")[-1]
        reg = kc("-n", NS, "run", "regq", "--rm", "-i", "--restart=Never", "--image=curlimages/curl:8.8.0", "--command", "--", "curl", "-sI", "-H",
                 "Accept: application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json",
                 f"http://{REG}/v2/e2e/root-app/manifests/{digest}", stdin="", timeout=180).stdout
        check("образ с этим дайджестом лежит в реестре", "200" in reg.splitlines()[0] if reg else False, reg[:150])
        dep = kc("-n", NS, "get", "deploy", "app", "-o", "jsonpath={.spec.template.spec.containers[0].image}").stdout
        check("Deployment переключён на собранный образ", dep == img, dep)
        jobs = kc("-n", NS, "get", "jobs", "-o", "json").stdout
        check("токен и секреты не попали в аргументы задания", "GIT_TOKEN" in jobs and "password" not in json.dumps(json.loads(jobs)["items"][0]["spec"]["template"]["spec"]["containers"][0]["args"]))
        builds = api.get("/api/projects/root-app/builds").json()["builds"]
        check("сборка видна в списке сборок", any(b.get("method") == "dockerfile" and b["status"] == "succeeded" for b in builds), str(builds)[:200])
        logs = api.get(f"/api/projects/root-app/builds/{builds[0]['name']}/logs")
        check("логи сборки доступны", logs.status_code == 200 and "kaniko" in logs.text, logs.text[:150])

        check("проект с подпапкой создан", project("sub-app", sub_path="svc").status_code == 201)
        rel = wait_release("sub-app", sha)
        check("сборка из подпапки svc завершена", rel["status"] == "deployed", json.dumps(rel)[:300])
        check("подпапка собрана отдельно (другой дайджест)", rel.get("image_digest", "").split("@")[-1] != digest)

    check("проект с ошибочным Dockerfile создан", project("bad-app").status_code == 201)
    bad_sha = kc("-n", NS, "exec", "gitd", "--", "git", "-C", "/repos/app", "rev-parse", "bad").stdout.strip()
    rel = wait_release("bad-app", bad_sha)
    check("ошибка в Dockerfile: выкат провален, текст причины сохранён", rel["status"] == "failed" and ("exit status 7" in (rel.get("error_message") or "")), json.dumps(rel)[:300])
    dep2 = kc("-n", NS, "get", "deploy", "app", "-o", "jsonpath={.spec.template.spec.containers[0].image}").stdout
    check("провал сборки не меняет работающий Deployment", dep2 != "" and "bad-app" not in dep2, dep2)
    check("цепочка аудита цела", api.get("/api/audit/verify").json()["ok"] is True)
finally:
    kc("delete", "ns", NS, "--wait=false")
print(f"ИТОГО: PASS={sum(results)} FAIL={len(results) - sum(results)}")
sys.exit(0 if results and all(results) else 1)
