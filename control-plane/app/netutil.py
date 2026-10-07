"""Адрес клиента и схема запроса за обратным прокси.

Заголовки X-Forwarded-For / X-Forwarded-Proto принимаются ТОЛЬКО от прокси из TRUSTED_PROXIES (список сетей CIDR через запятую):
иначе любой клиент подставил бы себе чужой адрес и обошёл ограничение перебора паролей. Адрес клиента — первый справа
адрес, не принадлежащий доверенным прокси."""
import ipaddress
from typing import Optional

from fastapi import Request

from app.config import settings


def _networks() -> list:
    nets = []
    for part in settings.trusted_proxies.split(","):
        part = part.strip()
        if part:
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                pass
    return nets


def _ip(value: str) -> Optional[ipaddress._BaseAddress]:
    try:
        return ipaddress.ip_address(value.strip().split("%")[0])
    except ValueError:
        return None


def _trusted(value: str, nets: list) -> bool:
    ip = _ip(value)
    return bool(ip) and any(ip in n for n in nets)


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    nets = _networks()
    if not nets or not _trusted(peer, nets):
        return peer
    chain = [p for p in (request.headers.get("x-forwarded-for") or "").split(",") if p.strip()]
    for hop in reversed(chain):
        ip = _ip(hop)
        if ip is None:
            return peer            # мусор в заголовке: не доверяем ничему, что левее
        if not _trusted(hop, nets):
            return str(ip)
    return peer


def is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else ""
    return _trusted(peer, _networks()) and (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower() == "https"
