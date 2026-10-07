from app import security as sec

# Контрольные векторы RFC 6238 (SHA-1, секрет "12345678901234567890", 8 цифр в оригинале, берём последние 6)
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32("12345678901234567890")


def test_totp_rfc6238_vectors():
    for at, expected8 in [(59, "94287082"), (1111111109, "07081804"), (1234567890, "89005924"), (2000000000, "69279037")]:
        assert sec.totp_code(RFC_SECRET, at=at) == expected8[-6:]


def test_totp_window_and_replay_counter():
    now = 1_700_000_000
    code = sec.totp_code(RFC_SECRET, at=now)
    c = sec.verify_totp(RFC_SECRET, code, at=now)
    assert c == now // 30
    assert sec.verify_totp(RFC_SECRET, code, at=now + 30) == c            # соседний интервал допустим (расхождение часов)
    assert sec.verify_totp(RFC_SECRET, code, at=now + 120) is None         # слишком старый код
    assert sec.verify_totp(RFC_SECRET, "12345", at=now) is None
    assert sec.verify_totp(RFC_SECRET, "abcdef", at=now) is None


def test_password_hash_roundtrip_and_uniqueness():
    h1, h2 = sec.hash_password("correct horse battery"), sec.hash_password("correct horse battery")
    assert h1 != h2 and h1.startswith("scrypt$")                            # соль уникальна
    assert sec.verify_password("correct horse battery", h1)
    assert not sec.verify_password("wrong password!!", h1)
    assert not sec.verify_password("x", "garbage") and not sec.verify_password("x", "scrypt$1$2")


def test_password_policy():
    assert "at least 12 characters" in sec.password_problems("short")
    assert "too repetitive" in sec.password_problems("aaaaaaaaaaaaaaaa")
    assert "must not contain the username" in sec.password_problems("alice-and-bob-2026", "alice")
    assert sec.password_problems("Str0ng-Enough-Passphrase") == []


def test_recovery_codes_and_session_tokens():
    codes = sec.new_recovery_codes()
    assert len(codes) == 8 and len(set(codes)) == 8 and all(len(c) == 11 and c[5] == "-" for c in codes)
    assert sec.hash_recovery(codes[0]) == sec.hash_recovery(codes[0].upper().replace("-", " ").replace(" ", "-"))
    a, b = sec.new_session_token(), sec.new_session_token()
    assert a != b and len(a) >= 40 and sec.hash_session_token(a) != a


def test_otpauth_uri_contains_issuer_and_secret():
    uri = sec.otpauth_uri("ABCDEF", "alice@example.com")
    assert uri.startswith("otpauth://totp/") and "secret=ABCDEF" in uri and "SplitWave" in uri
