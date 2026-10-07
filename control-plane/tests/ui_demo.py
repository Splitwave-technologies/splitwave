"""Демо-сервер для снимков интерфейса: как ui_server, но с проектами, историей релизов и аудита. Запуск: python tests/ui_demo.py (порт 18098)."""
import os, random, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ui_server  # noqa: F401,E402  (подменяет кластер, миграции и сборки заглушками)
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.db.base import SessionLocal  # noqa: E402
from app.db.models import AuditEvent, Environment, Project, Release  # noqa: E402
from app.services import audit  # noqa: E402
import app.main as main  # noqa: E402

random.seed(11)
ADMIN = {"Authorization": "Bearer ui-test-admin-token-0123456789abcdef"}
c = TestClient(main.app)
for slug, repo in (("shop", "acme/shop"), ("api-gateway", "acme/api-gateway"), ("mobile-api", "acme/mobile-api"), ("billing", "acme/billing"), ("docs-site", "acme/docs-site")):
    c.post("/api/projects", headers=ADMIN, json={"slug": slug, "repo_full_name": repo, "environments": [
        {"name": "prod", "namespace": slug, "deployment_name": slug, "container_name": slug, "branch": "main"},
        {"name": "staging", "namespace": slug + "-stg", "deployment_name": slug, "container_name": slug, "branch": "develop"}]})
db = SessionLocal()
now = datetime.now(timezone.utc)
envs = db.query(Environment).all()
for d in range(30):
    day = now - timedelta(days=29 - d)
    for env in envs:
        for _ in range(random.choice([0, 1, 1, 2, 3]) if day.weekday() < 5 else random.choice([0, 0, 1])):
            ts = day.replace(hour=random.randint(8, 19), minute=random.randint(0, 59))
            st = random.choices(["deployed", "failed", "building"], [90, 8, 2])[0] if d < 29 else "deployed"
            db.add(Release(environment_id=env.id, git_revision=os.urandom(4).hex(), status=st, triggered_by=random.choice(["ci-bot", "maria", "daniel"]), image_digest="reg/x@sha256:" + os.urandom(8).hex(),
                           error_message="rollout_failed: CrashLoopBackOff, restarts 4" if st == "failed" else None, created_at=ts, deployed_at=ts + timedelta(minutes=random.randint(2, 6)) if st == "deployed" else None))
db.commit()
proj = {p.slug: p for p in db.query(Project).all()}
acts = [("deploy", 60), ("build_finished", 20), ("secret_write", 8), ("domain_set", 3), ("access_denied", 4), ("deploy_failed", 3), ("connection_check", 2)]
names = [a for a, w in acts for _ in range(w)]
for i in range(260):
    a = random.choice(names); p = proj[random.choice(list(proj))]
    audit.log_event(db, random.choice(["ci-bot", "ci-bot", "maria", "maria", "daniel", "olga"]), a, p.id, None, {"revision": os.urandom(3).hex(), "reason": "rollout_failed" if a == "deploy_failed" else None})
rows = db.query(AuditEvent).order_by(AuditEvent.seq).all()
for r in rows:
    r.created_at = now - timedelta(minutes=random.randint(0, 60 * 24 * 14))
db.commit(); db.close()
c.post("/__test/limits", json={"enforce": True, "tier": "pro"})
tok = c.post("/api/tokens", headers=ADMIN, json={"name": "maria", "role": "admin"}).json()["token"]
open("/tmp/demo-token", "w").write(tok)
uvicorn.run(main.app, host="127.0.0.1", port=18098, log_level="warning")
