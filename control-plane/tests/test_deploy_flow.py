from app.db.base import SessionLocal
from app.db.models import AuditEvent, Environment, Project, Release
from app.routers import webhooks


def _ids():
    db = SessionLocal()
    p = db.query(Project).filter_by(slug="demo").one()
    e = db.query(Environment).filter_by(project_id=p.id).one()
    ids = (p.id, e.id)
    db.close()
    return ids


def test_build_failure_marks_release_failed(project, monkeypatch):
    monkeypatch.setattr(webhooks.kpack, "get_status", lambda *a, **k: {"latest_image": None, "ready": False})
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(webhooks.kpack, "trigger_build", boom)

    pid, eid = _ids()
    webhooks.run_build_and_deploy(pid, eid, "https://x/y.git", "abc123", "webhook")

    db = SessionLocal()
    rel = db.query(Release).one()
    assert rel.status == "failed" and "RuntimeError: boom" in rel.error_message
    ev = db.query(AuditEvent).filter_by(action="deploy_failed").one()
    assert ev.detail["reason"] == "build_error"
    db.close()


def test_timeout_is_reported_as_build_timeout(project, monkeypatch):
    monkeypatch.setattr(webhooks.kpack, "get_status", lambda *a, **k: {"latest_image": None, "ready": False})
    monkeypatch.setattr(webhooks.kpack, "trigger_build", lambda *a, **k: None)
    def timeout(*a, **k):
        raise TimeoutError("did not complete")
    monkeypatch.setattr(webhooks.kpack, "wait_for_new_build", timeout)

    pid, eid = _ids()
    webhooks.run_build_and_deploy(pid, eid, "https://x/y.git", "abc123", "webhook")
    db = SessionLocal()
    assert db.query(AuditEvent).filter_by(action="deploy_failed").one().detail["reason"] == "build_timeout"
    db.close()


def test_deploy_error_after_build_is_recorded(project, monkeypatch):
    monkeypatch.setattr(webhooks.kpack, "get_status", lambda *a, **k: {"latest_image": None, "ready": False})
    monkeypatch.setattr(webhooks.kpack, "trigger_build", lambda *a, **k: None)
    monkeypatch.setattr(webhooks.kpack, "wait_for_new_build", lambda *a, **k: "reg/acme/demo@sha256:1")
    def deploy_fail(*a, **k):
        raise RuntimeError("k8s unavailable")
    monkeypatch.setattr(webhooks.deploy, "deploy_image", deploy_fail)

    pid, eid = _ids()
    webhooks.run_build_and_deploy(pid, eid, "https://x/y.git", "abc123", "webhook")
    db = SessionLocal()
    assert db.query(Release).one().status == "failed"
    assert db.query(AuditEvent).filter_by(action="deploy_failed").one().detail["reason"] == "deploy_error"
    db.close()


def test_successful_deploy_syncs_secrets_before_image(project, admin, client, fake_k8s, monkeypatch):
    core, apps = fake_k8s
    client.put("/api/projects/demo/secrets/DB_PASSWORD", json={"value": "pw"}, headers=admin)

    order = []
    monkeypatch.setattr(webhooks.kpack, "get_status", lambda *a, **k: {"latest_image": None, "ready": False})
    monkeypatch.setattr(webhooks.kpack, "trigger_build", lambda *a, **k: None)
    monkeypatch.setattr(webhooks.kpack, "wait_for_new_build", lambda *a, **k: "reg/acme/demo@sha256:new")
    monkeypatch.setattr("app.services.secrets.sync_to_cluster", lambda *a, **k: order.append("secrets") or 1)
    monkeypatch.setattr(webhooks.deploy, "deploy_image", lambda *a, **k: order.append("image"))

    pid, eid = _ids()
    webhooks.run_build_and_deploy(pid, eid, "https://x/y.git", "abc123", "webhook")

    assert order == ["secrets", "image"]
    db = SessionLocal()
    rel = db.query(Release).one()
    assert rel.status == "deployed" and rel.image_digest == "reg/acme/demo@sha256:new"
    assert db.query(AuditEvent).filter_by(action="deploy").count() == 1
    db.close()


def test_deployed_at_is_real_time_not_transaction_start(project, monkeypatch):
    import time
    from datetime import datetime, timezone
    monkeypatch.setattr(webhooks.kpack, "get_status", lambda *a, **k: {"latest_image": None, "ready": False})
    monkeypatch.setattr(webhooks.kpack, "trigger_build", lambda *a, **k: None)
    def slow_build(*a, **k):
        time.sleep(1.2)
        return "reg/acme/demo@sha256:new"
    monkeypatch.setattr(webhooks.kpack, "wait_for_new_build", slow_build)
    monkeypatch.setattr("app.services.secrets.sync_to_cluster", lambda *a, **k: 0)
    monkeypatch.setattr(webhooks.deploy, "deploy_image", lambda *a, **k: None)

    pid, eid = _ids()
    webhooks.run_build_and_deploy(pid, eid, "https://x/y.git", "abc123", "webhook")

    db = SessionLocal()
    rel = db.query(Release).one()
    created = rel.created_at if rel.created_at.tzinfo else rel.created_at.replace(tzinfo=timezone.utc)
    deployed = rel.deployed_at if rel.deployed_at.tzinfo else rel.deployed_at.replace(tzinfo=timezone.utc)
    assert (deployed - created).total_seconds() >= 1.0                       # длительность сборки видна во времени релиза
    assert abs((datetime.now(timezone.utc) - deployed).total_seconds()) < 30
    db.close()


def test_redeploying_the_same_revision_reuses_the_built_image(project, monkeypatch):
    monkeypatch.setattr(webhooks.kpack, "get_status", lambda *a, **k: {"latest_image": "reg/acme/demo@sha256:built", "ready": True})
    monkeypatch.setattr(webhooks.kpack, "trigger_build", lambda *a, **k: False)   # kpack: ревизия та же
    def must_not_wait(*a, **k):
        raise AssertionError("ждать новую сборку не нужно")
    monkeypatch.setattr(webhooks.kpack, "wait_for_new_build", must_not_wait)
    deployed = []
    monkeypatch.setattr("app.services.secrets.sync_to_cluster", lambda *a, **k: 0)
    monkeypatch.setattr(webhooks.deploy, "deploy_image", lambda dep, con, image, ns=None, cluster=None: deployed.append(image))

    pid, eid = _ids()
    webhooks.run_build_and_deploy(pid, eid, "https://x/y.git", "abc123", "api:me")

    db = SessionLocal()
    rel = db.query(Release).one()
    assert rel.status == "deployed" and rel.image_digest == "reg/acme/demo@sha256:built"
    assert deployed == ["reg/acme/demo@sha256:built"]
    db.close()
