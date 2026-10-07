"""Лицензии платных функций.

Ключ — `<payload>.<подпись>` (base64url): payload — JSON (customer, tier, features, expires_at),
подпись Ed25519 по тексту payload. Проверка офлайн открытым ключом; закрытый ключ хранится
только у издателя и в репозиториях не лежит.
Без ключа или с недействительным ключом работает редакция Community (платных функций нет)."""
import base64
import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger(__name__)

# Открытый ключ издателя (боевой). Закрытая часть хранится офлайн только у владельца продукта.
DEFAULT_PUBLIC_KEY = "tW85iaErQWVQT77lvEgeiP2mYOZrvgqmH9u/UAcp5zk="


@dataclass(frozen=True)
class LicenseInfo:
    status: str                      # none | valid | expired | invalid
    tier: str                        # community | lite | pro | enterprise
    customer: Optional[str]
    features: frozenset
    expires_at: Optional[str]
    reason: str
    install_id: Optional[str] = None          # привязка ключа к установке (необязательно)
    customer_id: Optional[str] = None         # привязка ключа к сборке, выданной клиенту (водяной знак)

    @property
    def valid(self) -> bool:
        return self.status == "valid"

    def public(self) -> dict:
        return {"status": self.status, "tier": self.tier, "customer": self.customer,
                "features": sorted(self.features), "expires_at": self.expires_at, "reason": self.reason}


def _community(status: str, reason: str, customer: Optional[str] = None, expires: Optional[str] = None) -> LicenseInfo:
    return LicenseInfo(status, "community", customer, frozenset(), expires, reason)


def check_binding(info: LicenseInfo, installation_id: str) -> LicenseInfo:
    """Ключ, привязанный к другой установке, не действует (работает Community). Ключ без привязки действует везде."""
    if info.valid and info.install_id and info.install_id != installation_id:
        return _community("invalid", "license is bound to another installation", info.customer, info.expires_at)
    return info


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify(key: str, public_key_b64: Optional[str] = None, today: Optional[date] = None) -> LicenseInfo:
    if not key or not key.strip():
        return _community("none", "no license key")
    try:
        payload_b64, sig_b64 = key.strip().split(".")
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64 or settings.license_public_key or DEFAULT_PUBLIC_KEY))
        pub.verify(_b64d(sig_b64), payload_b64.encode())
        payload = json.loads(_b64d(payload_b64))
    except (ValueError, InvalidSignature, KeyError):
        return _community("invalid", "invalid license key")
    expires = payload.get("expires_at")
    try:
        if expires and date.fromisoformat(expires) < (today or date.today()):
            return _community("expired", f"license expired on {expires}", payload.get("customer"), expires)
    except ValueError:
        return _community("invalid", "invalid expiry date")
    return LicenseInfo("valid", str(payload.get("tier", "lite")), payload.get("customer"),
                       frozenset(payload.get("features", [])), expires, "ok", payload.get("install_id"), payload.get("customer_id"))


_cached: Optional[LicenseInfo] = None


def _read_key() -> str:
    if settings.license_key:
        return settings.license_key
    if settings.license_file and Path(settings.license_file).is_file():
        return Path(settings.license_file).read_text().strip()
    return ""


def current_license(refresh: bool = False) -> LicenseInfo:
    global _cached
    if _cached is None or refresh:
        info = verify(_read_key())
        if info.valid and info.install_id:
            try:
                from app.services import instance
                info = check_binding(info, instance.get_id())
            except Exception:
                return info              # база ещё не готова (первый запуск, миграции выполняются при старте): привязку проверим при следующем обращении, не кешируя
        _cached = info
        if _cached.status in ("invalid", "expired"):
            logger.warning("License problem: %s — running as Community", _cached.reason)
    return _cached


def require_feature(feature: str):
    """Зависимость FastAPI для платных эндпоинтов: 403, если функции нет в лицензии."""

    def dependency() -> LicenseInfo:
        lic = current_license()
        if feature not in lic.features:
            raise HTTPException(status_code=403, detail={"error": "feature_not_licensed", "feature": feature, "tier": lic.tier})
        return lic

    return dependency


# Лимиты редакций (None — без ограничений; 0 — недоступно). Проверяются при создании; уже созданное не удаляется и не блокируется.
#   projects / environments (на проект) / users (учётные записи людей; API-токены не считаются) проверяет ядро;
#   clusters (зарегистрированные удалённые) / servers (с агентом) / previews (одновременные временные среды PR) проверяет платный модуль.
# Временные среды превью (preview_of) в лимит сред не входят. Без действующей лицензии (нет ключа, истёк, недействителен) действует Community.
_UNLIMITED = {"projects": None, "environments": None, "users": None, "clusters": None, "servers": None, "previews": None}
TIER_LIMITS = {
    "community": {"projects": 1, "environments": 1, "users": 1, "clusters": 0, "servers": 0, "previews": 0},
    "lite": {"projects": 5, "environments": 3, "users": 3, "clusters": 0, "servers": 0, "previews": 3},
    "pro": {"projects": 25, "environments": 10, "users": 10, "clusters": 3, "servers": 5, "previews": 20},
    "enterprise": dict(_UNLIMITED),
}


def limits(lic: Optional[LicenseInfo] = None) -> dict:
    lic = lic or current_license()
    tier = lic.tier if lic.valid else "community"
    table = TIER_LIMITS.get(tier, TIER_LIMITS["lite"])           # неизвестная метка редакции — как Lite
    return {"tier": tier, **{f"max_{k}": v for k, v in table.items()}}