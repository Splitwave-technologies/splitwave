from datetime import datetime, timedelta, timezone

from app.db.base import SessionLocal
from app.db.models import AuditEvent, Environment, Project, Release

BODY = {"slug": "shop", "repo_full_name": "acme/shop", "environments": [
    {"name": "prod", "namespace": "shop", "deployment_name": "shop", "container_name": "shop"}]}


def _seed(client, admin):
    client.post("/api/projects", json=BODY, headers=admin)
    db = SessionLocal()
    env = db.query(Environment).one()
    now = datetime.now(timezone.utc)
    for status, ago_days, took in [("deployed", 0, 60), ("deployed", 1, 120), ("failed", 1, None), ("building", 0, None)]:
        created = now - timedelta(days=ago_days, minutes=5)
        db.add(Release(environment_id=env.id, status=status, triggered_by="api", created_at=created,
                       deployed_at=created + timedelta(seconds=took) if took else None))
    db.commit(); db.close()


def test_overview_aggregates(client, admin):
    _seed(client, admin)
    o = client.get("/api/overview?days=7", headers=admin).json()
    assert o["totals"]["projects"] == 1 and o["totals"]["environments"] == 1 and o["totals"]["releases"] == 4
    assert o["success_rate"] == round(2 / 3, 4)                      # 2 деплоя и 1 сбой; сборка «в процессе» не считается
    assert o["avg_build_seconds"] == 90
    assert len(o["deploys_by_day"]) == 7
    today = o["deploys_by_day"][-1]
    assert today["deployed"] == 1 and today["other"] == 1
    assert o["by_project"][0]["slug"] == "shop" and o["by_project"][0]["failed"] == 1
    assert o["by_project"][0]["last_deploy_at"]


def test_overview_failure_reasons_and_denied(client, admin, make_token):
    _seed(client, admin)
    db = SessionLocal()
    db.add(AuditEvent(actor="webhook", action="deploy_failed", detail={"reason": "build_timeout"}))
    db.add(AuditEvent(actor="webhook", action="deploy_failed", detail={"reason": "build_timeout"}))
    db.commit(); db.close()
    client.post("/api/projects/shop/redeploy", headers=make_token("viewer"))       # 403 -> access_denied
    o = client.get("/api/overview", headers=admin).json()
    assert o["failures"][0] == {"reason": "build_timeout", "count": 2}
    assert o["denied_24h"] >= 1


def test_recent_activity_only_for_audit_readers(client, admin, make_token):
    _seed(client, admin)
    assert client.get("/api/overview", headers=admin).json()["recent"]
    assert client.get("/api/overview", headers=make_token("developer")).json()["recent"] is None


def test_overview_needs_auth_and_validates_days(client, admin):
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview?days=0", headers=admin).status_code == 422
    assert client.get("/api/overview?days=365", headers=admin).status_code == 422


def test_empty_platform_has_no_rates(client, admin):
    o = client.get("/api/overview", headers=admin).json()
    assert o["success_rate"] is None and o["avg_build_seconds"] is None and o["by_project"] == []
