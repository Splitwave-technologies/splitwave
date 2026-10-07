#!/usr/bin/env python3
"""Сквозная проверка мастера подключения проекта на настоящем Kubernetes без внешних сервисов.

В кластере поднимается настоящий Gitea (API провайдера), платформа работает локально (SQLite, kubeconfig администратора).
Проходит весь путь пользователя: ссылка → разбор репозитория (токены, приватный репозиторий, монорепозиторий, ошибки) →
создание проекта и приложения в кластере → сборка kaniko → образ из реестра → запущенное приложение отвечает →
ручной путь (YAML для администратора, проверка kubectl --dry-run=server) → webhook Gitea запускает новый выкат.

Требования: на dev настроено зеркало реестра в k3s (/etc/rancher/k3s/registries.yaml: registry.registry.svc.cluster.local:5000 → ClusterIP реестра),
иначе под не сможет скачать образ из реестра кластера; на время webhook нужно временное правило nftables (скрипт добавляет и убирает).
Запуск на dev: source control-plane/.venv/bin/activate && python scripts/e2e_onboarding.py   (E2E_KEEP=1 — не удалять созданное)"""
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
NODE = os.environ["E2E_NODE_IP"]          # IP узла тестового кластера; задаётся окружением
GITEA_PORT = 30300
GITEA = f"http://{NODE}:{GITEA_PORT}"
REG = "registry.registry.svc.cluster.local:5000"
PLATFORM_PORT = 8297
TOKEN = "e2e-admin-token-0123456789abcdef"
USER, PASSWORD = "e2e", "e2e-password-12345"
SLUGS = ["onb-web", "onb-manual", "onb-hook", "onb-priv", "gen-node", "gen-py", "gen-go", "gen-static"]
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
gitea = httpx.Client(base_url=GITEA, auth=(USER, PASSWORD), timeout=30)
RULE = "e2e-onboarding-temp"


def wait(cond, timeout=300, step=4):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return None


def make_repo(name, files, private=False):
    r = gitea.post("/api/v1/user/repos", json={"name": name, "private": private, "default_branch": "main"})
    assert r.status_code in (201, 409), r.text
    work = tempfile.mkdtemp()
    sh("git", "init", "-q", "-b", "main", cwd=work)
    for path, text in files.items():
        p = Path(work, path); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
    sh("git", "add", "-A", cwd=work)
    sh("git", "-c", "user.email=e2e@example.com", "-c", "user.name=e2e", "commit", "-q", "-m", "init", cwd=work)
    p = sh("git", "push", "-q", "-f", f"http://{USER}:{PASSWORD}@{NODE}:{GITEA_PORT}/{USER}/{name}.git", "main", cwd=work)
    assert p.returncode == 0, p.stderr
    # Gitea индексирует ветку асинхронно: ждём, пока API её увидит (иначе разбор сразу после пуша ловит 404)
    assert wait(lambda: gitea.get(f"/api/v1/repos/{USER}/{name}/branches/main").status_code == 200, 60, 1), f"Gitea не показал ветку main у {name}"
    return work, sh("git", "rev-parse", "HEAD", cwd=work).stdout.strip()


def inspect(repo, token=None, provider="gitea"):
    return api.post("/api/onboarding/inspect", json={"url": f"{GITEA}/{USER}/{repo}", "provider": provider, "token": token})


def node_ready(ns, slug, timeout=240):
    return wait(lambda: kc("-n", ns, "get", "deploy", slug, "-o", "jsonpath={.status.readyReplicas}").stdout.strip() == "1", timeout)


def serves(ns, slug, needle, timeout=90, port=8080):
    def probe():
        out = kc("-n", ns, "run", f"curl-{int(time.time()) % 10000}", "--rm", "-i", "--restart=Never", "--image=busybox:1.36", "--",
                 "wget", "-qO-", f"http://{slug}.{ns}.svc.cluster.local:{port}/", timeout=60)
        return needle in out.stdout
    return wait(probe, timeout, 5)


def cleanup():
    kc("delete", "ns", RBAC_NS, "--ignore-not-found", "--wait=false")
    kc("delete", "clusterrole", f"{SA.removesuffix('-splitwave')}-splitwave-deployer", f"{REL}-splitwave-provisioner", "--ignore-not-found")
    kc("delete", "clusterrolebinding", f"{REL}-splitwave-provisioner", "--ignore-not-found")
    for ns in [INFRA] + SLUGS:
        kc("delete", "ns", ns, "--ignore-not-found", "--wait=false")
    out = sh("sudo", "-n", "nft", "-a", "list", "chain", "inet", "filter", "input").stdout
    for line in out.splitlines():
        if RULE in line and "# handle" in line:
            sh("sudo", "-n", "nft", "delete", "rule", "inet", "filter", "input", "handle", line.rsplit("handle", 1)[1].strip())


try:
    for ns in [INFRA] + SLUGS:
        kc("delete", "ns", ns, "--ignore-not-found", "--wait=true")
    kc("create", "ns", INFRA)
    kc("-n", INFRA, "apply", "-f", "-", stdin=f"""
apiVersion: apps/v1
kind: Deployment
metadata: {{name: gitea}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app: gitea}}}}
  template:
    metadata: {{labels: {{app: gitea}}}}
    spec:
      securityContext: {{fsGroup: 1000}}
      containers:
      - name: gitea
        image: gitea/gitea:1.22-rootless
        env:
        - {{name: GITEA__security__INSTALL_LOCK, value: "true"}}
        - {{name: GITEA__server__ROOT_URL, value: "{GITEA}/"}}
        - {{name: GITEA__server__DISABLE_SSH, value: "true"}}
        - {{name: GITEA__database__DB_TYPE, value: sqlite3}}
        - {{name: GITEA__webhook__ALLOWED_HOST_LIST, value: "*"}}
        - {{name: GITEA__service__DISABLE_REGISTRATION, value: "true"}}
        ports: [{{containerPort: 3000}}]
        readinessProbe: {{httpGet: {{path: /api/healthz, port: 3000}}, initialDelaySeconds: 10, periodSeconds: 3}}
        volumeMounts: [{{name: data, mountPath: /var/lib/gitea}}, {{name: conf, mountPath: /etc/gitea}}]
      volumes: [{{name: data, emptyDir: {{}}}}, {{name: conf, emptyDir: {{}}}}]
---
apiVersion: v1
kind: Service
metadata: {{name: gitea}}
spec: {{type: NodePort, selector: {{app: gitea}}, ports: [{{port: 3000, nodePort: {GITEA_PORT}}}]}}
""")
    ok = wait(lambda: kc("-n", INFRA, "get", "deploy", "gitea", "-o", "jsonpath={.status.readyReplicas}").stdout.strip() == "1", 300)
    check("Gitea поднят в кластере", ok)
    pod = kc("-n", INFRA, "get", "pod", "-l", "app=gitea", "-o", "jsonpath={.items[0].metadata.name}").stdout.strip()
    r = kc("-n", INFRA, "exec", pod, "--", "gitea", "admin", "user", "create", "--admin", "--username", USER, "--password", PASSWORD,
           "--email", "e2e@example.com", "--must-change-password=false")
    check("администратор Gitea создан", r.returncode == 0, r.stderr[-200:])
    tok = gitea.post(f"/api/v1/users/{USER}/tokens", json={"name": "wiz", "scopes": ["read:repository"]})
    api_token = tok.json().get("sha1", "") if tok.status_code == 201 else ""
    check("токен доступа Gitea (read:repository) выпущен", bool(api_token), tok.text[:150])

    # ---- репозитории
    web_files = {"Dockerfile": 'FROM busybox:1.36\nEXPOSE 8080\nRUN mkdir /www && echo onboarded-ok > /www/index.html\nCMD ["httpd","-f","-p","8080","-h","/www"]\n',
                 ".env.example": "DB_URL=postgres://x\nAPI_KEY=changeme\n"}
    web_dir, web_sha = make_repo("web", web_files)
    make_repo("node", {"package.json": json.dumps({"dependencies": {"express": "4"}, "scripts": {"start": "node ."}}), "index.js": "x"})
    make_repo("mono", {"backend/package.json": "{}", "frontend/package.json": "{}", "README.md": "x"})
    _, priv_sha = make_repo("secret", dict(web_files, **{"go.mod": "module x\n"}), private=True)

    # ---- разбор репозиториев
    who = f"--as=system:serviceaccount:{RBAC_NS}:{SA}"
    can = lambda *a: kc("auth", "can-i", *a, who).stdout.strip() == "yes"
    check("платформа работает под ServiceAccount Helm-чарта, а не под администратором", can("create", "namespaces") and not can("delete", "namespaces"))
    check("у платформы НЕТ лишних прав: pods create, secrets list по кластеру, nodes", not can("create", "pods", "-n", "default") and not can("list", "secrets", "-A") and not can("get", "nodes"))
    check("права платформы в кластере проверяются (access)", api.get("/api/onboarding/access").json().get("allowed") is True)
    r = inspect("web")
    p = r.json().get("proposal", {}) if r.status_code == 200 else {}
    check("web: провайдер, Dockerfile, порт EXPOSE и коммит определены верно",
          p.get("provider") == "gitea" and p.get("build_method") == "dockerfile" and p.get("port") == 8080 and p.get("port_source") == "EXPOSE" and p.get("head_sha") == web_sha
          and p.get("git_url") == f"{GITEA}/{USER}/web.git" and p.get("env_hints") == ["DB_URL", "API_KEY"], json.dumps(p)[:300] if p else r.text)
    p = inspect("node").json()["proposal"]
    check("node без Dockerfile: Express, предложен Dockerfile платформы (порт 3000)", (p["build_method"], p["framework"], p["dockerfile_generated"], p["port"]) == ("dockerfile", "Express", True, 3000), json.dumps(p)[:200])
    p = inspect("mono").json()["proposal"]
    check("монорепозиторий определён, предложены подпапки", any(w["code"] == "monorepo" for w in p["warnings"]) and {s["sub_path"] for s in p["alternatives"]["subdirs"]} == {"backend", "frontend"})
    r = inspect("secret")
    check("приватный репозиторий без токена: понятная ошибка", r.status_code == 422 and "токен" in r.json()["detail"].lower(), r.text[:200])
    r = inspect("secret", token=api_token)
    check("приватный репозиторий с токеном: разобран, помечен приватным", r.status_code == 200 and r.json()["proposal"]["private"] is True and api_token not in r.text, r.text[:200])
    r = inspect("secret", token="wrong-token")
    check("неверный токен: понятная ошибка", r.status_code == 422, r.text[:200])
    r = inspect("does-not-exist")
    check("несуществующий репозиторий: понятная ошибка", r.status_code == 422 and "не найден" in r.json()["detail"].lower(), r.text[:200])

    # ---- создание: платформа сама готовит приложение, собирает и выкатывает
    body = {"slug": "onb-web", "repo_full_name": f"{USER}/web", "provider": "gitea", "git_url": f"{GITEA}/{USER}/web.git", "build_method": "dockerfile",
            "port": 8080, "head_sha": web_sha}
    r = api.post("/api/onboarding/create", json=body)
    out = r.json() if r.status_code == 201 else {}
    check("мастер: проект создан, приложение подготовлено платформой, выкат запущен",
          r.status_code == 201 and out.get("provisioned") is True and out.get("deploy", {}).get("started") is True, r.text[:300])
    kinds = {x["kind"]: x["result"] for x in out.get("results", [])}
    check("создано: Namespace, RoleBinding, Deployment, Service", kinds == {"Namespace": "created", "RoleBinding": "created", "Deployment": "created", "Service": "created"}, str(kinds))
    ns_obj = json.loads(kc("get", "ns", "onb-web", "-o", "json").stdout or "{}")
    check("namespace помечен baseline (Pod Security) и managed-by", ns_obj.get("metadata", {}).get("labels", {}).get("pod-security.kubernetes.io/enforce") == "baseline")
    rel = wait(lambda: next((x for x in api.get("/api/projects/onb-web/releases").json() if x["status"] in ("deployed", "failed")), None), 480, 6)
    check("первый выкат: релиз deployed (kaniko собрал образ из Gitea)", rel and rel["status"] == "deployed", json.dumps(rel)[:300] if rel else "timeout")
    img = kc("-n", "onb-web", "get", "deploy", "onb-web", "-o", "jsonpath={.spec.template.spec.containers[0].image}").stdout.strip()
    check("Deployment переключён с заглушки на образ из реестра по дайджесту", img.startswith(f"{REG}/e2e/onb-web@sha256:"), img)
    check("под запущен и готов (образ скачан из реестра кластера)", node_ready("onb-web", "onb-web", 240), kc("-n", "onb-web", "get", "pods").stdout[-300:])
    check("приложение отвечает через Service (onboarded-ok)", serves("onb-web", "onb-web", "onboarded-ok"))
    spec = json.loads(kc("-n", "onb-web", "get", "deploy", "onb-web", "-o", "json").stdout)["spec"]["template"]["spec"]
    check("Deployment жёсткий: без токена ServiceAccount, capabilities drop ALL", spec.get("automountServiceAccountToken") is False
          and spec["containers"][0]["securityContext"]["capabilities"]["drop"] == ["ALL"])

    # ---- повторное создание не ломает существующее
    again = api.post("/api/onboarding/onb-web/provision").json()
    check("повторное применение идемпотентно (всё exists, ничего не перезаписано)", again.get("provisioned") is True and {x["result"] for x in again["results"]} == {"exists"}, str(again)[:200])
    r = api.post("/api/onboarding/create", json=body)
    check("тот же репозиторий второй раз: 409", r.status_code == 409)

    # ---- ручной путь: платформа ничего не создаёт, отдаёт YAML
    body2 = dict(body, slug="onb-manual", repo_full_name=f"{USER}/web-copy", provision=False, deploy=False)
    make_repo("web-copy", web_files)
    r = api.post("/api/onboarding/create", json=body2)
    out = r.json() if r.status_code == 201 else {}
    check("ручной режим: объекты не созданы, получен YAML", out.get("reason") == "manual" and "kind: Deployment" in out.get("manifest_yaml", "") and kc("get", "ns", "onb-manual").returncode != 0, r.text[:200])
    ap = kc("apply", "-f", "-", stdin=api.get("/api/onboarding/onb-manual/manifest").json()["manifest_yaml"])
    check("YAML из GET /manifest применён администратором (kubectl apply)", ap.returncode == 0, (ap.stderr or ap.stdout)[-300:])
    check("kubectl не предупреждает о нарушении Pod Security", "would violate" not in (ap.stderr + ap.stdout), (ap.stderr + ap.stdout)[-300:])
    r = api.post(f"/api/projects/onb-manual/redeploy?revision={web_sha}")
    rel = wait(lambda: next((x for x in api.get("/api/projects/onb-manual/releases").json() if x["status"] in ("deployed", "failed")), None), 480, 6)
    check("после ручного применения выкат проходит (релиз deployed)", r.status_code in (200, 202) and rel and rel["status"] == "deployed", json.dumps(rel)[:300] if rel else "timeout")
    check("и приложение отвечает", node_ready("onb-manual", "onb-manual", 240) and serves("onb-manual", "onb-manual", "onboarded-ok"))

    # ---- автовыкат: webhook Gitea с секретом проекта запускает новый выкат
    sh("sudo", "-n", "nft", "add", "rule", "inet", "filter", "input", "ip", "saddr", "10.42.0.0/16", "tcp", "dport", str(PLATFORM_PORT), "accept", "comment", RULE)
    hook_dir, _ = make_repo("hook", web_files)
    hbody = {"slug": "onb-hook", "repo_full_name": f"{USER}/hook", "provider": "gitea", "git_url": f"{GITEA}/{USER}/hook.git", "build_method": "dockerfile", "port": 8080,
             "deploy": False}
    out = api.post("/api/onboarding/create", json=hbody).json()
    wh = out["webhook"]
    check("webhook: путь и секрет проекта выданы", wh["path"] == "/webhook/gitea/onb-hook" and len(wh["secret"]) == 40, str(wh))
    hk = gitea.post(f"/api/v1/repos/{USER}/hook/hooks", json={"type": "gitea", "active": True, "events": ["push"], "branch_filter": "main",
                    "config": {"url": f"http://{NODE}:{PLATFORM_PORT}{wh['path']}", "content_type": "json", "secret": wh["secret"]}})
    check("webhook добавлен в Gitea", hk.status_code == 201, hk.text[:200])
    Path(hook_dir, "index-change.txt").write_text(str(time.time()))
    sh("git", "add", "-A", cwd=hook_dir)
    sh("git", "-c", "user.email=e2e@example.com", "-c", "user.name=e2e", "commit", "-q", "-m", "change", cwd=hook_dir)
    sh("git", "push", "-q", f"http://{USER}:{PASSWORD}@{NODE}:{GITEA_PORT}/{USER}/hook.git", "main", cwd=hook_dir)
    new_sha = sh("git", "rev-parse", "HEAD", cwd=hook_dir).stdout.strip()
    rel = wait(lambda: next((x for x in api.get("/api/projects/onb-hook/releases").json() if x["git_revision"] == new_sha and x["status"] in ("deployed", "failed")), None), 540, 6)
    check("push в репозиторий → webhook → сборка → релиз deployed на новом коммите", rel and rel["status"] == "deployed", json.dumps(rel)[:300] if rel else "timeout")
    check("приложение после автовыката отвечает", node_ready("onb-hook", "onb-hook", 240) and serves("onb-hook", "onb-hook", "onboarded-ok"))

    # ---- репозитории БЕЗ Dockerfile: платформа предлагает свой, мастер создаёт проект с его текстом; сборка и запуск настоящие
    PY_APP = ("import os\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\n\nclass H(BaseHTTPRequestHandler):\n"
              "    def do_GET(self):\n        self.send_response(200)\n        self.end_headers()\n        self.wfile.write(b'gen-py-ok')\n\n"
              "HTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8000'))), H).serve_forever()\n")
    GO_APP = ('package main\n\nimport (\n\t"net/http"\n\t"os"\n)\n\nfunc main() {\n\thttp.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("gen-go-ok")) })\n'
              '\thttp.ListenAndServe(":"+os.Getenv("PORT"), nil)\n}\n')
    gen = {
        "gen-node": ("node", {"package.json": json.dumps({"name": "g", "scripts": {"start": "node server.js"}}),
                              "server.js": "require('http').createServer((q, r) => r.end('gen-node-ok')).listen(process.env.PORT || 3000)\n"}, 3000, "gen-node-ok"),
        "gen-py": ("python", {"requirements.txt": "# без зависимостей\n", "main.py": PY_APP}, 8000, "gen-py-ok"),
        "gen-go": ("go", {"go.mod": "module gen\n\ngo 1.22\n", "main.go": GO_APP}, 8080, "gen-go-ok"),
        "gen-static": ("static", {"index.html": "<html><body>gen-static-ok</body></html>\n"}, 8080, "gen-static-ok"),
    }
    gen_sha = {}
    for name, (lang, files, port, needle) in gen.items():
        _, sha = make_repo(name, files)
        r = inspect(name)
        pr = r.json().get("proposal", {}) if r.status_code == 200 else {}
        tpl = pr.get("dockerfile_template") or {}
        check(f"{name}: нет Dockerfile → платформа предложила свой для «{lang}» на порту {port}",
              pr.get("dockerfile_generated") is True and tpl.get("language") == lang and pr.get("port") == port and tpl.get("content", "").startswith("FROM "), json.dumps(pr)[:300] or r.text)
        content = tpl.get("content", "")
        if name == "gen-node":                                   # пользователь правит текст: правка должна попасть в образ
            content = content.replace("USER node", "ENV EDITED_MARK=yes\nUSER node")
        b = {"slug": name, "repo_full_name": f"{USER}/{name}", "provider": "gitea", "git_url": f"{GITEA}/{USER}/{name}.git", "build_method": "dockerfile",
             "dockerfile_content": content, "port": port, "head_sha": sha, "sub_path": pr.get("sub_path", "")}
        r = api.post("/api/onboarding/create", json=b)
        check(f"{name}: проект создан мастером с Dockerfile платформы, выкат запущен", r.status_code == 201 and r.json().get("deploy", {}).get("started") is True, r.text[:200])
        gen_sha[name] = sha
    job = json.loads(kc("-n", "gen-node", "get", "jobs", "-o", "json").stdout or '{"items": []}')["items"]
    envs = [e for j in job for c in j["spec"]["template"]["spec"]["initContainers"] for e in c.get("env", [])]
    check("задание сборки получило Dockerfile платформы (DSP_DOCKERFILE) и kaniko собирает по Dockerfile.platform",
          any(e["name"] == "DSP_DOCKERFILE" and e.get("value", "").startswith("FROM node:") for e in envs)
          and any("--dockerfile=Dockerfile.platform" in c.get("args", []) for j in job for c in j["spec"]["template"]["spec"]["containers"]), str(envs)[:200])
    check("GET /dockerfile отдаёт сохранённый текст с правкой пользователя", "EDITED_MARK" in api.get("/api/projects/gen-node/dockerfile").json().get("content", ""))
    for name, (lang, files, port, needle) in gen.items():
        rel = wait(lambda n=name: next((x for x in api.get(f"/api/projects/{n}/releases").json() if x["status"] in ("deployed", "failed")), None), 540, 6)
        check(f"{name}: сборка по сгенерированному Dockerfile прошла (релиз deployed)", rel and rel["status"] == "deployed", json.dumps(rel)[:400] if rel else "timeout")
        ok = node_ready(name, name, 240)
        check(f"{name}: под запущен", ok, kc("-n", name, "get", "pods").stdout[-300:] + kc("-n", name, "logs", f"deploy/{name}", "--tail=8").stdout[-400:])
        check(f"{name}: приложение отвечает «{needle}»", ok and serves(name, name, needle, 90, port))
    envdump = kc("-n", "gen-node", "exec", "deploy/gen-node", "--", "env").stdout
    check("правка пользователя в Dockerfile попала в запущенный образ (EDITED_MARK=yes)", "EDITED_MARK=yes" in envdump)
    uid = kc("-n", "gen-node", "exec", "deploy/gen-node", "--", "id", "-u").stdout.strip()
    check("приложение запущено без root (uid ≠ 0)", uid not in ("", "0"), uid)
    st = api.get("/api/projects/gen-py/status").json()
    check("статус проекта с Dockerfile: ready и образ виден (не «Сборка…»)", st.get("state") == "ready" and (st.get("latest_image") or "").startswith(f"{REG}/e2e/gen-py@sha256:"), json.dumps(st)[:200])

    # ---- приватный репозиторий: токен используется для сборки, но не попадает в задания и поды
    pbody = {"slug": "onb-priv", "repo_full_name": f"{USER}/secret", "provider": "gitea", "git_url": f"{GITEA}/{USER}/secret.git", "build_method": "dockerfile",
             "port": 8080, "head_sha": priv_sha, "git_token": api_token, "git_username": USER}
    r = api.post("/api/onboarding/create", json=pbody)
    check("приватный репозиторий: проект создан мастером с токеном", r.status_code == 201 and r.json().get("deploy", {}).get("started") is True, r.text[:200])
    rel = wait(lambda: next((x for x in api.get("/api/projects/onb-priv/releases").json() if x["status"] in ("deployed", "failed")), None), 480, 6)
    check("сборка приватного репозитория по токену прошла (релиз deployed)", rel and rel["status"] == "deployed", json.dumps(rel)[:300] if rel else "timeout")
    # ---- токен приватного репозитория не утёк
    dump = kc("get", "jobs,pods,deploy", "-A", "-o", "json").stdout
    check("токен доступа не попал в описания заданий, подов и Deployment кластера", api_token and api_token not in dump)
finally:
    if not os.environ.get("E2E_KEEP"):
        cleanup()

print(f"\n{sum(results)}/{len(results)} проверок пройдено")
sys.exit(0 if all(results) else 1)
