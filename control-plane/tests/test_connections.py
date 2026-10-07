"""Проверка подключений к внешним сервисам: локальные TLS/Redis/Postgres-подобные серверы, ошибки и защита от SSRF."""
import datetime as dt
import ipaddress
import socket
import ssl
import tempfile
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.config import settings
from app.services import connections

NOW = dt.datetime.now(dt.timezone.utc)


def _key():
    return ec.generate_private_key(ec.SECP256R1())


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


@pytest.fixture(scope="module")
def ca():
    k = _key()
    cert = (x509.CertificateBuilder().subject_name(_name("Test CA")).issuer_name(_name("Test CA")).public_key(k.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(NOW - dt.timedelta(days=1)).not_valid_after(NOW + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(k, hashes.SHA256()))
    return k, cert


def _leaf(ca, names, start=-1, end=365, ips=("127.0.0.1",)):
    ck, cc = ca
    k = _key()
    san = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder().subject_name(_name(names[0])).issuer_name(cc.subject).public_key(k.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(NOW + dt.timedelta(days=start)).not_valid_after(NOW + dt.timedelta(days=end))
            .add_extension(x509.SubjectAlternativeName(san), False).add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .sign(ck, hashes.SHA256()))
    return k, cert


def _pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM).decode()


class Server:
    """Одноразовый сервер в потоке: handler(conn) вызывается на каждое соединение; TLS по необходимости."""

    def __init__(self, handler, leaf=None, plain_first=None):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.ctx = None
        if leaf:
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            with tempfile.NamedTemporaryFile("w", suffix=".pem") as c, tempfile.NamedTemporaryFile("wb", suffix=".key") as k:
                c.write(_pem(leaf[1])); c.flush()
                k.write(leaf[0].private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())); k.flush()
                self.ctx.load_cert_chain(c.name, k.name)
        self.handler, self.plain_first, self.alive = handler, plain_first, True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.alive:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()     # проверка открывает несколько соединений подряд

    def _serve(self, conn):
        try:
            conn.settimeout(3)
            if self.plain_first:
                self.plain_first(conn)
            if self.ctx:
                conn = self.ctx.wrap_socket(conn, server_side=True)
            self.handler(conn)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def close(self):
        self.alive = False
        self.sock.close()


def _http_ok(conn):
    conn.recv(2048)
    conn.sendall(b"HTTP/1.1 200 OK\r\nServer: test\r\nContent-Length: 0\r\n\r\n")


@pytest.fixture(autouse=True)
def loopback(monkeypatch):
    monkeypatch.setattr(settings, "connection_check_allow_loopback", True)


def _steps(r):
    return {s["step"]: s for s in r["steps"]}


def test_validate_spec_rejects_bad_input():
    for bad in ({}, {"kind": "ftp", "host": "x"}, {"kind": "tcp", "host": "bad host!"}, {"kind": "tcp", "host": "a", "port": 70000},
                {"kind": "tcp", "host": "a", "tls": "maybe"}, {"kind": "tcp", "host": "a", "timeout": 99}, {"kind": "tcp", "host": "a", "ca_pem": "nope"}):
        with pytest.raises(connections.SpecError):
            connections.validate_spec(bad)
    ok = connections.validate_spec({"kind": "postgres", "host": "db.example.com"})
    assert ok["port"] == 5432 and ok["tls"] == "off"
    assert connections.validate_spec({"kind": "s3", "host": "s3.example.com"})["tls"] == "require"


def test_loopback_and_link_local_are_forbidden_by_default(monkeypatch):
    monkeypatch.setattr(settings, "connection_check_allow_loopback", False)
    for host in ("127.0.0.1", "169.254.169.254", "0.0.0.0"):
        r = connections.check({"kind": "tcp", "host": host, "port": 80})
        assert not r["ok"] and r["code"] == "address_forbidden", host
    # link-local запрещён всегда, даже когда loopback разрешён
    monkeypatch.setattr(settings, "connection_check_allow_loopback", True)
    assert connections.check({"kind": "tcp", "host": "169.254.169.254", "port": 80})["code"] == "address_forbidden"


def test_closed_port_is_reported_as_refused():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    r = connections.check({"kind": "tcp", "host": "127.0.0.1", "port": port})
    assert not r["ok"] and r["failed_step"] == "tcp" and r["code"] == "refused"


def test_dns_failure():
    r = connections.check({"kind": "tcp", "host": "no-such-host.invalid", "port": 443})
    assert r["failed_step"] == "dns" and r["code"] == "dns_failed"


def test_tls_ok_with_custom_ca_and_cert_details(ca):
    srv = Server(_http_ok, _leaf(ca, ["db.test"]))
    try:
        r = connections.check({"kind": "https", "host": "127.0.0.1", "port": srv.port, "tls": "verify-full", "ca_pem": _pem(ca[1])})
        assert r["ok"], r
        assert r["cert"]["dns_names"] == ["db.test"] and r["cert"]["ips"] == ["127.0.0.1"] and r["cert"]["days_left"] >= 364
        assert _steps(r)["http"]["status"] == 200 and r["warnings"] is None
    finally:
        srv.close()


def test_verify_fails_without_the_ca_and_require_only_warns(ca):
    srv = Server(_http_ok, _leaf(ca, ["db.test"]))
    try:
        r = connections.check({"kind": "https", "host": "127.0.0.1", "port": srv.port, "tls": "verify-full"})
        assert not r["ok"] and r["code"] == "cert_untrusted" and r["cert"]["issuer"] == "Test CA"
        r = connections.check({"kind": "https", "host": "127.0.0.1", "port": srv.port, "tls": "require"})
        assert r["ok"] and "cert_untrusted" in r["warnings"]            # шифрование есть, но подлинность не проверена
    finally:
        srv.close()


def test_expired_certificate(ca):
    srv = Server(_http_ok, _leaf(ca, ["db.test"], start=-400, end=-35))
    try:
        for mode in ("require", "verify-full"):
            r = connections.check({"kind": "https", "host": "127.0.0.1", "port": srv.port, "tls": mode, "ca_pem": _pem(ca[1])})
            assert not r["ok"] and r["code"] == "cert_expired" and r["cert"]["expired"] and r["cert"]["days_left"] <= -35, mode
    finally:
        srv.close()


def test_hostname_mismatch_lists_names_in_the_certificate(ca):
    srv = Server(_http_ok, _leaf(ca, ["other.example.com"], ips=()))
    try:
        r = connections.check({"kind": "https", "host": "localhost", "port": srv.port, "tls": "verify-full", "ca_pem": _pem(ca[1])})
        assert not r["ok"] and r["code"] == "cert_hostname_mismatch"
        tls = _steps(r)["tls"]
        assert tls["names"] == ["other.example.com"]
    finally:
        srv.close()


def test_certificate_expiring_soon_is_a_warning(ca):
    srv = Server(_http_ok, _leaf(ca, ["db.test"], start=-300, end=9))
    try:
        r = connections.check({"kind": "https", "host": "127.0.0.1", "port": srv.port, "tls": "verify-full", "ca_pem": _pem(ca[1])})
        assert r["ok"] and "cert_expires_soon" in r["warnings"] and 8 <= r["cert"]["days_left"] <= 9
    finally:
        srv.close()


def test_plain_server_when_tls_is_required():
    srv = Server(lambda c: c.sendall(b"-ERR plain\r\n"))
    try:
        r = connections.check({"kind": "redis", "host": "127.0.0.1", "port": srv.port, "tls": "require"})
        assert not r["ok"] and r["failed_step"] == "tls" and r["code"] in ("tls_not_spoken", "tls_failed")
    finally:
        srv.close()


def _redis(password):
    def handler(conn):
        while True:
            data = conn.recv(512)
            if not data:
                return
            if b"AUTH" in data:
                conn.sendall(b"+OK\r\n" if password.encode() in data else b"-WRONGPASS invalid\r\n")
            elif b"PING" in data:
                conn.sendall(b"+PONG\r\n")
    return handler


def test_redis_auth_success_and_failure():
    srv = Server(_redis("s3cret-value"))
    try:
        base = {"kind": "redis", "host": "127.0.0.1", "port": srv.port, "tls": "off"}
        assert connections.check({**base, "password": "s3cret-value"})["ok"]
        r = connections.check({**base, "password": "wrong-value"})
        assert not r["ok"] and r["failed_step"] == "auth" and r["code"] == "auth_failed"
    finally:
        srv.close()


def test_redis_requires_password_when_not_given():
    def noauth(conn):
        conn.recv(512)
        conn.sendall(b"-NOAUTH Authentication required.\r\n")
    srv = Server(noauth)
    try:
        r = connections.check({"kind": "redis", "host": "127.0.0.1", "port": srv.port, "tls": "off"})
        assert not r["ok"] and r["code"] == "auth_required"
    finally:
        srv.close()


def test_postgres_without_tls_support_is_explained():
    def no_ssl(conn):
        conn.sendall(b"N")
    srv = Server(no_ssl)
    try:
        r = connections.check({"kind": "postgres", "host": "127.0.0.1", "port": srv.port, "tls": "require"})
        assert not r["ok"] and r["failed_step"] == "tls" and r["code"] == "tls_unsupported"
        r = connections.check({"kind": "postgres", "host": "127.0.0.1", "port": srv.port, "tls": "off"})
        assert r["ok"]                                              # без TLS проверяется только достижимость
    finally:
        srv.close()


def test_postgres_starttls_then_tls_handshake(ca):
    def accept_ssl(conn):                                           # Postgres: 8 байт SSLRequest -> «S» -> TLS
        data = conn.recv(8)
        conn.sendall(b"S" if len(data) == 8 else b"N")
    srv = Server(lambda c: None, _leaf(ca, ["db.test"]), plain_first=accept_ssl)
    try:
        r = connections.check({"kind": "postgres", "host": "127.0.0.1", "port": srv.port, "tls": "verify-full", "ca_pem": _pem(ca[1])})
        assert r["ok"] and _steps(r)["tls"]["verified"] is True
    finally:
        srv.close()


def test_secrets_never_appear_in_results(ca):
    srv = Server(_redis("topsecretpassword"))
    try:
        r = connections.check({"kind": "redis", "host": "127.0.0.1", "port": srv.port, "tls": "off", "password": "topsecretpassword", "user": "svc"})
        assert "topsecretpassword" not in repr(r)
    finally:
        srv.close()


# ---------- API ----------

BODY = {"slug": "shop", "repo_full_name": "acme/shop", "environments": [
    {"name": "prod", "namespace": "shop", "deployment_name": "shop", "container_name": "shop"}]}


def test_probe_api_permissions_validation_and_audit(client, admin, make_token):
    from app.db.base import SessionLocal
    from app.db.models import AuditEvent
    client.post("/api/projects", json=BODY, headers=admin)
    srv = Server(_redis("api-secret-pass"))
    try:
        payload = {"targets": [{"kind": "redis", "host": "127.0.0.1", "port": srv.port, "tls": "off", "password": "api-secret-pass"}]}
        assert client.post("/api/projects/shop/connections/probe", json=payload, headers=make_token("viewer")).status_code == 403
        r = client.post("/api/projects/shop/connections/probe", json=payload, headers=admin)
        assert r.status_code == 200 and r.json()["results"][0]["ok"] is True
        assert "api-secret-pass" not in r.text
        db = SessionLocal()
        ev = db.query(AuditEvent).filter_by(action="connection_check").one()
        assert "api-secret-pass" not in repr(ev.detail) and ev.detail["targets"][0]["host"] == "127.0.0.1" and ev.detail["targets"][0]["ok"] is True
        db.close()
        assert client.post("/api/projects/shop/connections/probe", json={"targets": []}, headers=admin).status_code == 422
        assert client.post("/api/projects/shop/connections/probe", json={"targets": [{"kind": "tcp", "host": "x"}] * 11}, headers=admin).status_code == 422
        assert client.post("/api/projects/shop/connections/probe", json={"targets": [{"kind": "nope", "host": "x"}]}, headers=admin).status_code == 422
        assert client.post("/api/projects/nope/connections/probe", json=payload, headers=admin).status_code == 404
    finally:
        srv.close()


def test_probe_api_rate_limit(client, admin, monkeypatch):
    from app.routers import connections as router
    client.post("/api/projects", json=BODY, headers=admin)
    monkeypatch.setattr(settings, "connection_check_per_minute", 2)
    router._hits.clear()
    t = {"targets": [{"kind": "tcp", "host": "no-such-host.invalid", "port": 1}]}
    assert client.post("/api/projects/shop/connections/probe", json=t, headers=admin).status_code == 200
    assert client.post("/api/projects/shop/connections/probe", json=t, headers=admin).status_code == 200
    assert client.post("/api/projects/shop/connections/probe", json=t, headers=admin).status_code == 429
    router._hits.clear()
