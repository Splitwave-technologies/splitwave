"""Git-провайдеры: проверка подписи вебхука и приведение событий GitHub / GitLab / Bitbucket Cloud / Gitea (Forgejo) к одному виду.

Секрет вебхука для GitLab/Bitbucket/Gitea у каждого проекта свой и вычисляется из ключа платформы (ничего не хранится):
по нему платформа проверяет, что событие пришло от настроенного репозитория проекта."""
import hashlib
import hmac
from dataclasses import dataclass
from typing import Optional

from app.config import settings

PROVIDERS = ("github", "gitlab", "bitbucket", "gitea")
DEFAULT_HOST = {"github": "https://github.com", "gitlab": "https://gitlab.com", "bitbucket": "https://bitbucket.org"}
GIT_USERNAME = {"github": "x-access-token", "gitlab": "oauth2", "bitbucket": "x-token-auth", "gitea": "git"}
ZERO = "0" * 40


@dataclass(frozen=True)
class Push:
    repo: str
    branch: str
    revision: str
    clone_url: str


def webhook_secret(slug: str) -> str:
    key = next((k.strip() for k in settings.secret_encryption_key.split(",") if k.strip()), "")
    if not key:
        raise RuntimeError("secret_encryption_key is not configured")
    return hmac.new(hashlib.sha256(b"webhook|" + key.encode()).digest(), slug.encode(), hashlib.sha256).hexdigest()[:40]


def verify(provider: str, headers, raw: bytes, secret: str) -> bool:
    """Проверка подлинности события (сравнение за постоянное время)."""
    if provider == "gitlab":
        return hmac.compare_digest(headers.get("x-gitlab-token", ""), secret)
    mac = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if provider == "bitbucket":
        return hmac.compare_digest(headers.get("x-hub-signature", ""), "sha256=" + mac)
    if provider == "gitea":
        sig = headers.get("x-gitea-signature") or headers.get("x-forgejo-signature") or ""
        alt = headers.get("x-hub-signature-256", "")
        return hmac.compare_digest(sig, mac) or hmac.compare_digest(alt, "sha256=" + mac)
    return False


def default_clone_url(provider: str, repo: str, git_url: Optional[str]) -> str:
    if git_url:
        return git_url
    return f"{DEFAULT_HOST[provider]}/{repo}.git"


def _branch(ref: str) -> Optional[str]:
    return ref.removeprefix("refs/heads/") if ref and ref.startswith("refs/heads/") else None


def parse_push(provider: str, headers, p: dict) -> list[Push]:
    """Список запушенных веток (пусто — событие не про пуш в ветку или ветка удалена)."""
    if provider in ("github", "gitea"):
        event = headers.get("x-github-event") or headers.get("x-gitea-event") or headers.get("x-forgejo-event")
        branch, rev = _branch(p.get("ref", "")), p.get("after")
        repo = p.get("repository") or {}
        if event != "push" or not branch or not rev or rev == ZERO:
            return []
        return [Push(repo.get("full_name", ""), branch, rev, repo.get("clone_url", ""))]
    if provider == "gitlab":
        if p.get("object_kind") != "push" and headers.get("x-gitlab-event") != "Push Hook":
            return []
        branch, rev = _branch(p.get("ref", "")), p.get("checkout_sha") or p.get("after")
        proj = p.get("project") or {}
        if not branch or not rev or rev == ZERO:
            return []
        return [Push(proj.get("path_with_namespace", ""), branch, rev, proj.get("git_http_url", ""))]
    if provider == "bitbucket":
        if headers.get("x-event-key") != "repo:push":
            return []
        repo = (p.get("repository") or {}).get("full_name", "")
        out = []
        for ch in (p.get("push") or {}).get("changes", []):
            new = ch.get("new") or {}
            if new.get("type") == "branch" and (new.get("target") or {}).get("hash"):
                out.append(Push(repo, new["name"], new["target"]["hash"], f"https://bitbucket.org/{repo}.git"))
        return out
    return []


def parse_pull_request(provider: str, headers, p: dict) -> Optional[dict]:
    """Единый вид события PR/MR: action open|update|close, номер, ветка, sha, репозитории головы и базы (для защиты от форков)."""
    if provider in ("github", "gitea"):
        ev = headers.get("x-github-event") or headers.get("x-gitea-event") or headers.get("x-forgejo-event")
        if ev != "pull_request":
            return None
        pr, repo = p.get("pull_request") or {}, p.get("repository") or {}
        action = {"opened": "open", "reopened": "open", "ready_for_review": "open", "synchronize": "update", "synchronized": "update",
                  "closed": "close"}.get(p.get("action"))
        head = pr.get("head") or {}
        number = p.get("number") or pr.get("number")
        return {"provider": provider, "action": action, "number": number, "repo": repo.get("full_name"),
                "head_repo": (head.get("repo") or {}).get("full_name"), "branch": head.get("ref"), "sha": head.get("sha"),
                "author": (pr.get("user") or {}).get("login", "unknown"), "clone_url": repo.get("clone_url")}
    if provider == "gitlab":
        if p.get("object_kind") != "merge_request":
            return None
        a = p.get("object_attributes") or {}
        action = {"open": "open", "reopen": "open", "update": "update", "close": "close", "merge": "close"}.get(a.get("action"))
        target = (p.get("project") or {})
        return {"provider": provider, "action": action, "number": a.get("iid"), "repo": target.get("path_with_namespace"),
                "head_repo": (a.get("source") or {}).get("path_with_namespace"), "branch": a.get("source_branch"),
                "sha": (a.get("last_commit") or {}).get("id"), "author": (p.get("user") or {}).get("username", "unknown"),
                "clone_url": target.get("git_http_url")}
    if provider == "bitbucket":
        key = headers.get("x-event-key", "")
        if not key.startswith("pullrequest:"):
            return None
        pr = p.get("pullrequest") or {}
        action = {"pullrequest:created": "open", "pullrequest:updated": "update", "pullrequest:fulfilled": "close", "pullrequest:rejected": "close"}.get(key)
        src, dst = pr.get("source") or {}, pr.get("destination") or {}
        repo = (dst.get("repository") or {}).get("full_name")
        return {"provider": provider, "action": action, "number": pr.get("id"), "repo": repo,
                "head_repo": (src.get("repository") or {}).get("full_name"), "branch": (src.get("branch") or {}).get("name"),
                "sha": (src.get("commit") or {}).get("hash"), "author": (pr.get("author") or {}).get("nickname", "unknown"),
                "clone_url": f"https://bitbucket.org/{repo}.git" if repo else None}
    return None
