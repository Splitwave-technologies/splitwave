from app.db.base import SessionLocal
from app.services import audit


def _seed():
    db = SessionLocal()
    for actor, action in (("exp-ci", "deploy"), ("exp-ci", "deploy_failed"), ("exp-bob", "secret_write"), ("exp-bob", "access_denied")):
        audit.log_event(db, actor, action, detail={"k": "needle"})
    db.close()


def test_query_filters_and_result(client, admin):
    _seed()
    r = client.get("/api/explorer/events", params={"q": "actor:exp-ci", "range": "1h"}, headers=admin).json()
    assert r["total"] == 2 and {e["action"] for e in r["events"]} == {"deploy", "deploy_failed"}
    r = client.get("/api/explorer/events", params={"q": "actor:exp-* result:failed", "range": "1h"}, headers=admin).json()
    assert {e["action"] for e in r["events"]} == {"deploy_failed", "access_denied"}
    r = client.get("/api/explorer/events", params={"q": "actor:exp-* -action:deploy*", "range": "1h"}, headers=admin).json()
    assert r["total"] == 2
    r = client.get("/api/explorer/events", params={"q": "needle actor:exp-bob", "range": "1h"}, headers=admin).json()
    assert r["total"] == 2 and sum(b["n"] for b in r["histogram"]["buckets"]) == 2
    assert r["fields"]["actor"][0] == {"value": "exp-bob", "count": 2}


def test_bad_queries_rejected(client, admin):
    for q in ("nofield:x", "a OR b", "action:", 'actor:"unterminated', "result:maybe"):
        assert client.get("/api/explorer/events", params={"q": q}, headers=admin).status_code == 422, q
    assert client.get("/api/explorer/events", params={"range": "5y"}, headers=admin).status_code == 422


def test_requires_audit_permission(client):
    assert client.get("/api/explorer/events").status_code == 401
