"""Ожидание готовности после выката: релиз «deployed» только когда приложение реально поднялось."""
import pytest
from kubernetes import client as k8s

from app.config import settings
from app.db.base import SessionLocal
from app.db.models import AuditEvent, Environment, Project, Release
from app.routers import webhooks
from app.services import deploy

IMG = "reg/acme/demo@sha256:new"


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _dep(replicas=2, updated=2, available=2, total=2, generation=2, observed=2, conditions=None):
    return k8s.V1Deployment(
        metadata=k8s.V1ObjectMeta(generation=generation),
        spec=k8s.V1DeploymentSpec(replicas=replicas, selector=k8s.V1LabelSelector(match_labels={"app": "demo"}),
                                  template=k8s.V1PodTemplateSpec(spec=k8s.V1PodSpec(containers=[k8s.V1Container(name="app", image=IMG)]))),
        status=k8s.V1DeploymentStatus(replicas=total, updated_replicas=updated, available_replicas=available, ready_replicas=available,
                                      observed_generation=observed, conditions=conditions or []))


def _pod(name, image=IMG, waiting=None, restarts=0, exit_code=None):
    state = k8s.V1ContainerState(waiting=k8s.V1ContainerStateWaiting(reason=waiting)) if waiting else k8s.V1ContainerState(running=k8s.V1ContainerStateRunning())
    last = k8s.V1ContainerState(terminated=k8s.V1ContainerStateTerminated(exit_code=exit_code, reason="Error")) if exit_code is not None else None
    return k8s.V1Pod(metadata=k8s.V1ObjectMeta(name=name), spec=k8s.V1PodSpec(containers=[k8s.V1Container(name="app", image=image)]),
                     status=k8s.V1PodStatus(container_statuses=[k8s.V1ContainerStatus(name="app", image=image, image_id="", ready=not waiting,
                                                                                    restart_count=restarts, state=state, last_state=last)]))


class Apps:
    def __init__(self, seq):
        self.seq, self.i = seq, 0

    def read_namespaced_deployment(self, name, namespace):
        d = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return d


class Core:
    def __init__(self, pods, log="", log_error=False):
        self.pods, self.log, self.log_error, self.log_calls = pods, log, log_error, []

    def list_namespaced_pod(self, namespace, label_selector=None):
        return k8s.V1PodList(items=self.pods)

    def read_namespaced_pod_log(self, name, namespace, container=None, tail_lines=None, previous=False):
        self.log_calls.append(previous)
        if self.log_error:
            raise RuntimeError("no log")
        return self.log


def _wait(monkeypatch, apps, core, clock=None, timeout=60, **kw):
    clock = clock or Clock()
    monkeypatch.setattr(deploy, "apps_v1_api", lambda cluster=None: apps)
    monkeypatch.setattr(deploy, "core_v1_api", lambda cluster=None: core)
    return deploy.wait_rollout("demo", "app", IMG, "ns", timeout=timeout, poll=3, sleep=clock.sleep, clock=clock.now, **kw), clock


def test_returns_when_all_replicas_updated_and_available(monkeypatch):
    apps = Apps([_dep(updated=0, available=0, total=2), _dep(updated=2, available=1, total=3), _dep(updated=2, available=2, total=2)])
    _, clock = _wait(monkeypatch, apps, Core([_pod("p1"), _pod("p2")]))
    assert clock.t == 6          # ждали два опроса и дождались готовности


def test_waits_for_generation_to_be_observed(monkeypatch):
    # контроллер ещё не увидел новую ревизию: старые счётчики «готово» не считаются успехом
    apps = Apps([_dep(generation=3, observed=2), _dep(generation=3, observed=3)])
    _, clock = _wait(monkeypatch, apps, Core([_pod("p1"), _pod("p2")]))
    assert clock.t == 3


def test_zero_replicas_is_not_waited(monkeypatch):
    _, clock = _wait(monkeypatch, Apps([_dep(replicas=0, updated=0, available=0, total=0)]), Core([]))
    assert clock.t == 0


def test_crashloop_fails_fast_with_masked_logs(monkeypatch):
    apps = Apps([_dep(updated=2, available=1, total=3)])
    core = Core([_pod("demo-xyz", waiting="CrashLoopBackOff", restarts=3, exit_code=1)], log="start\nconnect to postgres://u:sup3rsecret@db failed\n")
    with pytest.raises(deploy.RolloutError) as e:
        _wait(monkeypatch, apps, core, mask_values=["sup3rsecret"])
    msg = str(e.value)
    assert "CrashLoopBackOff" in msg and "demo-xyz" in msg and "exit code 1" in msg and "connect to postgres" in msg
    assert "sup3rsecret" not in msg and "***" in msg
    assert e.value.reason == "rollout_failed" and core.log_calls == [True]      # лог прошлого запуска (после падения)


def test_missing_secret_key_fails_immediately(monkeypatch):
    with pytest.raises(deploy.RolloutError) as e:
        _wait(monkeypatch, Apps([_dep(updated=2, available=1, total=3)]), Core([_pod("p", waiting="CreateContainerConfigError")], log_error=True))
    assert "CreateContainerConfigError" in str(e.value)


def test_image_pull_error_has_grace_period(monkeypatch):
    apps = Apps([_dep(updated=2, available=1, total=3)])
    with pytest.raises(deploy.RolloutError) as e:
        _wait(monkeypatch, apps, Core([_pod("p", waiting="ImagePullBackOff")]), timeout=300)
    assert "ImagePullBackOff" in str(e.value) and e.value.reason == "rollout_failed"
    assert apps.i > 10          # сначала ждали (реестр бывает медленным), а не упали на первом опросе


def test_old_pods_are_ignored(monkeypatch):
    apps = Apps([_dep(updated=1, available=1, total=2), _dep()])
    core = Core([_pod("old", image="reg/acme/demo@sha256:old", waiting="CrashLoopBackOff", restarts=9), _pod("new")])
    _wait(monkeypatch, apps, core)          # падающий под СТАРОЙ ревизии не считается провалом нового выката


def test_timeout_names_pods_and_counts(monkeypatch):
    apps = Apps([_dep(updated=2, available=1, total=3)])
    with pytest.raises(deploy.RolloutError) as e:
        _wait(monkeypatch, apps, Core([_pod("p1", waiting="ContainerCreating")]), timeout=30)
    assert e.value.reason == "rollout_timeout" and "updated 2/2, available 1/2" in str(e.value) and "p1(ContainerCreating)" in str(e.value)


def test_progress_deadline_exceeded(monkeypatch):
    cond = [k8s.V1DeploymentCondition(type="Progressing", status="False", reason="ProgressDeadlineExceeded", message="ReplicaSet has timed out")]
    with pytest.raises(deploy.RolloutError) as e:
        _wait(monkeypatch, Apps([_dep(updated=1, available=1, total=2, conditions=cond)]), Core([]))
    assert "ProgressDeadlineExceeded" in str(e.value)


def _ids():
    db = SessionLocal()
    p = db.query(Project).filter_by(slug="demo").one()
    e = db.query(Environment).filter_by(project_id=p.id).one()
    ids = (p.id, e.id)
    db.close()
    return ids


def _prepare(monkeypatch, project, wait):
    monkeypatch.setattr(webhooks.kpack, "get_status", lambda *a, **k: {"latest_image": None, "ready": False})
    monkeypatch.setattr(webhooks.kpack, "trigger_build", lambda *a, **k: None)
    monkeypatch.setattr(webhooks.kpack, "wait_for_new_build", lambda *a, **k: IMG)
    monkeypatch.setattr(webhooks.deploy, "deploy_image", lambda *a, **k: None)
    monkeypatch.setattr(webhooks.deploy, "wait_rollout", wait)
    monkeypatch.setattr(settings, "rollout_wait_enabled", True)


def test_release_is_failed_when_app_does_not_start(project, monkeypatch):
    def crash(*a, **k):
        raise deploy.RolloutError("CrashLoopBackOff: pod demo-1, restarts 4")
    _prepare(monkeypatch, project, crash)
    pid, eid = _ids()
    webhooks.run_build_and_deploy(pid, eid, "https://x/y.git", "abc", "webhook")
    db = SessionLocal()
    rel = db.query(Release).one()
    assert rel.status == "failed" and "CrashLoopBackOff" in rel.error_message and rel.deployed_at is None
    assert db.query(AuditEvent).filter_by(action="deploy_failed").one().detail["reason"] == "rollout_failed"
    db.close()


def test_release_is_deployed_only_after_the_wait_succeeds(project, monkeypatch):
    seen = []
    _prepare(monkeypatch, project, lambda *a, **k: seen.append(k.get("mask_values") is not None))
    pid, eid = _ids()
    webhooks.run_build_and_deploy(pid, eid, "https://x/y.git", "abc", "webhook")
    db = SessionLocal()
    assert db.query(Release).one().status == "deployed" and seen == [True]
    db.close()
