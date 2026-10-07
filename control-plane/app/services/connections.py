"""Проверка подключения к внешним сервисам (БД, кэш, объектное хранилище, HTTPS) из платформы.

Шаги: DNS → TCP → TLS (с разбором сертификата: срок, имена, подпись) → вход (Postgres, Redis, S3, HTTPS).
Каждая ошибка получает код и подсказку, что исправить (порт закрыт / блокирует файрвол / сертификат истёк / чужое имя / неизвестный CA).
Защита от SSRF: loopback, link-local, unspecified и multicast адреса запрещены (частные сети разрешены: БД обычно во внутренней сети),
соединение идёт на уже разрешённый IP, а не на имя повторно (защита от подмены DNS между проверкой и подключением).
Учётные данные используются только в памяти и не попадают в ответ и журнал."""
import datetime as dt
import hashlib
import hmac
import ipaddress
import re
import socket
import ssl
import struct
import tempfile
import time
from typing import Optional

from cryptography import x509
from cryptography.x509.oid import NameOID

from app.config import settings

KINDS = {"postgres": 5432, "mysql": 3306, "redis": 6379, "s3": 443, "https": 443, "tcp": None}
TLS_MODES = ("off", "prefer", "require", "verify-ca", "verify-full")
HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|[0-9A-Fa-f:]+)$")
MAX_CA_BYTES = 20_000


class SpecError(ValueError):
    pass


def validate_spec(raw: dict) -> dict:
    """Строгая проверка описания цели; возвращает нормализованное описание (без секретов в логах)."""
    if not isinstance(raw, dict):
        raise SpecError("target must be an object")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise SpecError(f"kind must be one of {sorted(KINDS)}")
    host = str(raw.get("host") or "").strip()
    if not host or len(host) > 253 or not HOST_RE.match(host):
        raise SpecError("host must be a DNS name or an IP address")
    port = raw.get("port") or KINDS[kind]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise SpecError("port must be an integer 1..65535")
    default_tls = "require" if kind in ("s3", "https") else "off"
    tls = raw.get("tls") or default_tls
    if tls not in TLS_MODES:
        raise SpecError(f"tls must be one of {list(TLS_MODES)}")
    timeout = raw.get("timeout", settings.connection_check_timeout_seconds)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 1 <= timeout <= 15:
        raise SpecError("timeout must be 1..15 seconds")
    ca_pem = raw.get("ca_pem")
    if ca_pem:
        if not isinstance(ca_pem, str) or len(ca_pem) > MAX_CA_BYTES or "BEGIN CERTIFICATE" not in ca_pem:
            raise SpecError("ca_pem must be a PEM certificate (up to 20 KB)")
        try:
            ssl.PEM_cert_to_DER_cert(ca_pem.strip().split("-----END CERTIFICATE-----")[0] + "-----END CERTIFICATE-----")
        except Exception:
            raise SpecError("ca_pem is not a valid PEM certificate")
    for key in ("user", "password", "database", "access_key", "secret_key", "region", "path"):
        if raw.get(key) is not None and not isinstance(raw[key], str):
            raise SpecError(f"{key} must be a string")
    return {"kind": kind, "host": host, "port": port, "tls": tls, "timeout": float(timeout), "ca_pem": ca_pem or None,
            "user": raw.get("user"), "password": raw.get("password"), "database": raw.get("database"),
            "access_key": raw.get("access_key"), "secret_key": raw.get("secret_key"), "region": raw.get("region") or "us-east-1",
            "path": raw.get("path") or "/"}


# ---------- шаги ----------

class _Run:
    def __init__(self, spec: dict):
        self.spec, self.steps, self.cert = spec, [], None

    def step(self, name: str, ok: bool, ms: Optional[int] = None, code: Optional[str] = None, **params):
        entry = {"step": name, "ok": ok}
        if ms is not None:
            entry["ms"] = ms
        if code:
            entry["code"] = code
        entry.update({k: v for k, v in params.items() if v is not None})
        self.steps.append(entry)
        return ok


def _blocked(ip: ipaddress._BaseAddress) -> Optional[str]:
    if ip.is_loopback and not settings.connection_check_allow_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved and not ip.is_private:
        return "reserved"
    return None


def _resolve(host: str) -> list:
    try:
        return [ipaddress.ip_address(host.split("%")[0])]
    except ValueError:
        pass
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    addrs = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip not in addrs:
            addrs.append(ip)
    return sorted(addrs, key=lambda a: a.version)       # IPv4 первым: так проще разбирать ответы файрвола


def _san_names(cert: x509.Certificate) -> tuple[list, list]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return [], []
    return san.get_values_for_type(x509.DNSName), [str(i) for i in san.get_values_for_type(x509.IPAddress)]


def _host_matches(host: str, dns_names: list, ips: list, cn: Optional[str]) -> bool:
    try:
        return str(ipaddress.ip_address(host)) in ips
    except ValueError:
        pass
    names = dns_names or ([cn] if cn else [])      # без SAN современные клиенты не принимают CN, но подсказка полезна
    host = host.lower()
    for n in names:
        n = (n or "").lower()
        if n == host or (n.startswith("*.") and host.count(".") >= 1 and host.split(".", 1)[1] == n[2:] and host.split(".", 1)[0] != ""):
            return True
    return False


def describe_cert(der: bytes, host: str) -> dict:
    cert = x509.load_der_x509_certificate(der)
    now = dt.datetime.now(dt.timezone.utc)
    end = cert.not_valid_after_utc
    cn = next(iter(cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)), None)
    icn = next(iter(cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)), None)
    dns_names, ips = _san_names(cert)
    return {"subject": cn.value if cn else None, "issuer": icn.value if icn else None, "self_signed": cert.subject == cert.issuer,
            "dns_names": dns_names, "ips": ips, "not_before": cert.not_valid_before_utc.isoformat(), "not_after": end.isoformat(),
            "days_left": (end - now).days, "expired": end < now, "not_yet_valid": cert.not_valid_before_utc > now,
            "host_match": _host_matches(host, dns_names, ips, cn.value if cn else None),
            "sha256": hashlib.sha256(der).hexdigest()}


def _ctx(mode: str, ca_pem: Optional[str]) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if mode in ("verify-ca", "verify-full"):
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.check_hostname = mode == "verify-full"
        if ca_pem:
            ctx.load_verify_locations(cadata=ca_pem)
        else:
            ctx.load_default_certs()
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _tcp(ip, port: int, timeout: float) -> socket.socket:
    s = socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((str(ip), port))
    return s


def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed by server")
        buf += chunk
    return buf


def _starttls(kind: str, s: socket.socket) -> Optional[str]:
    """Протокольное согласование TLS (Postgres, MySQL). None — можно оборачивать в TLS; иначе код причины."""
    if kind == "postgres":
        s.sendall(struct.pack("!II", 8, 80877103))      # SSLRequest
        answer = _recv_exact(s, 1)
        return None if answer == b"S" else "tls_unsupported"
    if kind == "mysql":
        head = _recv_exact(s, 4)
        payload = _recv_exact(s, head[0] | head[1] << 8 | head[2] << 16)
        if not payload or payload[0] != 10:
            return "not_mysql"
        pos = payload.index(b"\x00", 1) + 1 + 4 + 8 + 1       # версия сервера, id соединения, salt, filler
        caps = struct.unpack("<H", payload[pos:pos + 2])[0]
        if not caps & 0x0800:
            return "tls_unsupported"
        flags = 0x200 | 0x800 | 0x8000 | 0x80000
        s.sendall(struct.pack("<I", 32)[:3] + b"\x01" + struct.pack("<IIB", flags, 0x01000000, 45) + b"\x00" * 23)
        return None
    return None


def _classify_tls_error(e: Exception, cert: Optional[dict]) -> tuple[str, dict]:
    msg = str(e)
    if isinstance(e, ssl.SSLCertVerificationError):
        reason = getattr(e, "verify_message", "") or msg
        if "expired" in reason:
            return "cert_expired", {"days_ago": -cert["days_left"] if cert else None}
        if "not yet valid" in reason:
            return "cert_not_yet_valid", {}
        if "Hostname mismatch" in msg or "hostname" in reason.lower() or "IP address mismatch" in msg:
            return "cert_hostname_mismatch", {"names": (cert["dns_names"] + cert["ips"]) if cert else None}
        return "cert_untrusted", {"detail": reason[:160]}
    if "WRONG_VERSION_NUMBER" in msg or "wrong version" in msg.lower() or "record layer" in msg.lower():
        return "tls_not_spoken", {}
    return "tls_failed", {"detail": msg[:160]}


def _connect_tls(run: _Run, ip: ipaddress._BaseAddress, spec: dict):
    """TCP + (по режиму) TLS; возвращает открытый сокет (ssl или обычный) либо None."""
    host, port, mode, timeout = spec["host"], spec["port"], spec["tls"], spec["timeout"]
    t0 = time.monotonic()
    try:
        s = _tcp(ip, port, timeout)
    except ConnectionRefusedError:
        run.step("tcp", False, code="refused", ip=str(ip))
        return None
    except (socket.timeout, TimeoutError):
        run.step("tcp", False, code="timeout", ip=str(ip))
        return None
    except OSError as e:
        run.step("tcp", False, code="unreachable", ip=str(ip), detail=str(e)[:120])
        return None
    run.step("tcp", True, int((time.monotonic() - t0) * 1000), ip=str(ip))
    if mode == "off":
        if spec["kind"] == "mysql":
            try:
                head = _recv_exact(s, 4)
                payload = _recv_exact(s, head[0] | head[1] << 8 | head[2] << 16)
                run.step("protocol", payload[:1] == b"\x0a", code=None if payload[:1] == b"\x0a" else "not_mysql",
                         server=payload[1:payload.index(b"\x00", 1)].decode("latin1") if payload[:1] == b"\x0a" else None)
            except Exception:
                run.step("protocol", False, code="no_greeting")
        return s
    t1 = time.monotonic()
    try:
        reason = _starttls(spec["kind"], s)
        if reason:
            s.close()
            if mode == "prefer":
                run.step("tls", True, code="tls_unsupported_ignored")
                return _tcp(ip, port, timeout)
            run.step("tls", False, code=reason)
            return None
        ss = _ctx(mode, spec["ca_pem"]).wrap_socket(s, server_hostname=host)     # для IP-адреса SNI не отправляется, но имя нужно для проверки
    except Exception as e:                                  # ошибка проверки сертификата или рукопожатия
        cert = _peek_cert(ip, spec)                         # сертификат всё равно показываем: так понятнее, что не так
        code, params = _classify_tls_error(e, cert)
        if mode == "prefer" and code == "tls_not_spoken":
            run.step("tls", True, code="tls_unsupported_ignored")
            try:
                return _tcp(ip, port, timeout)
            except OSError:
                return None
        run.cert = cert
        run.step("tls", False, code=code, **params)
        return None
    info = describe_cert(ss.getpeercert(binary_form=True), host)
    run.cert = info
    verified = mode in ("verify-ca", "verify-full")
    trusted = verified or _is_trusted(ip, spec)
    problems = []
    if info["expired"]:
        problems.append("cert_expired")
    if not info["host_match"]:
        problems.append("cert_hostname_mismatch")
    if not trusted:
        problems.append("cert_untrusted")
    if info["days_left"] is not None and 0 <= info["days_left"] < 30:
        problems.append("cert_expires_soon")
    ok = not [p for p in problems if mode in ("require", "prefer") and p in ("cert_expired",)]
    run.step("tls", ok if mode in ("require", "prefer") else True, int((time.monotonic() - t1) * 1000), version=ss.version(),
             verified=verified or (trusted and info["host_match"] and not info["expired"]), warnings=problems or None,
             code="cert_expired" if "cert_expired" in problems and mode in ("require", "prefer") else None)
    return ss if ok else (ss.close() or None)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _peek_cert(ip, spec: dict) -> Optional[dict]:
    """Второе соединение без проверки: достаём сертификат, чтобы объяснить причину отказа."""
    try:
        s = _tcp(ip, spec["port"], spec["timeout"])
        if _starttls(spec["kind"], s):
            return None
        ss = _ctx("require", None).wrap_socket(s, server_hostname=spec["host"])
        info = describe_cert(ss.getpeercert(binary_form=True), spec["host"])
        ss.close()
        return info
    except Exception:
        return None


def _is_trusted(ip, spec: dict) -> bool:
    try:
        s = _tcp(ip, spec["port"], spec["timeout"])
        if _starttls(spec["kind"], s):
            return False
        ctx = _ctx("verify-ca", spec["ca_pem"])
        ss = ctx.wrap_socket(s, server_hostname=spec["host"])
        ss.close()
        return True
    except Exception:
        return False


# ---------- вход ----------

def _auth_redis(run: _Run, s, spec: dict) -> None:
    def cmd(*parts: str) -> bytes:
        s.sendall(f"*{len(parts)}\r\n".encode() + b"".join(f"${len(p.encode())}\r\n".encode() + p.encode() + b"\r\n" for p in parts))
        return s.recv(256)
    t0 = time.monotonic()
    if spec["password"]:
        r = cmd("AUTH", spec["user"], spec["password"]) if spec["user"] else cmd("AUTH", spec["password"])
        if not r.startswith(b"+OK"):
            run.step("auth", False, code="auth_failed", detail=r.decode("latin1").strip()[:100])
            return
    r = cmd("PING")
    if r.startswith(b"+PONG"):
        run.step("auth", True, int((time.monotonic() - t0) * 1000))
    elif r.startswith(b"-NOAUTH"):
        run.step("auth", False, code="auth_required")
    else:
        run.step("auth", False, code="unexpected_reply", detail=r.decode("latin1").strip()[:100])


def _auth_postgres(run: _Run, ip, spec: dict) -> None:
    try:
        import psycopg2
    except ImportError:
        run.step("auth", True, code="auth_skipped_no_driver")
        return
    mode = {"off": "disable", "prefer": "prefer", "require": "require", "verify-ca": "verify-ca", "verify-full": "verify-full"}[spec["tls"]]
    kw = dict(host=spec["host"], hostaddr=str(ip), port=spec["port"], user=spec["user"], password=spec["password"], dbname=spec["database"] or spec["user"],
              sslmode=mode, connect_timeout=int(spec["timeout"]))
    t0 = time.monotonic()
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=True) as f:
        if spec["ca_pem"] and mode in ("verify-ca", "verify-full"):
            f.write(spec["ca_pem"]); f.flush(); kw["sslrootcert"] = f.name
        try:
            conn = psycopg2.connect(**kw)
            cur = conn.cursor(); cur.execute("SELECT 1"); cur.fetchone(); conn.close()
            run.step("auth", True, int((time.monotonic() - t0) * 1000))
        except Exception as e:
            msg = str(e).strip().splitlines()[0][:200]
            low = msg.lower()
            code = ("auth_failed" if "password authentication failed" in low or "authentication failed" in low else
                    "database_missing" if "does not exist" in low and "database" in low else
                    "not_allowed_from_ip" if "pg_hba.conf" in low else "auth_error")
            run.step("auth", False, code=code, detail=msg)


def _http(s, host: str, port: int, path: str, headers: Optional[dict] = None) -> tuple[int, dict]:
    lines = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}", "Connection: close", "User-Agent: splitwave-connection-check"]
    lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 16384:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    head = data.split(b"\r\n\r\n")[0].decode("latin1").split("\r\n")
    status = int(head[0].split()[1]) if head and len(head[0].split()) > 1 else 0
    return status, {h.split(":", 1)[0].lower(): h.split(":", 1)[1].strip() for h in head[1:] if ":" in h}


def _sigv4_headers(spec: dict, path: str) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    host = f"{spec['host']}:{spec['port']}"
    empty = hashlib.sha256(b"").hexdigest()
    canonical = f"GET\n{path}\n\nhost:{host}\nx-amz-content-sha256:{empty}\nx-amz-date:{amz}\n\nhost;x-amz-content-sha256;x-amz-date\n{empty}"
    scope = f"{day}/{spec['region']}/s3/aws4_request"
    to_sign = f"AWS4-HMAC-SHA256\n{amz}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    k = ("AWS4" + spec["secret_key"]).encode()
    for part in (day, spec["region"], "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return {"x-amz-date": amz, "x-amz-content-sha256": empty,
            "Authorization": f"AWS4-HMAC-SHA256 Credential={spec['access_key']}/{scope}, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature={sig}"}


def _auth_http(run: _Run, s, spec: dict) -> None:
    t0 = time.monotonic()
    try:
        if spec["kind"] == "s3" and spec["access_key"] and spec["secret_key"]:
            status, h = _http(s, spec["host"], spec["port"], "/", _sigv4_headers(spec, "/"))
            ok = status == 200
            run.step("auth", ok, int((time.monotonic() - t0) * 1000), code=None if ok else ("auth_failed" if status in (401, 403) else "http_status"), status=status)
        else:
            status, h = _http(s, spec["host"], spec["port"], spec["path"])
            run.step("http", status > 0 and status < 500, int((time.monotonic() - t0) * 1000), status=status, server=h.get("server"),
                     code=None if 0 < status < 500 else "http_status")
    except Exception as e:
        run.step("http", False, code="http_failed", detail=str(e)[:120])


# ---------- точка входа ----------

def check(raw: dict) -> dict:
    spec = validate_spec(raw)
    run = _Run(spec)
    t0 = time.monotonic()
    try:
        addrs = _resolve(spec["host"])
    except OSError:
        run.step("dns", False, code="dns_failed")
        return _result(run, spec)
    bad = next((b for b in (_blocked(a) for a in addrs) if b), None)
    if bad:
        run.step("dns", False, code="address_forbidden", reason=bad)
        return _result(run, spec)
    ip = addrs[0]
    run.step("dns", True, int((time.monotonic() - t0) * 1000), addresses=[str(a) for a in addrs] if not _is_ip(spec["host"]) else None)
    s = _connect_tls(run, ip, spec)
    if s is not None:
        try:
            s.settimeout(spec["timeout"])
            has_creds = bool(spec["password"] or spec["secret_key"])
            if spec["kind"] == "redis":
                _auth_redis(run, s, spec)
            elif spec["kind"] == "postgres" and has_creds and spec["user"]:
                s.close(); s = None
                _auth_postgres(run, ip, spec)
            elif spec["kind"] in ("s3", "https"):
                _auth_http(run, s, spec)
            elif spec["kind"] in ("postgres", "mysql"):
                run.step("auth", True, code="auth_not_checked")
        except (socket.timeout, TimeoutError):
            run.step("auth", False, code="timeout")
        except Exception as e:
            run.step("auth", False, code="auth_error", detail=str(e)[:120])
        finally:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
    return _result(run, spec)


def _result(run: _Run, spec: dict) -> dict:
    failed = next((s for s in run.steps if not s["ok"]), None)
    warnings = [w for s in run.steps for w in (s.get("warnings") or [])]
    return {"target": {"kind": spec["kind"], "host": spec["host"], "port": spec["port"], "tls": spec["tls"]},
            "ok": failed is None, "failed_step": failed["step"] if failed else None, "code": failed.get("code") if failed else None,
            "steps": run.steps, "cert": run.cert, "warnings": warnings or None, "vantage": "control-plane"}
