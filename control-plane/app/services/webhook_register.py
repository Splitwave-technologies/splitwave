"""Автоматическое создание webhook в репозитории (GitHub, GitLab, Bitbucket Cloud, Gitea/Forgejo).

Нужен токен с правом управлять webhook'ами (он отличается от токена только на чтение, которым платформа клонирует код): токен используется
ровно для этого запроса и нигде не сохраняется. Все исходящие адреса проходят проверку SSRF (safeurl), редиректы не выполняются.
Операция идемпотентна: если webhook с таким адресом уже есть, он обновляется (секрет), а не дублируется."""
import json
from typing import Optional
from urllib.parse import quote, urlparse

import httpx

from app.config import settings
from app.services import safeurl
from app.services.repoinspect import RepoRef

GITHUB_EVENTS = ["push", "pull_request"]
BITBUCKET_EVENTS = ["repo:push", "pullrequest:created", "pullrequest:updated", "pullrequest:fulfilled", "pullrequest:rejected"]
RIGHTS = {"github": "admin:repo_hook (или Webhooks: Read and write у fine-grained токена)", "gitlab": "api (роль Maintainer в проекте)",
          "bitbucket": "Webhooks: Read and write", "gitea": "write:repository"}


class RegisterError(Exception):
    pass


def _call(method: str, url: str, headers: dict, body: Optional[dict] = None):
    try:
        safeurl.check_url(url, settings.scm_allow_http, settings.scm_allow_private_hosts)
    except ValueError as e:
        raise RegisterError(f"Адрес {urlparse(url).hostname} недоступен для платформы: {e}.")
    try:
        with httpx.Client(timeout=settings.scm_timeout_seconds, follow_redirects=False) as c:
            r = c.request(method, url, headers=headers, json=body)
    except httpx.HTTPError as e:
        raise RegisterError(f"Провайдер не отвечает ({type(e).__name__}).")
    try:
        data = r.json() if r.content else None
    except ValueError:
        data = None
    return r.status_code, data


def _fail(provider: str, status: int, data) -> RegisterError:
    if status in (401, 403):
        return RegisterError(f"Токен не принят или у него нет права управлять webhook'ами. Нужно: {RIGHTS[provider]}.")
    if status == 404:
        return RegisterError("Репозиторий не найден или у токена нет к нему доступа.")
    detail = ""
    if isinstance(data, dict):
        detail = str(data.get("message") or data.get("error") or "")[:160]
    return RegisterError(f"Провайдер отказал ({status}){': ' + detail if detail else ''}.")


def _github(ref: RepoRef, token: str, url: str, secret: str) -> str:
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "splitwave"}
    base = f"https://api.github.com/repos/{ref.full_name}/hooks"
    st, hooks = _call("GET", base + "?per_page=100", h)
    if st != 200:
        raise _fail("github", st, hooks)
    cfg = {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0" if url.startswith("https://") else "1"}
    found = next((x for x in hooks if (x.get("config") or {}).get("url") == url), None)
    if found:
        st, data = _call("PATCH", f"{base}/{found['id']}", h, {"config": cfg, "events": GITHUB_EVENTS, "active": True})
        if st != 200:
            raise _fail("github", st, data)
        return "updated"
    st, data = _call("POST", base, h, {"name": "web", "active": True, "events": GITHUB_EVENTS, "config": cfg})
    if st != 201:
        raise _fail("github", st, data)
    return "created"


def _gitlab(ref: RepoRef, token: str, url: str, secret: str) -> str:
    h = {"PRIVATE-TOKEN": token, "User-Agent": "splitwave"}
    proj = f"{ref.base}/api/v4/projects/{quote(ref.full_name, safe='')}"
    st, hooks = _call("GET", proj + "/hooks", h)
    if st != 200:
        raise _fail("gitlab", st, hooks)
    body = {"url": url, "token": secret, "push_events": True, "merge_requests_events": True, "enable_ssl_verification": url.startswith("https://")}
    found = next((x for x in hooks if x.get("url") == url), None)
    if found:
        st, data = _call("PUT", f"{proj}/hooks/{found['id']}", h, body)
        if st != 200:
            raise _fail("gitlab", st, data)
        return "updated"
    st, data = _call("POST", proj + "/hooks", h, body)
    if st != 201:
        raise _fail("gitlab", st, data)
    return "created"


def _bitbucket(ref: RepoRef, token: str, url: str, secret: str) -> str:
    h = {"Authorization": f"Bearer {token}", "User-Agent": "splitwave"}
    base = f"https://api.bitbucket.org/2.0/repositories/{ref.full_name}/hooks"
    st, data = _call("GET", base + "?pagelen=100", h)
    if st != 200:
        raise _fail("bitbucket", st, data)
    body = {"description": "splitwave", "url": url, "active": True, "secret": secret, "events": BITBUCKET_EVENTS}
    found = next((x for x in (data or {}).get("values", []) if x.get("url") == url), None)
    if found:
        st, data = _call("PUT", f"{base}/{quote(found['uuid'], safe='')}", h, body)
        if st != 200:
            raise _fail("bitbucket", st, data)
        return "updated"
    st, data = _call("POST", base, h, body)
    if st != 201:
        raise _fail("bitbucket", st, data)
    return "created"


def _gitea(ref: RepoRef, token: str, url: str, secret: str) -> str:
    h = {"Authorization": f"token {token}", "User-Agent": "splitwave"}
    base = f"{ref.base}/api/v1/repos/{ref.full_name}/hooks"
    st, hooks = _call("GET", base, h)
    if st != 200:
        raise _fail("gitea", st, hooks)
    body = {"type": "gitea", "active": True, "events": ["push", "pull_request"], "config": {"url": url, "content_type": "json", "secret": secret}}
    found = next((x for x in hooks if (x.get("config") or {}).get("url") == url), None)
    if found:
        st, data = _call("PATCH", f"{base}/{found['id']}", h, body)
        if st != 200:
            raise _fail("gitea", st, data)
        return "updated"
    st, data = _call("POST", base, h, body)
    if st != 201:
        raise _fail("gitea", st, data)
    return "created"


_PROVIDERS = {"github": _github, "gitlab": _gitlab, "bitbucket": _bitbucket, "gitea": _gitea}


def reachable_warning(public_url: str) -> Optional[str]:
    """Предупреждение, если провайдер заведомо не сможет достучаться до платформы по этому адресу."""
    import ipaddress
    host = (urlparse(public_url).hostname or "").lower()
    if host in ("localhost", "") or host.endswith(".local") or host.endswith(".internal"):
        return "Адрес платформы локальный: провайдер не сможет доставлять события. Укажите внешний адрес (PUBLIC_URL)."
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            return "Адрес платформы во внутренней сети: провайдер в интернете не сможет доставлять события."
    except ValueError:
        pass
    return None


def check_public_url(raw: Optional[str]) -> str:
    raw = (raw or settings.public_url or "").strip().rstrip("/")
    u = urlparse(raw)
    if not raw or u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
        raise RegisterError("Не задан внешний адрес платформы (вида https://dsp.example.com): укажите его или задайте PUBLIC_URL.")
    return raw


def register(provider: str, ref: RepoRef, token: str, public_url: Optional[str], path: str, secret: Optional[str]) -> dict:
    """{result: created|updated, url, warning?}; RegisterError — понятная причина."""
    if provider not in _PROVIDERS:
        raise RegisterError("Провайдер не поддерживается.")
    if not secret:
        raise RegisterError("Не настроен общий секрет webhook платформы (GITHUB_WEBHOOK_SECRET).")
    base = check_public_url(public_url)
    url = f"{base}{path}"
    result = _PROVIDERS[provider](ref, token, url, secret)
    out = {"result": result, "url": url}
    warn = reachable_warning(base)
    if warn:
        out["warning"] = warn
    return out
