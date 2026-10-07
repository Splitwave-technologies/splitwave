"""Проверка обновлений: читает публичный файл раз в сутки, молчит при ошибках, выключается настройкой."""
import io
import json

import pytest

from app.config import settings
from app.services import updates
from app.version import VERSION


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    updates.reset()
    monkeypatch.setattr(settings, "update_check", True)
    monkeypatch.setattr(settings, "update_url", "https://updates.example.test/releases.json")
    yield
    updates.reset()


def _serve(monkeypatch, payload, calls=None):
    def fake(req, timeout=0):
        if calls is not None:
            calls.append((req.full_url, dict(req.header_items()), timeout))
        if isinstance(payload, Exception):
            raise payload
        return _Resp(json.dumps(payload).encode() if not isinstance(payload, bytes) else payload)
    monkeypatch.setattr(updates.urllib.request, "urlopen", fake)


def test_version_comparison():
    assert updates.is_newer("0.2.0", "0.1.0") and updates.is_newer("1.0.0", "0.9.9") and updates.is_newer("0.1.10", "0.1.9")
    assert not updates.is_newer("0.1.0", "0.1.0") and not updates.is_newer("0.0.9", "0.1.0") and not updates.is_newer("garbage", "0.1.0")


def test_a_newer_release_is_reported_with_its_link_and_security_flag(monkeypatch):
    _serve(monkeypatch, {"version": "9.9.9", "url": "https://split-wave.com/changelog", "security": True})
    s = updates.status()
    assert s == {"enabled": True, "current": VERSION, "latest": "9.9.9", "available": True, "security": True, "url": "https://split-wave.com/changelog"}


def test_the_same_or_older_release_is_not_an_update(monkeypatch):
    _serve(monkeypatch, {"version": VERSION})
    assert updates.status()["available"] is False


def test_errors_are_silent(monkeypatch):
    for bad in (OSError("no network"), b"not json", json.dumps({"nope": 1}).encode(), json.dumps({"version": "x"}).encode()):
        updates.reset()
        _serve(monkeypatch, bad)
        s = updates.status()
        assert s["available"] is False and s["latest"] is None


def test_one_request_per_day_and_nothing_identifying_is_sent(monkeypatch):
    calls = []
    _serve(monkeypatch, {"version": "9.9.9"}, calls)
    updates.status(now=1_000_000.0); updates.status(now=1_000_000.0 + 3600)
    assert len(calls) == 1
    updates.status(now=1_000_000.0 + 25 * 3600)
    assert len(calls) == 2
    url, headers, timeout = calls[0]
    assert url == "https://updates.example.test/releases.json" and timeout <= 5
    assert set(k.lower() for k in headers) <= {"accept", "user-agent"}


def test_disabled_makes_no_request(monkeypatch):
    calls = []
    _serve(monkeypatch, {"version": "9.9.9"}, calls)
    monkeypatch.setattr(settings, "update_check", False)
    s = updates.status()
    assert s["enabled"] is False and s["available"] is False and calls == []


def test_plain_http_is_refused(monkeypatch):
    calls = []
    _serve(monkeypatch, {"version": "9.9.9"}, calls)
    monkeypatch.setattr(settings, "update_url", "http://updates.example.test/releases.json")
    assert updates.status()["available"] is False and calls == []


def test_endpoint_is_for_administrators_only(client, admin, make_token, monkeypatch):
    _serve(monkeypatch, {"version": "9.9.9"})
    r = client.get("/api/updates", headers=admin)
    assert r.status_code == 200 and r.json()["available"] is True
    assert client.get("/api/updates", headers=make_token("viewer")).status_code == 403
    assert client.get("/api/updates").status_code == 401
