"""Сборка по Dockerfile (kaniko): спецификация задания, ожидание результата, интеграция в выкат."""
import json
from types import SimpleNamespace as NS

import pytest
from kubernetes.client.exceptions import ApiException

from app.config import settings
from app.db.base import SessionLocal
from app.db.models import Environment, Project, Release
from app.routers import webhooks
from app.services import kaniko, kubeclient

ENV = {"name": "prod", "namespace": "shop", "deployment_name": "shop", "container_name": "shop"}


class FakeBatch:
    def __init__(self):
        self.jobs, self.status = {}, NS(succeeded=None, failed=None, conditions=[])

    def create_namespaced_job(self, ns, body):
        self.jobs[body["metadata"]["name"]] = (ns, body)

    def read_namespaced_job_status(self, name, ns):
        return NS(status=self.status)

    def list_namespaced_job(self, ns, label_selector=None):
        return NS(items=[NS(metadata=NS(name=n, creation_timestamp=__import__("datetime").datetime(2026, 10, 2, 10, 0, i), labels=b["metadata"]["labels"]),
                            status=self.status) for i, (n, (_, b)) in enumerate(self.jobs.items())])


class FakeCore:
    digest = "sha256:" + "ab" * 32

    def list_namespaced_pod(self, ns, label_selector=None):
        term = NS(message=self.digest)
        return NS(items=[NS(metadata=NS(name="p1"), status=NS(container_statuses=[NS(name="kaniko", state=NS(terminated=term))]))])

    def read_namespaced_pod_log(self, name, ns, container=None, tail_lines=None):
        return f"log of {container}: Dockerfile error at step 3"


@pytest.fixture
def cluster(client, admin, monkeypatch):
    batch, core = FakeBatch(), FakeCore()
    monkeypatch.setattr(kubeclient, "batch_v1_api", lambda: batch)
    monkeypatch.setattr(kubeclient, "core_v1_api", lambda cluster=None: core)
    monkeypatch.setattr(settings, "registry_prefix", "registry.example.com/team")
    monkeypatch.setattr(kaniko, "DEADLINE_GRACE", 0)
    monkeypatch.setattr(settings, "build_timeout_seconds", 1)
    return batch, core


def mk(client, admin, **extra):
    body = {"slug": "shop", "repo_full_name": "acme/shop", "environments": [ENV], "build_method": "dockerfile", **extra}
    r = client.post("/api/projects", json=body, headers=admin)
    assert r.status_code == 201, r.text
    db = SessionLocal()
    p, e = db.query(Project).one(), db.query(Environment).one()
    return p, e, db


def test_job_spec_has_no_secrets_and_pins_the_source(client, admin, cluster):
    batch, _ = cluster
    p, e, db = mk(client, admin, sub_path="services/api", dockerfile_path="deploy/Dockerfile")
    with pytest.raises(TimeoutError):                                   # задание не завершается: нас интересует только его спецификация
        kaniko.build_image(p, e, "https://github.com/acme/shop.git", "a" * 40, poll_seconds=0.05)
    (ns, body), = batch.jobs.values()
    spec = body["spec"]["template"]["spec"]
    args = spec["containers"][0]["args"]
    assert ns == "shop" and body["metadata"]["labels"]["platform.split-wave.com/image"] == "shop"
    assert "--context-sub-path=services/api" in args and "--dockerfile=deploy/Dockerfile" in args
    assert f"--destination=registry.example.com/team/shop:{'a' * 12}" in args and "--digest-file=/dev/termination-log" in args
    init = {e["name"]: e for e in spec["initContainers"][0]["env"]}
    assert init["GIT_URL"]["value"] == "https://github.com/acme/shop.git" and init["REVISION"]["value"] == "a" * 40
    assert "secretKeyRef" in init["GIT_TOKEN"]["valueFrom"] and "value" not in init["GIT_TOKEN"]      # токен только из Secret
    assert "$GIT_TOKEN" not in " ".join(args)                                                        # и не в аргументах kaniko
    assert body["spec"]["activeDeadlineSeconds"] == settings.build_timeout_seconds and body["spec"]["backoffLimit"] == 0


def test_success_returns_immutable_digest_reference(client, admin, cluster):
    batch, core = cluster
    batch.status = NS(succeeded=1, failed=None, conditions=[])
    p, e, db = mk(client, admin)
    assert kaniko.build_image(p, e, "https://h/a.git", "b" * 40, poll_seconds=0) == f"registry.example.com/team/shop@{core.digest}"


def test_failure_carries_log_tail_and_timeout_is_distinct(client, admin, cluster):
    batch, _ = cluster
    p, e, db = mk(client, admin)
    batch.status = NS(succeeded=None, failed=1, conditions=[NS(type="Failed", reason="BackoffLimitExceeded")])
    with pytest.raises(RuntimeError, match="Dockerfile error at step 3"):
        kaniko.build_image(p, e, "https://h/a.git", "c" * 40, poll_seconds=0)
    batch.status = NS(succeeded=None, failed=1, conditions=[NS(type="Failed", reason="DeadlineExceeded")])
    with pytest.raises(TimeoutError):
        kaniko.build_image(p, e, "https://h/a.git", "c" * 40, poll_seconds=0)


@pytest.mark.parametrize("sub,df", [("../etc", "Dockerfile"), ("/abs", "Dockerfile"), ("ok", "../../Dockerfile"), ("ok", "a b")])
def test_unsafe_paths_are_refused(client, admin, sub, df):
    r = client.post("/api/projects", json={"slug": "p1", "repo_full_name": "a/b", "environments": [ENV], "sub_path": sub, "dockerfile_path": df, "build_method": "dockerfile"}, headers=admin)
    assert r.status_code == 422


def test_revision_is_validated_and_never_interpolated(client, admin, cluster):
    p, e, db = mk(client, admin)
    with pytest.raises(ValueError):
        kaniko.build_image(p, e, "https://h/a.git", "x; rm -rf /", poll_seconds=0)
    script = kaniko.job_body(p, "shop", "shop", "https://h/a.git", "abc1234", "n")["spec"]["template"]["spec"]["initContainers"][0]["command"][2]
    assert "abc1234" not in script and '"$REVISION"' in script                                        # значения — через переменные окружения


def test_patch_project_build_settings(client, admin, make_token):
    client.post("/api/projects", json={"slug": "shop", "repo_full_name": "acme/shop", "environments": [ENV]}, headers=admin)
    r = client.patch("/api/projects/shop", json={"build_method": "dockerfile", "dockerfile_path": "docker/Dockerfile"}, headers=admin)
    assert r.json() == {"build_method": "dockerfile", "dockerfile_path": "docker/Dockerfile", "sub_path": "", "dockerfile_generated": False}
    assert client.patch("/api/projects/shop", json={"build_method": "nope"}, headers=admin).status_code == 422
    assert client.patch("/api/projects/shop", json={"sub_path": "../x"}, headers=admin).status_code == 422
    assert client.get("/api/projects/shop/source", headers=admin).json()["build_method"] == "dockerfile"
    assert client.patch("/api/projects/shop", json={"build_method": "buildpacks"}, headers=make_token("developer")).status_code == 403


def test_deploy_flow_uses_kaniko_for_dockerfile_projects(client, admin, cluster, monkeypatch):
    p, e, db = mk(client, admin)
    pid, eid = p.id, e.id
    deployed, used = [], []
    monkeypatch.setattr(webhooks.kaniko, "build_image", lambda *a, **k: used.append(a) or "reg/shop@sha256:" + "f" * 64)
    monkeypatch.setattr(webhooks.kpack, "trigger_build", lambda *a, **k: pytest.fail("kpack must not be used for dockerfile projects"))
    monkeypatch.setattr(webhooks.deploy, "deploy_image", lambda dep, con, image, ns=None, cluster=None: deployed.append(image))
    monkeypatch.setattr("app.services.secrets.sync_to_cluster", lambda *a, **k: 0)
    webhooks.run_build_and_deploy(pid, eid, "https://h/a.git", "d" * 40, "t")
    assert deployed == ["reg/shop@sha256:" + "f" * 64] and used
    r = SessionLocal().query(Release).one()
    assert r.status == "deployed" and r.image_digest.endswith("f" * 64)


def test_builds_list_merges_kaniko_jobs(client, admin, cluster, monkeypatch):
    batch, _ = cluster
    p, e, db = mk(client, admin)
    batch.status = NS(succeeded=1, failed=None, conditions=[])
    kaniko.build_image(p, e, "https://h/a.git", "e" * 40, poll_seconds=0)
    monkeypatch.setattr("app.services.runtime._list_kpack_builds", lambda *a, **k: [])
    rows = client.get("/api/projects/shop/builds", headers=admin).json()["builds"]
    assert rows and rows[0]["method"] == "dockerfile" and rows[0]["status"] == "succeeded" and "-kbuild-" in rows[0]["name"]
    logs = client.get(f"/api/projects/shop/builds/{rows[0]['name']}/logs", headers=admin)
    assert logs.status_code == 200 and "log of kaniko" in logs.text


def test_builds_list_without_kpack_crd_is_not_an_error(client, admin, cluster, monkeypatch):
    from kubernetes.client.rest import ApiException
    p, e, db = mk(client, admin)

    def no_crd(*a, **k):
        raise ApiException(status=404, reason="Not Found")
    monkeypatch.setattr("app.services.runtime.kubeclient.custom_objects_api", lambda *a, **k: NS(list_namespaced_custom_object=no_crd))
    r = client.get("/api/projects/shop/builds", headers=admin)
    assert r.status_code == 200 and r.json().get("available", True) is not False and "error" not in r.json()


def test_failed_release_reason_is_visible_only_to_those_who_can_deploy(client, admin, make_token):
    client.post("/api/projects", json={"slug": "shop", "repo_full_name": "acme/shop", "environments": [ENV]}, headers=admin)
    db = SessionLocal()
    env = db.query(Environment).one()
    db.add(Release(environment_id=env.id, status="failed", git_revision="a" * 40, triggered_by="t", error_message="docker build failed: exit status 7"))
    db.commit(); db.close()
    assert client.get("/api/projects/shop/releases", headers=admin).json()[0]["error_message"] == "docker build failed: exit status 7"
    assert client.get("/api/projects/shop/releases", headers=make_token("viewer")).json()[0]["error_message"] is None
