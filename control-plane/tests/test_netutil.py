import pytest
from starlette.requests import Request

from app import netutil
from app.config import settings


def req(peer, headers=None, scheme="http"):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "client": (peer, 1234), "headers": raw, "scheme": scheme, "method": "GET", "path": "/", "query_string": b"", "server": ("x", 80)})


@pytest.fixture
def proxies(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "10.42.0.0/16, 127.0.0.1")


def test_headers_are_ignored_without_trusted_proxies():
    assert netutil.client_ip(req("203.0.113.9", {"X-Forwarded-For": "1.2.3.4"})) == "203.0.113.9"
    assert netutil.is_https(req("203.0.113.9", {"X-Forwarded-Proto": "https"})) is False


def test_untrusted_peer_cannot_spoof(proxies):
    assert netutil.client_ip(req("203.0.113.9", {"X-Forwarded-For": "1.2.3.4"})) == "203.0.113.9"
    assert netutil.is_https(req("203.0.113.9", {"X-Forwarded-Proto": "https"})) is False


def test_client_is_first_untrusted_hop_from_the_right(proxies):
    assert netutil.client_ip(req("10.42.0.5", {"X-Forwarded-For": "198.51.100.7"})) == "198.51.100.7"
    # клиент подставил свой адрес слева — берём тот, что дописал наш прокси
    assert netutil.client_ip(req("10.42.0.5", {"X-Forwarded-For": "6.6.6.6, 198.51.100.7, 10.42.1.1"})) == "198.51.100.7"
    assert netutil.client_ip(req("10.42.0.5")) == "10.42.0.5"
    assert netutil.client_ip(req("10.42.0.5", {"X-Forwarded-For": "10.42.9.9"})) == "10.42.0.5"


def test_garbage_in_header_is_not_trusted(proxies):
    assert netutil.client_ip(req("10.42.0.5", {"X-Forwarded-For": "1.2.3.4, not-an-ip"})) == "10.42.0.5"


def test_ipv6_and_https_from_trusted_proxy(proxies):
    assert netutil.client_ip(req("10.42.0.5", {"X-Forwarded-For": "2001:db8::1"})) == "2001:db8::1"
    assert netutil.is_https(req("10.42.0.5", {"X-Forwarded-Proto": "https"})) is True
    assert netutil.is_https(req("10.42.0.5", {"X-Forwarded-Proto": "http"})) is False
    assert netutil.is_https(req("1.1.1.1", scheme="https")) is True


@pytest.fixture
def behind_proxy(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "trusted_proxies", "10.42.0.0/16")

    async def as_ingress(scope, receive, send):                  # так приложение видит запрос, пришедший от ingress
        if scope["type"] == "http":
            scope = {**scope, "client": ("10.42.0.5", 5000)}
        await app(scope, receive, send)
    return TestClient(as_ingress)


def test_login_lockout_uses_real_client_ip_behind_proxy(behind_proxy, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failures", 1)      # порог по IP = 5 * порог по имени
    a = {"X-Forwarded-For": "198.51.100.1"}
    for i in range(6):
        behind_proxy.post("/api/auth/login", json={"username": f"u{i}", "password": "x"}, headers=a)
    assert behind_proxy.post("/api/auth/login", json={"username": "fresh", "password": "x"}, headers=a).status_code == 429
    assert behind_proxy.post("/api/auth/login", json={"username": "fresh", "password": "x"}, headers={"X-Forwarded-For": "198.51.100.2"}).status_code == 401


def test_hsts_only_over_https(behind_proxy):
    assert "strict-transport-security" not in behind_proxy.get("/health").headers
    assert behind_proxy.get("/health", headers={"X-Forwarded-Proto": "https"}).headers["strict-transport-security"].startswith("max-age=")


def test_login_cookie_is_secure_when_proxy_says_https(behind_proxy, admin):
    behind_proxy.post("/api/users", json={"username": "sec-user", "role": "admin", "password": "Sup3r-secret-pass!"}, headers=admin)
    h = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.3"}
    r = behind_proxy.post("/api/auth/login", json={"username": "sec-user", "password": "Sup3r-secret-pass!"}, headers=h)
    assert r.status_code == 200, r.text
    assert "secure" in r.headers["set-cookie"].lower() and "httponly" in r.headers["set-cookie"].lower()
    r2 = behind_proxy.post("/api/auth/login", json={"username": "sec-user", "password": "Sup3r-secret-pass!"}, headers={"X-Forwarded-For": "198.51.100.3"})
    assert "secure" not in r2.headers["set-cookie"].lower()
