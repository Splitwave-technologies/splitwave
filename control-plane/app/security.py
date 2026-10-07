"""Примитивы безопасности без внешних зависимостей: пароли (scrypt), TOTP (RFC 6238), коды восстановления, токены сессий."""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from typing import Optional

from app.config import settings

# ───────────── пароли ─────────────
_R, _P = 8, 1
_MAXMEM = 128 * 1024 * 1024
_COMMON = {"password1234", "123456789012", "qwertyuiop12", "administrator", "changemenow1", "letmein12345"}


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    n = 2 ** settings.scrypt_log2_n
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=_R, p=_P, maxmem=_MAXMEM, dklen=32)
    return f"scrypt${n}${_R}${_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), maxmem=_MAXMEM, dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


# Хеш-пустышка: сравнение с ним делается для несуществующего пользователя, чтобы время ответа не выдавало, есть ли такой логин.
_DUMMY = None


def dummy_verify(password: str) -> None:
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("dummy-password-for-timing")
    verify_password(password, _DUMMY)


def password_problems(password: str, username: str = "") -> list[str]:
    problems = []
    if len(password) < 12:
        problems.append("at least 12 characters")
    if len(set(password)) < 5:
        problems.append("too repetitive")
    if username and username.lower() in password.lower():
        problems.append("must not contain the username")
    if password.lower() in _COMMON:
        problems.append("too common")
    return problems


def random_password(length: int = 20) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ───────────── TOTP (RFC 6238, SHA-1, 6 цифр, шаг 30 с) ─────────────
def new_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp_code(secret_b32: str, at: Optional[float] = None, step: int = 30) -> str:
    return _hotp(secret_b32, int((time.time() if at is None else at) // step))


def verify_totp(secret_b32: str, code: str, at: Optional[float] = None, step: int = 30, window: int = 1) -> Optional[int]:
    """Возвращает номер интервала, которому соответствует код (для защиты от повторного использования), или None."""
    code = (code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return None
    now = int((time.time() if at is None else at) // step)
    for c in range(now - window, now + window + 1):
        if hmac.compare_digest(_hotp(secret_b32, c), code):
            return c
    return None


def otpauth_uri(secret_b32: str, account: str, issuer: str = "SplitWave") -> str:
    from urllib.parse import quote
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret_b32}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


# ───────────── коды восстановления ─────────────
def new_recovery_codes(n: int = 8) -> list[str]:
    def one() -> str:
        raw = base64.b32encode(os.urandom(7)).decode().lower().rstrip("=")[:10]
        return f"{raw[:5]}-{raw[5:]}"
    return [one() for _ in range(n)]


def hash_recovery(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace("-", "").encode()).hexdigest()


# ───────────── токены сессий ─────────────
def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
