"""Проверка адресов исходящих запросов платформы (защита от SSRF): только https, без учётных данных в адресе,
и по умолчанию только публичные адреса (запрещены loopback, частные сети, link-local).

Используется уведомлениями (webhook/Slack) и мастером подключения проекта (запросы к API Git-провайдера)."""
import ipaddress
import re
import socket
from typing import Callable, Optional
from urllib.parse import urlparse


def resolve(host: str) -> list[str]:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None)})


def check_url(url: str, allow_http: bool = False, allow_private: bool = False,
              resolver: Optional[Callable[[str], list[str]]] = None) -> str:
    """Возвращает имя хоста; ValueError — адрес недопустим."""
    u = urlparse(url)
    if u.scheme not in ("https", "http") or not u.hostname:
        raise ValueError("url must be an absolute https URL")
    if u.scheme == "http" and not allow_http:
        raise ValueError("http is not allowed (use https)")
    if u.username or u.password:
        raise ValueError("credentials in the URL are not allowed")
    try:
        addrs = [u.hostname] if re.fullmatch(r"[0-9a-fA-F:.]+", u.hostname) else (resolver or resolve)(u.hostname)
    except OSError:
        raise ValueError(f"cannot resolve host {u.hostname}")
    if not allow_private:
        for a in addrs:
            ip = ipaddress.ip_address(a.split("%")[0])
            if not ip.is_global:
                raise ValueError("target address is not public")
    return u.hostname
