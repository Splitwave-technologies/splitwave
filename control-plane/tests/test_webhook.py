import hashlib
import hmac
import json

from app.routers import webhooks

BODY = {"ref": "refs/heads/main", "after": "abc", "repository": {"full_name": "acme/demo", "clone_url": "https://github.com/acme/demo.git"}}


def _sign(raw: bytes, secret="whsec"):
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _post(client, raw, sig, event="push"):
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if sig:
        headers["X-Hub-Signature-256"] = sig
    return client.post("/webhook/github", content=raw, headers=headers)


def test_rejects_missing_and_wrong_signature(client, project):
    raw = json.dumps(BODY).encode()
    assert _post(client, raw, None).status_code == 401
    assert _post(client, raw, _sign(raw, "wrong")).status_code == 401


def test_accepts_signed_push_and_starts_build(client, project, monkeypatch):
    started = []
    monkeypatch.setattr(webhooks, "run_build_and_deploy", lambda *a, **k: started.append(a))
    raw = json.dumps(BODY).encode()
    r = _post(client, raw, _sign(raw))
    assert r.status_code == 200 and r.json()["accepted"] is True
    assert len(started) == 1 and started[0][3] == "abc"


def test_ignores_other_events_and_untracked_branches(client, project):
    raw = json.dumps(BODY).encode()
    assert _post(client, raw, _sign(raw), event="ping").json()["skipped"] is True
    other = json.dumps({**BODY, "ref": "refs/heads/feature"}).encode()
    assert _post(client, other, _sign(other)).json()["skipped"] is True
