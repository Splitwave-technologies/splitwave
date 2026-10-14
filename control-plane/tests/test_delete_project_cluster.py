from kubernetes import client as k8s
from kubernetes.client.rest import ApiException


class _Core:
    def __init__(self, labels):
        self.labels, self.deleted = labels, []

    def read_namespace(self, ns):
        if ns not in self.labels:
            raise ApiException(status=404)
        return k8s.V1Namespace(metadata=k8s.V1ObjectMeta(name=ns, labels=self.labels[ns]))

    def delete_namespace(self, ns):
        self.deleted.append(ns)


def test_cluster_cleanup_touches_only_namespaces_made_by_the_platform(monkeypatch):
    from app.routers.projects import _delete_namespaces
    core = _Core({"mine": {"app.kubernetes.io/managed-by": "splitwave"},
                  "old": {"app.kubernetes.io/managed-by": "devsecops-platform"},
                  "foreign": {"app.kubernetes.io/managed-by": "helm"}})
    monkeypatch.setattr("app.services.kubeclient.core_v1_api", lambda cluster=None: core)
    out = _delete_namespaces([(None, "mine"), (None, "old"), (None, "foreign"), (None, "kube-system"), (None, "gone")])
    assert core.deleted == ["mine", "old"]
    assert out["deleted"] == ["mine", "old"] and set(out["skipped"]) == {"foreign", "kube-system", "gone"} and not out["failed"]


def test_delete_project_default_leaves_cluster_alone(client, admin, project, monkeypatch):
    called = []
    monkeypatch.setattr("app.routers.projects._delete_namespaces", lambda pairs: called.append(pairs) or {})
    r = client.delete(f"/api/projects/{project}", headers=admin)
    assert r.status_code == 200 and "cluster" not in r.json() and not called


def test_delete_project_with_cleanup_reports_cluster_result(client, admin, project, monkeypatch):
    monkeypatch.setattr("app.routers.projects._delete_namespaces", lambda pairs: {"deleted": [n for _, n in pairs], "skipped": [], "failed": []})
    r = client.delete(f"/api/projects/{project}?cleanup_cluster=true", headers=admin)
    assert r.status_code == 200 and r.json()["cluster"]["deleted"]
