import base64
import json
from datetime import date

import pytest
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import licensing


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


@pytest.fixture
def issuer():
    priv = Ed25519PrivateKey.generate()
    pub = base64.b64encode(priv.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)).decode()

    def sign(**payload):
        body = _b64u(json.dumps(payload).encode())
        return f"{body}.{_b64u(priv.sign(body.encode()))}"

    return pub, sign


def test_no_key_is_community():
    lic = licensing.verify("")
    assert lic.status == "none" and lic.tier == "community" and not lic.features


def test_valid_license(issuer):
    pub, sign = issuer
    key = sign(customer="Acme", tier="pro", features=["audit_export", "sso"], expires_at="2999-01-01")
    lic = licensing.verify(key, pub)
    assert lic.valid and lic.tier == "pro" and lic.customer == "Acme"
    assert lic.features == {"audit_export", "sso"}


def test_tampered_payload_is_rejected(issuer):
    pub, sign = issuer
    key = sign(customer="Acme", tier="lite", features=[], expires_at="2999-01-01")
    forged_payload = _b64u(json.dumps({"customer": "Acme", "tier": "enterprise", "features": ["sso"], "expires_at": "2999-01-01"}).encode())
    forged = forged_payload + "." + key.split(".")[1]
    assert licensing.verify(forged, pub).status == "invalid"


def test_key_signed_by_someone_else_is_rejected(issuer):
    _, sign = issuer
    other_pub = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)).decode()
    assert licensing.verify(sign(tier="pro", features=["sso"]), other_pub).status == "invalid"


def test_garbage_is_invalid(issuer):
    pub, _ = issuer
    for junk in ("abc", "a.b", "x.y.z", "....", "!!!.???"):
        assert licensing.verify(junk, pub).status == "invalid"


def test_expired_license_loses_features(issuer):
    pub, sign = issuer
    key = sign(customer="Acme", tier="pro", features=["sso"], expires_at="2026-01-01")
    lic = licensing.verify(key, pub, today=date(2026, 6, 1))
    assert lic.status == "expired" and not lic.features and lic.tier == "community"


def test_require_feature_gate(issuer, monkeypatch):
    from fastapi import HTTPException
    pub, sign = issuer
    monkeypatch.setattr(licensing.settings, "license_public_key", pub)
    monkeypatch.setattr(licensing.settings, "license_key", sign(tier="lite", features=["audit_export"], expires_at="2999-01-01"))
    licensing.current_license(refresh=True)
    assert licensing.require_feature("audit_export")().tier == "lite"
    with pytest.raises(HTTPException) as e:
        licensing.require_feature("sso")()
    assert e.value.status_code == 403 and e.value.detail["error"] == "feature_not_licensed"
    monkeypatch.setattr(licensing.settings, "license_key", "")
    licensing.current_license(refresh=True)


def test_license_endpoint_never_returns_key(client, admin, issuer, monkeypatch):
    pub, sign = issuer
    key = sign(customer="Acme", tier="pro", features=["sso"], expires_at="2999-01-01")
    monkeypatch.setattr(licensing.settings, "license_public_key", pub)
    monkeypatch.setattr(licensing.settings, "license_key", key)
    licensing.current_license(refresh=True)
    r = client.get("/api/license", headers=admin)
    assert r.status_code == 200 and r.json()["tier"] == "pro" and r.json()["features"] == ["sso"]
    assert key not in r.text and "ee_loaded" in r.json()
    monkeypatch.setattr(licensing.settings, "license_key", "")
    licensing.current_license(refresh=True)


def test_license_endpoint_permissions(client, make_token):
    assert client.get("/api/license").status_code == 401
    assert client.get("/api/license", headers=make_token("viewer")).status_code == 403
    assert client.get("/api/license", headers=make_token("devops")).status_code == 200
    assert client.get("/api/license", headers=make_token("admin", "adm2")).status_code == 200


def test_enterprise_module_not_loaded_without_license(monkeypatch):
    import sys
    import types
    from app import plugins
    fake = types.ModuleType("platform_ee")
    called = []
    fake.register = lambda app, lic: called.append(lic.tier)
    monkeypatch.setitem(sys.modules, "platform_ee", fake)
    monkeypatch.setattr(licensing.settings, "license_key", "")
    licensing.current_license(refresh=True)
    assert plugins.load_enterprise(object()) is False and called == []


def test_default_public_key_is_not_the_development_key():
    """Лицензия, подписанная тестовым (dev) ключом, не должна приниматься боевой сборкой."""
    from app import licensing
    assert licensing.DEFAULT_PUBLIC_KEY != "sWFtt1HNuDwDx/NgDMiBT7naY9e+Lh6gDCtOZhIfDx8="
    import base64
    assert len(base64.b64decode(licensing.DEFAULT_PUBLIC_KEY)) == 32


# ---------- привязка ключа к установке ----------

def test_installation_id_is_stable_and_has_no_secrets():
    from app.services import instance
    first = instance.get_id()
    assert first.startswith("dsp-") and len(first) == 24
    instance.reset_cache()
    assert instance.get_id() == first                       # хранится в базе, переживает перезапуск процесса


def test_key_bound_to_another_installation_does_not_work(issuer):
    pub, sign = issuer
    here = "dsp-aaaaaaaaaaaaaaaaaaaa"
    bound = licensing.verify(sign(customer="Acme", tier="pro", features=["sso"], expires_at="2999-01-01", install_id=here), pub)
    assert bound.valid and bound.install_id == here
    assert licensing.check_binding(bound, here).valid
    other = licensing.check_binding(bound, "dsp-bbbbbbbbbbbbbbbbbbbb")
    assert not other.valid and other.status == "invalid" and other.tier == "community" and "another installation" in other.reason
    unbound = licensing.verify(sign(customer="Acme", tier="pro", features=["sso"], expires_at="2999-01-01"), pub)
    assert licensing.check_binding(unbound, "anything").valid          # ключ без привязки действует везде (так выпускаются пробные)


def test_current_license_checks_the_binding(issuer):
    from app.services import instance
    pub, sign = issuer
    old_key, old_pub = licensing.settings.license_key, licensing.settings.license_public_key
    try:
        licensing.settings.license_public_key = pub
        licensing.settings.license_key = sign(customer="Acme", tier="pro", features=["sso"], expires_at="2999-01-01", install_id=instance.get_id())
        assert licensing.current_license(refresh=True).valid
        licensing.settings.license_key = sign(customer="Acme", tier="pro", features=["sso"], expires_at="2999-01-01", install_id="dsp-someoneelse0000000")
        assert licensing.current_license(refresh=True).status == "invalid"
    finally:
        licensing.settings.license_key, licensing.settings.license_public_key = old_key, old_pub
        licensing.current_license(refresh=True)


def test_license_api_shows_the_installation_id(client, admin):
    from app.services import instance
    r = client.get("/api/license", headers=admin).json()
    assert r["install_id"] == instance.get_id() and "key" not in r
