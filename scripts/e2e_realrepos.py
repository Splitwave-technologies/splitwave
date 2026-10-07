"""Сквозная проверка платформы на реальных публичных репозиториях GitHub: ссылка → разбор → проект → сборка → под → ответ приложения.
Запуск на dev: source control-plane/.venv/bin/activate && python scripts/e2e_realrepos.py   (E2E_ONLY=r-node,r-go — выбрать часть; E2E_KEEP=1 — не удалять)
Итог — /tmp/real-repos.json."""
import base64
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

INFRA = "e2e-onb"
NODE = os.environ.get("E2E_NODE_IP", "127.0.0.1")          # IP узла тестового кластера; задаётся окружением
GITEA_PORT = 30300
GITEA = f"http://{NODE}:{GITEA_PORT}"
REG = "registry.registry.svc.cluster.local:5000"
PLATFORM_PORT = 8297
TOKEN = "e2e-admin-token-0123456789abcdef"
USER, PASSWORD = "e2e", "e2e-password-12345"
SLUGS = []
os.environ.update(SPLITWAVE_TEST_MODE="1", TIER_LIMITS_ENFORCE="false", DATABASE_URL=f"sqlite:///{tempfile.mkdtemp()}/onb.db", SECRET_ENCRYPTION_KEY=Fernet.generate_key().decode(), ADMIN_BOOTSTRAP_TOKEN=TOKEN,
                  GITHUB_WEBHOOK_SECRET="x", REGISTRY_PREFIX=f"{REG}/e2e", KANIKO_INSECURE_REGISTRIES=REG, BUILD_TIMEOUT_SECONDS="600",
                  KANIKO_REGISTRY_SECRET="absent-secret", KANIKO_CPU="250m", KANIKO_MEMORY="512Mi", KANIKO_MEMORY_LIMIT="2Gi", CHECKS_INTERVAL_MINUTES="0",
                  SCM_ALLOW_HTTP="true", SCM_ALLOW_PRIVATE_HOSTS="true",
                  PLATFORM_NAMESPACE="e2e-rbac", PLATFORM_SERVICE_ACCOUNT="e2erbac-splitwave",
                  DEPLOYER_CLUSTER_ROLE="e2erbac-splitwave-deployer")

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from app import auth  # noqa: E402
from app.db.base import Base, SessionLocal, engine  # noqa: E402
import app.main as main  # noqa: E402
from app.routers import projects  # noqa: E402

Base.metadata.create_all(engine)
d = SessionLocal(); auth.bootstrap_admin(d); d.close()
main.run_migrations = lambda: None
projects._check_git_url = lambda provider, url: url          # в стенде Gitea по http, в продукте разрешён только https

results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{extra}]" if not cond and extra else ""), flush=True)


def sh(*args, stdin=None, timeout=180, cwd=None):
    return subprocess.run(list(args), input=stdin, capture_output=True, text=True, timeout=timeout, cwd=cwd)


def kc(*a, **k):
    return sh("kubectl", *a, **k)


# ── Платформа работает НЕ под администратором кластера, а под ServiceAccount с теми же правами, что выдаёт Helm-чарт
# (ClusterRole deployer + provisioner из `helm template`). Раньше e2e шли от администратора и не видели нехватки прав (jobs/status).
RBAC_NS, REL = "e2e-rbac", "e2erbac"
SA = f"{REL}-splitwave"
CHART = Path(__file__).resolve().parents[1] / "deploy/helm/splitwave"


def use_minimal_rbac():
    kc("delete", "ns", RBAC_NS, "--ignore-not-found", "--wait=true")
    kc("create", "ns", RBAC_NS)
    tpl = sh("helm", "template", REL, str(CHART), "-n", RBAC_NS, "--set", "provisioner.enabled=true", "--set", "secrets.adminBootstrapToken=0123456789012345678901234")
    assert tpl.returncode == 0, tpl.stderr
    import yaml as _y
    docs = [d for d in _y.safe_load_all(tpl.stdout) if d and d["kind"] in ("ServiceAccount", "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding")]
    ap = kc("apply", "-n", RBAC_NS, "-f", "-", stdin=_y.safe_dump_all(docs))   # у ServiceAccount в шаблоне нет namespace
    assert ap.returncode == 0, ap.stderr
    tok = kc("-n", RBAC_NS, "create", "token", SA, "--duration=3h").stdout.strip()
    assert tok, "не удалось выпустить токен ServiceAccount"
    cfg = json.loads(kc("config", "view", "--raw", "--minify", "-o", "json").stdout)
    cl = cfg["clusters"][0]["cluster"]
    path = Path(tempfile.mkdtemp()) / "sa-kubeconfig"
    path.write_text(json.dumps({"apiVersion": "v1", "kind": "Config", "current-context": "sa", "clusters": [{"name": "c", "cluster": cl}],
                                "users": [{"name": "u", "user": {"token": tok}}], "contexts": [{"name": "sa", "context": {"cluster": "c", "user": "u"}}]}))
    from kubernetes import config as k8s_config
    from app.services import kubeclient
    k8s_config.load_kube_config(config_file=str(path))
    kubeclient._loaded = True                      # платформа дальше ходит в кластер только под этим ServiceAccount


use_minimal_rbac()
threading.Thread(target=lambda: uvicorn.run(main.app, host="0.0.0.0", port=PLATFORM_PORT, log_level="warning"), daemon=True).start()
api = httpx.Client(base_url=f"http://127.0.0.1:{PLATFORM_PORT}", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120)
for _ in range(60):
    try:
        if api.get("/health").status_code == 200:
            break
    except httpx.HTTPError:
        time.sleep(0.5)
def wait(cond, timeout=300, step=4):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return None


def node_ready(ns, slug, timeout=240):
    return wait(lambda: kc("-n", ns, "get", "deploy", slug, "-o", "jsonpath={.status.readyReplicas}").stdout.strip() == "1", timeout)


def serves(ns, slug, needle, timeout=90, port=8080):
    def probe():
        out = kc("-n", ns, "run", f"curl-{int(time.time()) % 10000}", "--rm", "-i", "--restart=Never", "--image=busybox:1.36", "--",
                 "wget", "-qO-", f"http://{slug}.{ns}.svc.cluster.local:{port}/", timeout=60)
        return needle in out.stdout
    return wait(probe, timeout, 5)


# ───────── реальные публичные репозитории: от ссылки до работающего приложения ─────────
REPOS = [
    # (slug, url, ожидание: язык | None, с Dockerfile или без)
    ("r-node", "https://github.com/heroku/nodejs-getting-started", "Node.js (Express), Procfile"),
    ("r-python", "https://github.com/heroku/python-getting-started", "Python (Django), Procfile"),
    ("r-go", "https://github.com/heroku/go-getting-started", "Go (Gin), Procfile"),
    ("r-php", "https://github.com/heroku/php-getting-started", "PHP (composer), Procfile"),
    ("r-static", "https://github.com/mdn/beginner-html-site-styled", "статический сайт без сервера"),
    ("r-docker", "https://github.com/docker/welcome-to-docker", "есть Dockerfile (Node)"),
    ("r-java", "https://github.com/spring-projects/spring-petclinic", "Java (Spring Boot, Maven)"),
    ("r-ruby", "https://github.com/heroku/ruby-getting-started", "Ruby (Rails)"),
    ("r-dotnet", "https://github.com/heroku/dotnet-getting-started", ".NET (ASP.NET)"),
    ("r-rust", "https://github.com/selvacodes/axum-hello-world", "Rust (axum)"),
]
ONLY = [s for s in os.environ.get("E2E_ONLY", "").split(",") if s]
report = []


def probe(ns, slug, port, timeout=120):
    """Отвечает ли приложение: любой HTTP-ответ сервера (2xx–4xx) означает «работает»; 5xx и тишина — нет."""
    def go():
        out = kc("-n", ns, "run", f"c{int(time.time()) % 100000}", "--rm", "-i", "--restart=Never", "--image=busybox:1.36", "--",
                 "wget", "-S", "-qO-", f"http://{slug}.{ns}.svc.cluster.local:{port}/", timeout=60)
        text = out.stdout + out.stderr
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("HTTP/") and len(line.split()) > 1 and line.split()[1][0] in "234":
                return line
        return None
    return wait(go, timeout, 6)


try:
    for slug, url, note in REPOS:
        if ONLY and slug not in ONLY:
            continue
        row = {"slug": slug, "url": url, "note": note}
        report.append(row)
        t0 = time.time()
        r = api.post("/api/onboarding/inspect", json={"url": url, "provider": "github"})
        row["inspect_status"] = r.status_code
        if r.status_code != 200:
            row["verdict"] = "FAIL inspect: " + r.text[:200]
            print(slug, row["verdict"], flush=True)
            continue
        pr = r.json()["proposal"]
        row.update(language=pr.get("language"), method=pr.get("build_method"), port=pr.get("port"), warnings=pr.get("warnings"), generated=pr.get("dockerfile_generated"))
        body = {"slug": slug, "repo_full_name": pr["repo_full_name"], "provider": "github", "git_url": pr.get("git_url") or url + ".git", "build_method": pr.get("build_method", "dockerfile"),
                "port": pr.get("port") or 8080, "head_sha": pr.get("head_sha"), "sub_path": pr.get("sub_path", "")}
        if pr.get("dockerfile_generated"):
            body["dockerfile_content"] = (pr.get("dockerfile_template") or {}).get("content", "")
        c = api.post("/api/onboarding/create", json=body)
        row["create_status"] = c.status_code
        if c.status_code != 201:
            row["verdict"] = "FAIL create: " + c.text[:300]
            print(slug, row["verdict"], flush=True)
            continue
        rel = wait(lambda: next((x for x in api.get(f"/api/projects/{slug}/releases").json() if x["status"] in ("deployed", "failed")), None), 900, 8)
        row["release"] = (rel or {}).get("status", "timeout")
        row["release_error"] = (rel or {}).get("error_message")
        if not rel or rel["status"] != "deployed":
            builds = api.get(f"/api/projects/{slug}/builds").json().get("builds", [])
            if builds:
                logs = api.get(f"/api/projects/{slug}/builds/{builds[0]['name']}/logs")
                row["build_log_tail"] = logs.text[-600:]
            row["verdict"] = f"FAIL release {row['release']}"
            print(slug, row["verdict"], flush=True)
            continue
        ready = node_ready(slug, slug, 240)
        row["pod_ready"] = ready
        if ready:
            row["http"] = probe(slug, slug, pr.get("port") or 8080)
        row["verdict"] = "PASS" if ready and row.get("http") else "FAIL runtime"
        if not ready or not row.get("http"):
            row["pod_log"] = kc("-n", slug, "logs", f"deploy/{slug}", "--tail=12").stdout[-500:]
        row["seconds"] = int(time.time() - t0)
        print(slug, row["verdict"], row.get("http") or "", f"{row['seconds']}s", flush=True)
finally:
    json.dump(report, open("/tmp/real-repos.json", "w"), ensure_ascii=False, indent=1)
    if not os.environ.get("E2E_KEEP"):
        kc("delete", "ns", RBAC_NS, "--ignore-not-found", "--wait=false")
        kc("delete", "clusterrole", f"{REL}-splitwave-deployer", f"{REL}-splitwave-provisioner", "--ignore-not-found")
        kc("delete", "clusterrolebinding", f"{REL}-splitwave-provisioner", "--ignore-not-found")
        for slug, _, _ in REPOS:
            kc("delete", "ns", slug, "--ignore-not-found", "--wait=false")
print("DONE")
