from app.db.base import SessionLocal
from app.services import plugin_state as ps


def test_put_get_merge_list_delete():
    db = SessionLocal()
    assert ps.get(db, "x.a") is None
    ps.put(db, "x.a", {"n": 1, "keep": "yes"})
    ps.put(db, "x.b", {"n": 2})
    ps.put(db, "y.c", {"n": 3})
    assert ps.merge(db, "x.a", {"n": 5}) == {"n": 5, "keep": "yes"}
    assert ps.merge(db, "nope", {"n": 1}) is None
    assert sorted(ps.list_prefix(db, "x.")) == ["x.a", "x.b"]
    assert ps.delete(db, "x.b") and not ps.delete(db, "x.b")
    db.close()


def test_me_reports_licensed_features(client, admin):
    assert client.get("/api/me", headers=admin).json()["features"] == []
    assert "integrations:manage" in client.get("/api/me", headers=admin).json()["permissions"]
