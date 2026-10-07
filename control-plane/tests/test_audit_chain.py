from app.db.base import SessionLocal
from app.db.models import AuditEvent
from app.services import audit


def _events(db):
    return db.query(AuditEvent).filter(AuditEvent.seq.isnot(None)).order_by(AuditEvent.seq).all()


def test_chain_is_valid_and_grows(client, admin):
    db = SessionLocal()
    for i in range(5):
        audit.log_event(db, "tester", "demo", detail={"i": i, "nested": {"b": 1, "a": [1, 2]}})
    r = client.get("/api/audit/verify", headers=admin).json()
    assert r["ok"] and r["events"] >= 5 and r["first_problem"] is None
    evs = _events(db)
    assert [e.seq for e in evs] == list(range(1, len(evs) + 1))
    assert evs[0].prev_hash == audit.GENESIS and evs[1].prev_hash == evs[0].hash
    assert r["head"] == evs[-1].hash and r["head_seq"] == evs[-1].seq
    db.close()


def test_modification_is_detected(client, admin):
    db = SessionLocal()
    for i in range(4):
        audit.log_event(db, "tester", "demo", detail={"i": i})
    victim = _events(db)[2]
    victim.detail = {"i": 999}
    db.commit()
    r = client.get("/api/audit/verify", headers=admin).json()
    assert not r["ok"] and r["first_problem"] == {"seq": victim.seq, "reason": "content_modified"}
    db.close()


def test_actor_and_time_changes_are_detected(client, admin):
    db = SessionLocal()
    for i in range(3):
        audit.log_event(db, "tester", "demo")
    ev = _events(db)[1]
    ev.actor = "somebody-else"
    db.commit()
    assert client.get("/api/audit/verify", headers=admin).json()["first_problem"]["reason"] == "content_modified"
    db.close()


def test_deletion_is_detected(client, admin):
    db = SessionLocal()
    for i in range(4):
        audit.log_event(db, "tester", "demo", detail={"i": i})
    victim = _events(db)[1]
    seq = victim.seq
    db.delete(victim)
    db.commit()
    r = client.get("/api/audit/verify", headers=admin).json()
    assert not r["ok"] and r["first_problem"] == {"seq": seq, "reason": "missing_event"}
    db.close()


def test_recompute_without_key_fails(client, admin, monkeypatch):
    """Тот, у кого есть только база, не может пересчитать цепочку: нужен ключ платформы."""
    db = SessionLocal()
    for i in range(3):
        audit.log_event(db, "tester", "demo", detail={"i": i})
    evs = _events(db)
    monkeypatch.setattr(audit, "_key", lambda: b"attacker-guess")
    evs[1].detail = {"i": 42}
    evs[1].hash = audit.compute_hash(evs[1].seq, evs[1].actor, evs[1].action, evs[1].detail, evs[1].created_at, evs[1].prev_hash)
    db.commit()
    monkeypatch.undo()
    assert not client.get("/api/audit/verify", headers=admin).json()["ok"]
    db.close()


def test_project_delete_does_not_break_chain(client, admin):
    body = {"slug": "chainy", "repo_full_name": "o/chainy", "environments": [
        {"name": "prod", "namespace": "ns", "deployment_name": "d", "container_name": "c"}]}
    assert client.post("/api/projects", json=body, headers=admin).status_code == 201
    assert client.delete("/api/projects/chainy", headers=admin).status_code == 200
    r = client.get("/api/audit/verify", headers=admin).json()
    assert r["ok"], r
    ev = [e for e in client.get("/api/audit", headers=admin).json() if e["action"] == "project_create"][0]
    assert ev["context"].get("project_slug") == "chainy"


def test_verify_requires_permission(client, make_token):
    dev = make_token("developer")
    assert client.get("/api/audit/verify", headers=dev).status_code == 403
