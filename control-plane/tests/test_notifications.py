import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.db.base import SessionLocal
from app.db.models import Secret
from app.services import checks

BODY = {"slug": "shop", "repo_full_name": "acme/shop", "environments": [
    {"name": "prod", "namespace": "shop", "deployment_name": "shop", "container_name": "shop", "require_approval": True}]}
HOOK = "https://hooks.example.com/dsp"
TG = {"kind": "telegram", "bot_token": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "chat_id": "-100123"}


def mk(client, admin, **kw):
    body = {"name": "ops", "kind": "webhook", "url": HOOK, **kw}
    return client.post("/api/notifications/channels", json=body, headers=admin)


def test_webhook_signed_delivery_for_selected_events(client, admin, sent):
    r = mk(client, admin, events=["deploy_failed", "break_glass"])
    assert r.status_code == 201
    secret = r.json()["signing_secret"]
    client.post("/api/projects", json=BODY, headers=admin)          # project_create — не в списке событий
    assert sent == []
    from app.services import audit
    db = SessionLocal()
    audit.log_event(db, "webhook", "deploy_failed", None, None, {"reason": "build_timeout", "slug": "shop"})
    db.close()
    assert len(sent) == 1
    url, body, headers = sent[0]
    assert url == HOOK and json.loads(body)["event"] == "deploy_failed" and json.loads(body)["detail"]["reason"] == "build_timeout"
    assert headers["X-DSP-Signature"] == "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_secret_material_never_returned(client, admin, sent):
    mk(client, admin, name="a")
    client.post("/api/notifications/channels", json={"name": "t", **TG}, headers=admin)
    listing = client.get("/api/notifications/channels", headers=admin).text
    assert "AAAAAAAA" not in listing and "hooks.example.com/dsp" not in listing and "signing_secret" not in listing
    assert "hooks.example.com" in listing            # безопасная подпись адресата — только хост


def test_approval_flow_notifies_and_project_scope(client, admin, sent, make_token):
    client.post("/api/projects", json=BODY, headers=admin)
    other = dict(BODY, slug="other", repo_full_name="acme/other")
    client.post("/api/projects", json=other, headers=admin)
    mk(client, admin, name="only-shop", project="shop", events=["approval_requested"])
    mk(client, admin, name="all", url="https://hooks.example.com/all", events=["approval_requested"])
    dev = make_token("devops")
    sent.clear()
    client.post("/api/projects/shop/redeploy?environment=prod", headers=dev)
    urls = sorted(u for u, _, _ in sent)
    assert urls == sorted([HOOK, "https://hooks.example.com/all"])
    sent.clear()
    client.post("/api/projects/other/redeploy?environment=prod", headers=dev)
    assert [u for u, _, _ in sent] == ["https://hooks.example.com/all"]   # канал проекта shop чужие события не получает


def test_slack_and_telegram_formats(client, admin, sent):
    mk(client, admin, name="s", kind="slack", url="https://hooks.slack.com/services/T/B/X", events=["*"])
    client.post("/api/notifications/channels", json={"name": "t", "events": ["*"], **TG}, headers=admin)
    sent.clear()
    from app.services import audit
    db = SessionLocal(); audit.log_event(db, "alice", "rollback", None, None, {"k": "v"}); db.close()
    by = {u: json.loads(b) for u, b, _ in sent}
    assert "text" in by["https://hooks.slack.com/services/T/B/X"] and "rollback" not in by["https://hooks.slack.com/services/T/B/X"].get("event", "")
    tg = by[f"https://api.telegram.org/bot{TG['bot_token']}/sendMessage"]
    assert tg["chat_id"] == "-100123" and "alice" in tg["text"]


def test_ssrf_and_validation(client, admin, monkeypatch):
    from app.services import notifications
    assert mk(client, admin, url="http://hooks.example.com/x").status_code == 422           # http запрещён
    monkeypatch.setattr(notifications, "_resolve", lambda h: ["10.0.0.5"])
    assert mk(client, admin, url="https://internal.example.com/x").status_code == 422        # частная сеть
    assert mk(client, admin, url="https://127.0.0.1/x").status_code == 422
    assert mk(client, admin, url="https://169.254.169.254/latest").status_code == 422        # metadata
    assert mk(client, admin, url="https://user:pw@hooks.example.com/x").status_code == 422
    monkeypatch.setattr(notifications, "_resolve", lambda h: ["93.184.216.34"])
    assert mk(client, admin, events=["nope"]).status_code == 422
    assert mk(client, admin, kind="carrier-pigeon").status_code == 422
    assert client.post("/api/notifications/channels", json={"name": "t", "kind": "telegram", "bot_token": "x", "chat_id": "1"},
                       headers=admin).status_code == 422
    settings.notify_allow_private_targets = True
    try:
        monkeypatch.setattr(notifications, "_resolve", lambda h: ["10.0.0.5"])
        assert mk(client, admin, name="internal", url="https://internal.example.com/x").status_code == 201
    finally:
        settings.notify_allow_private_targets = False


def test_failure_is_recorded_and_never_breaks_the_action(client, admin, monkeypatch, sent):
    from app.services import notifications
    mk(client, admin, events=["project_create"])
    monkeypatch.setattr(notifications, "_post", lambda *a: 500)
    assert client.post("/api/projects", json=BODY, headers=admin).status_code == 201       # действие выполнено
    ch = client.get("/api/notifications/channels", headers=admin).json()[0]
    assert ch["last_status"] == "error" and "HTTP 500" in ch["last_error"]
    cid = ch["id"]
    assert client.post(f"/api/notifications/channels/{cid}/test", headers=admin).json()["ok"] is False
    monkeypatch.setattr(notifications, "_post", lambda *a: 204)
    assert client.post(f"/api/notifications/channels/{cid}/test", headers=admin).json()["ok"] is True
    assert client.get("/api/notifications/channels", headers=admin).json()[0]["last_status"] == "ok"


def test_update_disable_delete(client, admin, sent):
    cid = mk(client, admin, events=["*"]).json()["id"]
    assert client.patch(f"/api/notifications/channels/{cid}", json={"enabled": False}, headers=admin).json()["enabled"] is False
    sent.clear()
    client.post("/api/projects", json=BODY, headers=admin)
    assert sent == []
    assert client.delete(f"/api/notifications/channels/{cid}", headers=admin).status_code == 200
    assert client.delete(f"/api/notifications/channels/{cid}", headers=admin).status_code == 404
    assert client.get("/api/notifications/channels/not-a-uuid", headers=admin).status_code in (404, 405)


def test_admin_only(client, admin, make_token):
    for role in ("devops", "developer", "viewer"):
        h = make_token(role)
        assert client.get("/api/notifications/channels", headers=h).status_code == 403
        assert client.post("/api/notifications/channels", json={"name": "x", "kind": "webhook", "url": HOOK}, headers=h).status_code == 403


def test_periodic_checks_raise_secret_and_chain_events_once(client, admin, sent):
    client.post("/api/projects", json=BODY, headers=admin)
    client.put("/api/projects/shop/secrets/DB", json={"value": "x"}, headers=admin)
    db = SessionLocal()
    row = db.query(Secret).one(); row.expires_at = datetime.now(timezone.utc) + timedelta(days=2); db.commit(); db.close()
    mk(client, admin, events=["secret_expiring", "secret_expired", "audit_chain_broken"])
    sent.clear()
    assert client.post("/api/notifications/check", headers=admin).json()["raised"] == 1
    assert len(sent) == 1 and json.loads(sent[0][1])["event"] == "secret_expiring"
    assert client.post("/api/notifications/check", headers=admin).json()["raised"] == 0        # без повторов
    db = SessionLocal()
    from app.db.models import AuditEvent
    ev = db.query(AuditEvent).filter(AuditEvent.seq == 2).one(); ev.actor = "tampered"; db.commit(); db.close()
    sent.clear()
    r = client.post("/api/notifications/check", headers=admin).json()
    assert r["chain_ok"] is False and r["raised"] == 1
    assert any(json.loads(b)["event"] == "audit_chain_broken" for _, b, _ in sent)
    assert client.post("/api/notifications/check", headers=admin).json()["raised"] == 0


def test_project_delete_removes_scoped_channels(client, admin, sent):
    client.post("/api/projects", json=BODY, headers=admin)
    mk(client, admin, project="shop")
    assert client.delete("/api/projects/shop", headers=admin).status_code == 200
    assert client.get("/api/notifications/channels", headers=admin).json() == []
