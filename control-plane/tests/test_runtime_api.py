from kubernetes import client as k8s

from app.services import kubeclient

BODY = {"slug": "shop", "repo_full_name": "acme/shop", "environments": [
    {"name": "prod", "namespace": "shop", "deployment_name": "shop", "container_name": "shop"}]}


def _pod(name, ready, restarts, waiting=None):
    state = k8s.V1ContainerState(waiting=k8s.V1ContainerStateWaiting(reason=waiting)) if waiting else k8s.V1ContainerState()
    return k8s.V1Pod(metadata=k8s.V1ObjectMeta(name=name), status=k8s.V1PodStatus(
        phase="Running", container_statuses=[k8s.V1ContainerStatus(name="c", image="i", image_id="x", ready=ready,
                                                                   restart_count=restarts, state=state)]))


def _mk(client, admin):
    client.post("/api/projects", json=BODY, headers=admin)


def test_runtime_reports_replicas_and_pods(client, admin, make_token, fake_k8s):
    core, _ = fake_k8s
    core.pods = [_pod("shop-b", False, 3, waiting="CrashLoopBackOff"), _pod("shop-a", True, 0)]
    _mk(client, admin)
    r = client.get("/api/projects/shop/runtime?environment=prod", headers=make_token("viewer")).json()
    assert r["available"] and (r["replicas"], r["ready"]) == (2, 1)
    assert [p["name"] for p in r["pods"]] == ["shop-a", "shop-b"]
    bad = r["pods"][1]
    assert bad["ready"] is False and bad["restarts"] == 3 and bad["reason"] == "CrashLoopBackOff"
    assert r["conditions"][0]["reason"] == "MinimumReplicasUnavailable"


def test_runtime_survives_cluster_errors(client, admin, monkeypatch):
    _mk(client, admin)
    def boom(cluster=None):
        raise RuntimeError("cluster down")
    monkeypatch.setattr(kubeclient, "apps_v1_api", boom)
    r = client.get("/api/projects/shop/runtime", headers=admin).json()
    assert r["available"] is False and "cluster down" in r["error"]
    assert client.get("/api/projects/shop/runtime?environment=nope", headers=admin).status_code == 404
    assert client.get("/api/projects/nope/runtime", headers=admin).status_code == 404


class FakeBuilds:
    def __init__(self, items):
        self.items = items

    def list_namespaced_custom_object(self, group, version, ns, plural, label_selector=None):
        assert plural == "builds" and label_selector == "image.kpack.io/image=shop"
        return {"items": self.items}


def _build(name, ts, status, reason="", rev="abc"):
    return {"metadata": {"name": name, "creationTimestamp": ts}, "spec": {"source": {"git": {"revision": rev}}},
            "status": {"latestImage": "reg/shop@sha256:1", "conditions": [{"type": "Succeeded", "status": status, "reason": reason, "message": "m"}]}}


def test_builds_are_listed_newest_first_with_status(client, admin, monkeypatch):
    _mk(client, admin)
    fake = FakeBuilds([_build("shop-build-1", "2026-09-01T10:00:00Z", "True"), _build("shop-build-3", "2026-09-03T10:00:00Z", "Unknown"),
                       _build("shop-build-2", "2026-09-02T10:00:00Z", "False", "BuildFailed")])
    monkeypatch.setattr(kubeclient, "custom_objects_api", lambda: fake)
    r = client.get("/api/projects/shop/builds", headers=admin).json()
    assert [b["name"] for b in r["builds"]] == ["shop-build-3", "shop-build-2", "shop-build-1"]
    assert [b["status"] for b in r["builds"]] == ["building", "failed", "succeeded"]
    assert r["builds"][1]["reason"] == "BuildFailed"


def test_build_logs_permissions_and_content(client, admin, make_token, fake_k8s):
    core, _ = fake_k8s
    core.logs = {"build": "compiling...", "prepare": "cloning..."}
    _mk(client, admin)
    dev, viewer = make_token("developer"), make_token("viewer")
    ok = client.get("/api/projects/shop/builds/shop-build-7/logs", headers=dev)
    assert ok.status_code == 200 and ok.headers["content-type"].startswith("text/plain")
    assert "=== prepare ===\ncloning..." in ok.text and "=== build ===\ncompiling..." in ok.text and "=== completion ===" in ok.text
    assert client.get("/api/projects/shop/builds/shop-build-7/logs", headers=viewer).status_code == 403


def test_build_logs_validation(client, admin, fake_k8s):
    core, _ = fake_k8s
    _mk(client, admin)
    for bad in ["other-build-1", "shop-build-1;rm", "SHOP-build-1", "shop-build-1%2F..%2Fx"]:
        assert client.get(f"/api/projects/shop/builds/{bad}/logs", headers=admin).status_code in (400, 404)
    core.missing_pod = True
    r = client.get("/api/projects/shop/builds/shop-build-9/logs", headers=admin)
    assert r.status_code == 404 and "build pod not found" in r.json()["detail"]


def test_kpack_builds_are_shown_even_if_kaniko_jobs_cannot_be_listed(client, admin, monkeypatch):
    """Регрессия: без доступа к кластеру для заданий (не ApiException) пропадал весь список, включая сборки kpack (kaniko_unavailable)."""
    from app.services import kaniko
    _mk(client, admin)
    monkeypatch.setattr(kubeclient, "custom_objects_api", lambda: FakeBuilds([_build("shop-build-1", "2026-09-01T10:00:00Z", "True")]))

    def boom(*a, **k):
        raise RuntimeError("cluster config is not available")
    monkeypatch.setattr(kaniko, "list_builds", boom)
    r = client.get("/api/projects/shop/builds", headers=admin).json()
    assert [b["name"] for b in r["builds"]] == ["shop-build-1"]
