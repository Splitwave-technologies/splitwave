"""Мастер подключения: манифесты приложения, проверка прав и создание ресурсов в кластере."""
import pytest
import yaml
from kubernetes import client as k8s
from kubernetes.client.exceptions import ApiException

from app.config import settings
from app.services import kubeclient, manifests, provision


def build(**kw):
    args = dict(name="shop", namespace="shop-prod", container="shop", port=8080)
    args.update(kw)
    return manifests.build(**args)


def by_kind(docs):
    return {d["kind"]: d for d in docs}


# ---------- манифесты ----------

def test_manifest_set_and_order():
    docs = build()
    assert [d["kind"] for d in docs] == ["Namespace", "RoleBinding", "Deployment", "Service"]
    k = by_kind(docs)
    assert k["Namespace"]["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "baseline"
    rb = k["RoleBinding"]
    assert rb["roleRef"]["name"] == "control-plane-deployer" and rb["metadata"]["namespace"] == "shop-prod"
    assert rb["subjects"][0]["name"] == settings.platform_service_account


def test_deployer_role_and_service_account_are_configurable(monkeypatch):
    """Helm называет роль и ServiceAccount по имени релиза: мастер должен ссылаться на них, а не на имена из ручной установки."""
    monkeypatch.setattr(settings, "deployer_cluster_role", "acme-dsp-deployer")
    monkeypatch.setattr(settings, "platform_service_account", "acme-dsp")
    monkeypatch.setattr(settings, "platform_namespace", "platform-system")
    rb = by_kind(build())["RoleBinding"]
    assert rb["roleRef"]["name"] == "acme-dsp-deployer" and rb["metadata"]["name"] == "acme-dsp-deployer"
    assert rb["subjects"] == [{"kind": "ServiceAccount", "name": "acme-dsp", "namespace": "platform-system"}]
    fake = FakeAuthz()
    monkeypatch.setattr(kubeclient, "authorization_v1_api", lambda cluster=None: fake)
    provision.check_access()
    assert ("rbac.authorization.k8s.io", "clusterroles", "bind", "acme-dsp-deployer") in fake.asked


def test_deployment_is_hardened_and_ready_for_first_deploy(monkeypatch):
    monkeypatch.setattr(settings, "app_pull_secret", "")
    d = by_kind(build(port=3000))["Deployment"]
    pod = d["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert c["name"] == "shop" and c["image"] == settings.placeholder_image
    assert {"name": "PORT", "value": "3000"} in c["env"] and c["ports"][0]["containerPort"] == 3000
    assert c["readinessProbe"]["tcpSocket"]["port"] == "http"
    assert c["securityContext"]["allowPrivilegeEscalation"] is False and c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    # регрессия: nginx от root падал на chown(), когда добавлялся только NET_BIND_SERVICE
    assert {"CHOWN", "SETUID", "SETGID", "NET_BIND_SERVICE"} <= set(c["securityContext"]["capabilities"]["add"])
    assert not {"SYS_ADMIN", "NET_RAW", "SYS_PTRACE", "NET_ADMIN"} & set(c["securityContext"]["capabilities"]["add"])
    assert pod["automountServiceAccountToken"] is False and "imagePullSecrets" not in pod
    assert d["spec"]["selector"]["matchLabels"].items() <= d["spec"]["template"]["metadata"]["labels"].items()
    svc = by_kind(build(port=3000))["Service"]["spec"]
    assert svc["ports"][0]["port"] == 3000 and svc["selector"] == {"app.kubernetes.io/name": "shop"}


def test_pull_secret_is_referenced_not_embedded(monkeypatch):
    monkeypatch.setattr(settings, "app_pull_secret", "ghcr-creds")
    docs = build()
    assert by_kind(docs)["Deployment"]["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "ghcr-creds"}]
    assert "Secret" not in by_kind(docs) and "dockerconfigjson" not in manifests.to_yaml(docs)


def test_ingress_only_with_host(monkeypatch):
    monkeypatch.setattr(settings, "ingress_class", "traefik")
    assert "Ingress" not in by_kind(build())
    ing = by_kind(build(host="shop.example.com"))["Ingress"]
    assert ing["spec"]["rules"][0]["host"] == "shop.example.com" and ing["spec"]["ingressClassName"] == "traefik"
    assert ing["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"] == "shop"
    monkeypatch.setattr(settings, "ingress_class", "")
    assert "ingressClassName" not in by_kind(build(host="a.example.com"))["Ingress"]["spec"]


def test_yaml_roundtrip():
    docs = build(host="shop.example.com")
    assert list(yaml.safe_load_all(manifests.to_yaml(docs))) == docs


@pytest.mark.parametrize("kw", [
    {"namespace": "kube-system"}, {"namespace": "kube-anything"}, {"namespace": "Bad_Name"}, {"name": "UPPER"}, {"container": "-x"},
    {"port": 0}, {"port": 70000}, {"host": "*.example.com"}, {"host": "bad host"}, {"host": "a" * 300 + ".com"},
])
def test_invalid_parameters_are_rejected(kw):
    with pytest.raises(ValueError):
        build(**kw)


def test_platform_namespaces_are_refused(monkeypatch):
    monkeypatch.setattr(settings, "platform_namespace", "platform-system")
    with pytest.raises(ValueError):
        build(namespace="platform-system")
    monkeypatch.setattr(settings, "build_namespace", "builds")
    with pytest.raises(ValueError):
        build(namespace="builds")


# ---------- проверка прав ----------

class FakeAuthz:
    def __init__(self, denied=(), boom=False):
        self.denied, self.boom, self.asked = set(denied), boom, []

    def create_self_subject_access_review(self, body):
        if self.boom:
            raise ApiException(status=403)
        a = body.spec.resource_attributes
        self.asked.append((a.group, a.resource, a.verb, a.name))
        return k8s.V1SelfSubjectAccessReview(spec=body.spec, status=k8s.V1SubjectAccessReviewStatus(allowed=(a.resource, a.verb) not in self.denied))


def test_access_review_all_allowed(monkeypatch):
    fake = FakeAuthz()
    monkeypatch.setattr(kubeclient, "authorization_v1_api", lambda cluster=None: fake)
    assert provision.check_access() == {"allowed": True, "missing": []}
    assert ("rbac.authorization.k8s.io", "clusterroles", "bind", settings.deployer_cluster_role) in fake.asked
    assert not any(r == "ingresses" for _, r, _, _ in fake.asked)
    provision.check_access(ingress=True)
    assert any(r == "ingresses" for _, r, _, _ in fake.asked)


def test_access_review_lists_missing_rights(monkeypatch):
    monkeypatch.setattr(kubeclient, "authorization_v1_api", lambda cluster=None: FakeAuthz(denied={("namespaces", "create"), ("deployments", "create")}))
    r = provision.check_access()
    assert r["allowed"] is False and r["missing"] == ["create namespaces", "create deployments"]


def test_access_review_failure_means_not_allowed(monkeypatch):
    monkeypatch.setattr(kubeclient, "authorization_v1_api", lambda cluster=None: FakeAuthz(boom=True))
    assert provision.check_access()["allowed"] is False


# ---------- создание ----------

class FakeApi:
    """Один объект изображает все API; записывает вызовы и умеет отвечать ошибкой на объект."""

    def __init__(self, fail=None, secret=None):
        self.calls, self.fail, self.secret = [], fail or {}, secret

    def _do(self, kind, name, ns=None):
        self.calls.append((kind, name, ns))
        if (kind, name) in self.fail:
            raise ApiException(status=self.fail[(kind, name)])

    def create_namespace(self, body): self._do("Namespace", body["metadata"]["name"])
    def create_namespaced_role_binding(self, ns, body): self._do("RoleBinding", body["metadata"]["name"], ns)
    def create_namespaced_deployment(self, ns, body): self._do("Deployment", body["metadata"]["name"], ns)
    def create_namespaced_service(self, ns, body): self._do("Service", body["metadata"]["name"], ns)
    def create_namespaced_ingress(self, ns, body): self._do("Ingress", body["metadata"]["name"], ns)
    def create_namespaced_secret(self, ns, body): self._do("Secret", body.metadata.name, ns)
    def read_namespaced_secret(self, name, ns): return self.secret


@pytest.fixture
def fake_cluster(monkeypatch):
    api = FakeApi()
    seen = []
    for fn in ("core_v1_api", "apps_v1_api", "rbac_v1_api", "networking_v1_api"):
        monkeypatch.setattr(kubeclient, fn, lambda cluster=None, _s=seen: (_s.append(cluster), api)[1])
    monkeypatch.setattr(settings, "app_pull_secret", "")
    return api, seen


def test_apply_creates_in_order(fake_cluster):
    api, _ = fake_cluster
    res = provision.apply(build(host="shop.example.com"))
    assert [r["kind"] for r in res] == ["Namespace", "RoleBinding", "Deployment", "Service", "Ingress"]
    assert all(r["result"] == "created" for r in res)
    assert [c[0] for c in api.calls] == ["Namespace", "RoleBinding", "Deployment", "Service", "Ingress"]
    assert all(c[2] == "shop-prod" for c in api.calls[1:])


def test_existing_objects_are_left_alone(fake_cluster):
    api, _ = fake_cluster
    api.fail = {("Namespace", "shop-prod"): 409, ("Deployment", "shop"): 409}
    res = provision.apply(build())
    assert {r["kind"]: r["result"] for r in res} == {"Namespace": "exists", "RoleBinding": "created", "Deployment": "exists", "Service": "created"}


def test_forbidden_raises_denied_with_object(fake_cluster):
    api, _ = fake_cluster
    api.fail = {("Deployment", "shop"): 403}
    with pytest.raises(provision.ProvisionDenied) as e:
        provision.apply(build())
    assert "Deployment shop" in e.value.what
    assert [c[0] for c in api.calls] == ["Namespace", "RoleBinding", "Deployment"]      # дальше не идём


def test_other_errors_are_reported(fake_cluster):
    api, _ = fake_cluster
    api.fail = {("Service", "shop"): 500}
    with pytest.raises(provision.ProvisionError):
        provision.apply(build())


def test_apply_targets_remote_cluster(fake_cluster):
    _, seen = fake_cluster
    provision.apply(build(), cluster="edge")
    assert seen and set(seen) == {"edge"}


def test_pull_secret_is_copied_after_rolebinding(fake_cluster, monkeypatch):
    api, _ = fake_cluster
    monkeypatch.setattr(settings, "app_pull_secret", "ghcr-creds")
    api.secret = k8s.V1Secret(type="kubernetes.io/dockerconfigjson", data={".dockerconfigjson": "e30="})
    res = provision.apply(build())
    assert [r["kind"] for r in res] == ["Namespace", "RoleBinding", "Secret", "Deployment", "Service"]
    assert ("Secret", "ghcr-creds", "shop-prod") in api.calls
