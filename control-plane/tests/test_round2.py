"""Замечания реального теста на VPS: HTTPS для приложений, смена домена после создания, логи подов, логин-email, подсказка о нужных БД/хранилище."""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from kubernetes.client.exceptions import ApiException

from app.config import settings
from app.db.base import SessionLocal
from app.db.models import AuditEvent
from app.services import kubeclient, manifests, provision, repoinspect, runtime

ROOT = Path(__file__).resolve().parents[2]
TR = "traefik.ingress.kubernetes.io/"


# ---------- HTTPS в Ingress приложений ----------

def ing(**kw):
    return manifests.ingress_doc(name="shop", namespace="shop-prod", host="app.test.example.com", **kw)


def test_ingress_without_tls_by_default():
    d = ing()
    assert "annotations" not in d["metadata"] and "tls" not in d["spec"] and d["spec"]["rules"][0]["host"] == "app.test.example.com"


def test_ingress_auto_uses_the_acme_resolver(monkeypatch):
    monkeypatch.setattr(settings, "ingress_tls", "auto")
    d = ing()
    a = d["metadata"]["annotations"]
    assert a[TR + "router.tls"] == "true" and a[TR + "router.entrypoints"] == "websecure" and a[TR + "router.tls.certresolver"] == "le"
    assert d["spec"]["tls"] == [{"hosts": ["app.test.example.com"]}]
    monkeypatch.setattr(settings, "ingress_cert_resolver", "letsencrypt-prod")
    assert ing()["metadata"]["annotations"][TR + "router.tls.certresolver"] == "letsencrypt-prod"


def test_ingress_custom_certificate_has_no_resolver_and_may_name_a_secret(monkeypatch):
    monkeypatch.setattr(settings, "ingress_tls", "custom")
    d = ing()
    assert TR + "router.tls.certresolver" not in d["metadata"]["annotations"] and d["spec"]["tls"] == [{"hosts": ["app.test.example.com"]}]
    monkeypatch.setattr(settings, "ingress_tls_secret", "wildcard-tls")
    assert ing()["spec"]["tls"] == [{"hosts": ["app.test.example.com"], "secretName": "wildcard-tls"}]


def test_extra_annotations_and_class_and_bad_json(monkeypatch):
    monkeypatch.setattr(settings, "ingress_annotations", json.dumps({"cert-manager.io/cluster-issuer": "letsencrypt", "n": 5}))
    monkeypatch.setattr(settings, "ingress_class", "nginx")
    d = ing()
    assert d["metadata"]["annotations"] == {"cert-manager.io/cluster-issuer": "letsencrypt", "n": "5"} and d["spec"]["ingressClassName"] == "nginx"
    for bad in ("not json", "[1,2]", '"str"'):
        monkeypatch.setattr(settings, "ingress_annotations", bad)
        assert "annotations" not in ing()["metadata"]


@pytest.mark.parametrize("host", ["*.example.com", "app_x.example.com", "http://x.io", "a b.io", "", "-a.io"])
def test_ingress_rejects_bad_hosts(host):
    with pytest.raises(ValueError):
        manifests.ingress_doc(name="shop", namespace="n", host=host)


def test_wizard_manifests_include_tls_when_a_domain_is_given(monkeypatch):
    monkeypatch.setattr(settings, "ingress_tls", "auto")
    docs = manifests.build(name="shop", namespace="shop-prod", container="shop", port=8080, host="shop.test.example.com")
    i = [d for d in docs if d["kind"] == "Ingress"][0]
    assert i["spec"]["tls"] and i["metadata"]["annotations"][TR + "router.tls.certresolver"] == "le"
    assert not [d for d in manifests.build(name="shop", namespace="shop-prod", container="shop", port=8080) if d["kind"] == "Ingress"]


@pytest.mark.skipif(not (ROOT / "deploy/helm").is_dir() or not shutil.which("helm"), reason="нужен каталог deploy и helm")
def test_chart_passes_tls_settings_and_grants_ingress_rights_only_when_asked():
    def render(*sets):
        out = subprocess.run(["helm", "template", "dsp", str(ROOT / "deploy/helm/splitwave"), "--set", "rbac.deployNamespaces={apps}", *sets],
                             capture_output=True, text=True, check=True).stdout
        return [d for d in yaml.safe_load_all(out) if d]

    def deployer_ingress_verbs(docs):
        role = [d for d in docs if d.get("kind") == "ClusterRole" and d["metadata"]["name"].endswith("-deployer")]
        return [set(r["verbs"]) for d in role for r in d["rules"] if "ingresses" in r["resources"]]

    def env(docs):
        dep = [d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith("splitwave")][0]
        return {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    plain = render()
    assert not deployer_ingress_verbs(plain) and "INGRESS_TLS" not in env(plain)
    tls = render("--set", "onboarding.tls=auto", "--set", "onboarding.certResolver=le2", "--set", "rbac.manageIngress=true")
    assert deployer_ingress_verbs(tls) == [{"get", "create", "update", "delete"}]
    assert env(tls)["INGRESS_TLS"] == "auto" and env(tls)["INGRESS_CERT_RESOLVER"] == "le2"
    assert deployer_ingress_verbs(render("--set", "provisioner.enabled=true"))                    # при provisioner.enabled — тоже


# ---------- фейковый кластер ----------

def api_error(status, reason=""):
    return ApiException(status=status, reason=reason or str(status))


class FakeNet:
    def __init__(self):
        self.items, self.calls = {}, []

    def read_namespaced_ingress(self, name, ns):
        if (ns, name) not in self.items:
            raise api_error(404)
        return self.items[(ns, name)]

    def create_namespaced_ingress(self, ns, doc):
        self.calls.append(("create", doc))
        self.items[(ns, doc["metadata"]["name"])] = SimpleNamespace(
            metadata=SimpleNamespace(resource_version="7", labels=doc["metadata"].get("labels")),
            spec=SimpleNamespace(rules=[SimpleNamespace(host=doc["spec"]["rules"][0]["host"])]))

    def replace_namespaced_ingress(self, name, ns, doc):
        self.calls.append(("replace", doc))
        self.items[(ns, name)] = SimpleNamespace(metadata=SimpleNamespace(resource_version="8", labels=doc["metadata"].get("labels")),
                                                 spec=SimpleNamespace(rules=[SimpleNamespace(host=doc["spec"]["rules"][0]["host"])]))

    def delete_namespaced_ingress(self, name, ns):
        self.calls.append(("delete", name))
        del self.items[(ns, name)]


class FakeCore:
    def __init__(self, ports=None, logs="line1\nsecret-value-123\n", pods=("shop-abc",), fail_log=None):
        self.ports, self.logs, self.pods, self.fail_log, self.log_calls = ports if ports is not None else [SimpleNamespace(name="http", port=8080)], logs, pods, fail_log, []

    def read_namespaced_service(self, name, ns):
        return SimpleNamespace(spec=SimpleNamespace(ports=self.ports))

    def list_namespaced_pod(self, ns, label_selector=""):
        return SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name=n)) for n in self.pods])

    def read_namespaced_pod_log(self, pod, ns, container=None, tail_lines=None, previous=False):
        self.log_calls.append({"pod": pod, "container": container, "tail": tail_lines, "previous": previous})
        if self.fail_log:
            raise self.fail_log
        return self.logs


class FakeApps:
    def read_namespaced_deployment(self, name, ns):
        return SimpleNamespace(spec=SimpleNamespace(selector=SimpleNamespace(match_labels={"app": name})))


@pytest.fixture
def cluster(monkeypatch):
    net, core = FakeNet(), FakeCore()
    monkeypatch.setattr(kubeclient, "networking_v1_api", lambda cluster=None: net)
    monkeypatch.setattr(kubeclient, "core_v1_api", lambda cluster=None: core)
    monkeypatch.setattr(kubeclient, "apps_v1_api", lambda cluster=None: FakeApps())
    return SimpleNamespace(net=net, core=core)


@pytest.fixture
def env(client, project, admin):
    return client.get("/api/projects/demo/environments", headers=admin).json()[0]


# ---------- смена домена ----------

def put_domain(client, headers, host, name="prod"):
    return client.put(f"/api/projects/demo/environments/{name}/domain", json={"host": host}, headers=headers)


def test_domain_create_replace_remove_and_read(client, env, admin, cluster, monkeypatch):
    monkeypatch.setattr(settings, "ingress_tls", "auto")
    r = put_domain(client, admin, "App.Test.Example.com ")
    assert r.status_code == 200 and r.json() == {"host": "app.test.example.com", "result": "created", "tls": "auto", "url": "https://app.test.example.com"}
    doc = cluster.net.calls[0][1]
    assert doc["metadata"]["name"] == env["deployment_name"] and doc["metadata"]["namespace"] == env["namespace"] and doc["spec"]["tls"]
    assert doc["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["port"] == {"name": "http"}
    assert client.get("/api/projects/demo/environments/prod/domain", headers=admin).json() == {"host": "app.test.example.com", "tls": "auto", "error": None}
    r = put_domain(client, admin, "other.test.example.com")
    assert r.json()["result"] == "updated" and cluster.net.calls[-1][1]["metadata"]["resourceVersion"] == "7"            # replace с актуальной версией
    assert put_domain(client, admin, None).json() == {"host": None, "result": "removed", "tls": "auto", "url": None}
    assert put_domain(client, admin, "").json()["result"] == "none"                                                       # убирать нечего
    db = SessionLocal()
    try:
        assert [e.detail["result"] for e in db.query(AuditEvent).filter_by(action="domain_set").order_by(AuditEvent.created_at).all()] == ["created", "updated", "removed", "none"]
    finally:
        db.close()


def test_domain_uses_the_service_port_number_when_it_has_no_name(client, env, admin, cluster, monkeypatch):
    cluster.core.ports = [SimpleNamespace(name=None, port=3000)]
    assert put_domain(client, admin, "x.test.example.com").status_code == 200
    assert cluster.net.calls[0][1]["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["port"] == {"number": 3000}
    cluster.core.ports = []
    cluster.net.items.clear()
    r = put_domain(client, admin, "x.test.example.com")
    assert r.status_code == 502 and "no ports" in r.json()["detail"]


def test_domain_validation_permissions_and_cluster_errors(client, env, admin, cluster, make_token, monkeypatch):
    for bad in ("*.example.com", "a b", "http://x.io", "-x.io", "x_y.io"):
        assert put_domain(client, admin, bad).status_code == 422, bad
    assert put_domain(client, make_token("devops"), "x.test.example.com").status_code == 403
    assert put_domain(client, make_token("developer"), "x.test.example.com").status_code == 403
    assert client.get("/api/projects/demo/environments/prod/domain", headers=make_token("viewer")).status_code == 200
    assert put_domain(client, admin, "x.test.example.com", name="nope").status_code == 404
    monkeypatch.setattr(FakeNet, "read_namespaced_ingress", lambda self, n, ns: (_ for _ in ()).throw(api_error(403)))
    r = put_domain(client, admin, "x.test.example.com")
    assert r.status_code == 403 and "provisioner.enabled" in r.json()["detail"]
    monkeypatch.setattr(FakeNet, "read_namespaced_ingress", lambda self, n, ns: (_ for _ in ()).throw(RuntimeError("down")))
    assert put_domain(client, admin, "x.test.example.com").status_code == 502
    g = client.get("/api/projects/demo/environments/prod/domain", headers=admin).json()
    assert g["host"] is None and g["error"] == "RuntimeError"


def test_domain_of_a_server_environment_is_refused(client, project, admin):
    from app.db.models import Environment
    db = SessionLocal()
    try:
        e = db.query(Environment).first(); e.server = "vps-1"; db.commit()
    finally:
        db.close()
    r = put_domain(client, admin, "x.test.example.com")
    assert r.status_code == 422 and "server" in r.json()["detail"]


# ---------- логи подов ----------

def logs(client, headers, pod="shop-abc", **q):
    return client.get(f"/api/projects/demo/pods/{pod}/logs", params={"environment": "prod", **q}, headers=headers)


def test_pod_logs_are_returned_with_secret_values_masked(client, env, admin, cluster, make_token):
    client.put("/api/projects/demo/secrets/DB_PASSWORD", json={"value": "secret-value-123"}, headers=admin)
    client.put("/api/projects/demo/secrets/SHORT", json={"value": "abc"}, headers=admin)              # короткие значения не маскируем
    cluster.core.logs = "starting abc\nconnect password=secret-value-123 ok\n"
    r = logs(client, make_token("developer"), tail=50, previous=True)
    assert r.status_code == 200 and r.text == "starting abc\nconnect password=*** ok\n" and r.headers["cache-control"] == "no-store"
    assert cluster.core.log_calls == [{"pod": "shop-abc", "container": env["container_name"], "tail": 50, "previous": True}]


def test_pod_logs_validation_permissions_and_errors(client, env, admin, cluster, make_token):
    assert logs(client, make_token("viewer")).status_code == 403
    assert logs(client, admin, pod="other-pod").status_code == 404                                         # чужие поды не отдаются
    for bad in ("../etc", "UPPER", "a b"):
        assert logs(client, admin, pod=bad).status_code in (404, 422), bad
    assert logs(client, admin, tail=0).status_code == 422 and logs(client, admin, tail=5000).status_code == 422
    assert logs(client, admin, environment="nope").status_code == 404
    cluster.core.fail_log = api_error(400)                                                                 # прошлого запуска нет
    assert logs(client, admin, previous=True).text == ""
    cluster.core.fail_log = api_error(500, "boom")
    assert logs(client, admin).status_code == 502


def test_mask_secrets_longest_first_and_ignores_empty():
    assert runtime.mask_secrets("a=abcdef b=abcdefgh", ["abcdef", "abcdefgh", "", None]) == "a=*** b=***"
    assert runtime.mask_secrets("x", []) == "x"


def test_pod_logs_are_truncated(monkeypatch):
    monkeypatch.setattr(kubeclient, "apps_v1_api", lambda cluster=None: FakeApps())
    core = FakeCore(logs="x" * (runtime.MAX_LOG_BYTES + 500))
    monkeypatch.setattr(kubeclient, "core_v1_api", lambda cluster=None: core)
    assert len(runtime.pod_logs("ns", "shop", "shop", "shop-abc")) == runtime.MAX_LOG_BYTES


# ---------- логин в виде email ----------

def test_user_can_be_created_with_an_email_login_and_it_is_normalised(client, admin):
    r = client.post("/api/users", json={"username": " Dev.Person@Example.com "}, headers=admin)
    assert r.status_code == 201 and r.json()["username"] == "dev.person@example.com"
    login = client.post("/api/auth/login", json={"username": "DEV.person@example.com", "password": r.json()["temporary_password"]})
    assert login.status_code in (200, 403)                                                                  # вход возможен (403 — потребуется смена пароля/2FA)
    assert client.post("/api/users", json={"username": "dev.person@example.com"}, headers=admin).status_code == 409
    for bad in ("a@", "ab", "bad name@x.io", "-x@y.io", "x" * 70):
        assert client.post("/api/users", json={"username": bad}, headers=admin).status_code == 422, bad


# ---------- подсказка о нужных внешних сервисах ----------

def test_detect_services_from_env_names():
    got = {x["service"]: x["names"] for x in repoinspect.detect_services(
        ["SPRING_DATASOURCE_URL", "DB_NAME", "MINIO_ENDPOINT", "REDIS_URL", "JWT_SECRET", "PORT", "RABBITMQ_URL", "AWS_REGION", "ELASTICSEARCH_URL"])}
    assert set(got) == {"database", "storage", "cache", "queue", "search"}
    assert got["database"] == ["SPRING_DATASOURCE_URL", "DB_NAME"] and "AWS_REGION" in got["storage"]
    assert repoinspect.detect_services(["JWT_SECRET", "PORT", "DEBUG", "API_KEY", "ADDRESS", "DBUS_X"]) == []
