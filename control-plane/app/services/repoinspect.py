"""Мастер подключения проекта, часть 1: по адресу репозитория выяснить, что это за проект, и предложить настройки.

Платформа читает репозиторий через API Git-провайдера (GitHub, GitLab, Bitbucket Cloud, Gitea/Forgejo) — только чтение,
ничего не клонируется и не сохраняется: токен живёт ровно один запрос. Определяются: провайдер и адрес клонирования,
ветка по умолчанию и её последний коммит, язык и фреймворк, Dockerfile (в корне и в типичных подпапках), порт, имена
переменных из .env.example и предупреждения. Все исходящие запросы проходят проверку SSRF (safeurl), редиректы не
выполняются, размер ответов ограничен."""
import json
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlparse

import httpx

from app.config import settings
from app.services import dockerfiles as dockergen
from app.services import safeurl, scm

DEFAULT_HOSTS = {"github.com": "github", "gitlab.com": "gitlab", "bitbucket.org": "bitbucket",
                 "codeberg.org": "gitea", "gitea.com": "gitea"}
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_FILE = 64 * 1024
MAX_JSON = 1024 * 1024
CALL_BUDGET = 60                      # обращений к API провайдера на один разбор репозитория


class InspectError(Exception):
    """Понятная пользователю причина, почему репозиторий не удалось разобрать."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.message, self.status = message, status


# ---------- адрес репозитория ----------

@dataclass(frozen=True)
class RepoRef:
    provider: str
    base: str                          # https://host (для API)
    full_name: str                     # owner/repo или группа/подгруппа/repo
    git_url: Optional[str]             # None — адрес по умолчанию для github.com / gitlab.com / bitbucket.org
    ref_hint: Optional[str] = None     # ветка из адреса вида /tree/<ветка>/...
    path_hint: str = ""                # подпапка из адреса


def _guess_provider(host: str, hint: Optional[str]) -> str:
    if hint:
        if hint not in scm.PROVIDERS:
            raise InspectError(f"provider must be one of {', '.join(scm.PROVIDERS)}")
        return hint
    if host in DEFAULT_HOSTS:
        return DEFAULT_HOSTS[host]
    label = host.split(".")[0]
    if label == "gitlab":
        return "gitlab"
    if label in ("gitea", "forgejo", "codeberg"):
        return "gitea"
    raise InspectError("Не удалось определить провайдера по адресу — выберите его вручную (GitHub, GitLab, Bitbucket, Gitea/Forgejo).")


def parse_repo_url(raw: str, provider_hint: Optional[str] = None) -> RepoRef:
    text = (raw or "").strip()
    if not text:
        raise InspectError("Укажите адрес репозитория.")
    m = re.match(r"^(?:ssh://)?git@([^:/\s]+)[:/](.+)$", text)          # git@github.com:owner/repo.git
    if m:
        text = f"https://{m.group(1)}/{m.group(2)}"
    elif re.fullmatch(r"[\w.-]+/[\w.-]+", text) and not text.startswith("."):    # owner/repo
        text = f"https://github.com/{text}"
    elif "://" not in text:
        text = "https://" + text
    u = urlparse(text)
    if u.username or u.password:
        raise InspectError("Не вставляйте токен в адрес: введите его в отдельном поле.")
    if u.scheme != "https" and not (u.scheme == "http" and settings.scm_allow_http):
        raise InspectError("Поддерживаются только https-адреса.")
    host = (u.hostname or "").lower()
    if not host:
        raise InspectError("Не удалось разобрать адрес репозитория.")
    provider = _guess_provider(host, provider_hint)
    if provider == "github" and host != "github.com":
        raise InspectError("GitHub Enterprise Server пока не поддерживается (только github.com).")
    if provider == "bitbucket" and host != "bitbucket.org":
        raise InspectError("Bitbucket Server / Data Center пока не поддерживается (только bitbucket.org).")
    segs = [s for s in u.path.split("/") if s]
    ref_hint, path_hint = None, ""
    if provider == "gitlab":
        if "-" in segs:                                                  # .../group/repo/-/tree/<ветка>/<путь>
            i = segs.index("-")
            tail, segs = segs[i + 1:], segs[:i]
            if len(tail) >= 2 and tail[0] in ("tree", "blob"):
                ref_hint, path_hint = tail[1], "/".join(tail[2:])
    else:
        tail = segs[2:]
        segs = segs[:2]
        if provider == "github" and len(tail) >= 2 and tail[0] in ("tree", "blob"):
            ref_hint, path_hint = tail[1], "/".join(tail[2:])
        elif provider == "gitea" and len(tail) >= 3 and tail[0] == "src" and tail[1] == "branch":
            ref_hint, path_hint = tail[2], "/".join(tail[3:])
    if segs and segs[-1].endswith(".git"):
        segs[-1] = segs[-1][:-4]
    if len(segs) < 2 or not all(NAME_RE.match(s) and s not in (".", "..") for s in segs):
        raise InspectError("Адрес должен вести на репозиторий: https://хост/владелец/репозиторий")
    base = f"{u.scheme}://{u.netloc.lower()}"
    full = "/".join(segs)
    default_base = scm.DEFAULT_HOST.get(provider)
    git_url = None if base == default_base else f"{base}/{full}.git"
    if not all(NAME_RE.match(p) for p in path_hint.split("/") if p) or ".." in path_hint.split("/"):
        path_hint = ""
    return RepoRef(provider, base, full, git_url, ref_hint, path_hint.strip("/"))


# ---------- обращения к API провайдера ----------

def fetch(url: str, headers: dict, limit: int = MAX_JSON) -> tuple[int, bytes]:
    """Один GET без редиректов с проверкой SSRF и ограничением размера (в тестах подменяется)."""
    try:
        safeurl.check_url(url, settings.scm_allow_http, settings.scm_allow_private_hosts)
    except ValueError as e:
        raise InspectError(f"Адрес {urlparse(url).hostname} недоступен для платформы: {e}.")
    try:
        with httpx.Client(timeout=settings.scm_timeout_seconds, follow_redirects=False) as c:
            with c.stream("GET", url, headers=headers) as r:
                if r.status_code in (301, 302, 307, 308):          # тело не нужно: передаём только адрес переезда
                    return r.status_code, r.headers.get("location", "").encode()[:500]
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > limit:
                        body = body[:limit]
                        break
                return r.status_code, body
    except httpx.HTTPError as e:
        raise InspectError(f"Провайдер не отвечает ({type(e).__name__}). Проверьте адрес и повторите.", 502)


def _moved_hint(location: bytes) -> str:
    """«: <новый адрес>» из заголовка Location, если он читаем (у GitHub API там числовой id — тогда подсказки нет)."""
    try:
        u = urlparse(location.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        return ""
    path = u.path
    for marker in ("/api/v4/projects/", "/api/v1/repos/", "/repos/", "/2.0/repositories/"):       # адрес API → адрес репозитория
        if marker in path:
            path = "/" + path.split(marker, 1)[1]
            break
    if "/repositories/" in path or path.rstrip("/").count("/") < 1 or any(c in path for c in " \"'<>\\") or len(path) > 150:
        return ""
    host = u.hostname.replace("api.", "", 1) if u.hostname.startswith("api.") else u.hostname
    from urllib.parse import unquote
    return f": {host}{unquote(path).rstrip('/')}"


class Client:
    """Общий каркас: учёт числа запросов, перевод ошибок провайдера в понятные сообщения."""
    api = ""

    def __init__(self, ref: RepoRef, token: Optional[str]):
        self.ref, self.token, self.calls = ref, token, 0

    def headers(self) -> dict:
        return {"User-Agent": "splitwave-onboarding", "Accept": "application/json"}

    def call(self, url: str, extra: Optional[dict] = None, limit: int = MAX_JSON, missing_ok: bool = False) -> Optional[bytes]:
        self.calls += 1
        if self.calls > CALL_BUDGET:
            raise InspectError("Репозиторий слишком большой для автоматического разбора: заполните настройки вручную.")
        status, body = fetch(url, {**self.headers(), **(extra or {})}, limit)
        if 200 <= status < 300:
            return body
        if status == 404 and missing_ok:
            return None
        if status in (301, 302, 307, 308):
            raise InspectError("Репозиторий перенесён или переименован: используйте его актуальный адрес" + _moved_hint(body) + ".")
        if status == 404:
            raise InspectError("Репозиторий не найден. Если он приватный, укажите токен доступа." if not self.token
                               else "Репозиторий не найден или у токена нет доступа к нему.")
        if status in (401, 403):
            raise InspectError("Нет доступа: репозиторий приватный или токен недействителен / без права чтения." if self.token
                               else "Нет доступа: репозиторий приватный или лимит запросов провайдера исчерпан — укажите токен.")
        if status == 429:
            raise InspectError("Провайдер ограничил число запросов: повторите позже или укажите токен.", 429)
        raise InspectError(f"Провайдер ответил ошибкой {status}.", 502)

    def json(self, url: str, **kw):
        body = self.call(url, **kw)
        if body is None:
            return None
        try:
            return json.loads(body)
        except ValueError:
            raise InspectError("Провайдер вернул неожиданный ответ.", 502)

    # интерфейс: repo_info() -> {default_branch, private}, head_sha(branch), list_dir(path, ref) -> [(name, is_dir)], read_file(path, ref) -> str|None


class GitHub(Client):
    def headers(self):
        h = {**super().headers(), "Accept": "application/vnd.github+json"}
        return {**h, "Authorization": f"Bearer {self.token}"} if self.token else h

    def _repo(self, tail=""):
        return f"https://api.github.com/repos/{self.ref.full_name}{tail}"

    def repo_info(self):
        d = self.json(self._repo())
        return {"default_branch": d.get("default_branch") or "main", "private": bool(d.get("private"))}

    def head_sha(self, branch):
        return (self.json(self._repo(f"/commits/{quote(branch, safe='')}")) or {}).get("sha", "")

    def list_dir(self, path, ref):
        d = self.json(self._repo(f"/contents/{quote(path)}?ref={quote(ref, safe='')}"), missing_ok=True)
        return [(e["name"], e.get("type") == "dir") for e in d] if isinstance(d, list) else []

    def read_file(self, path, ref):
        b = self.call(self._repo(f"/contents/{quote(path)}?ref={quote(ref, safe='')}"),
                      {"Accept": "application/vnd.github.raw+json"}, MAX_FILE, missing_ok=True)
        return None if b is None else b.decode("utf-8", "replace")


class GitLab(Client):
    def headers(self):
        h = super().headers()
        return {**h, "PRIVATE-TOKEN": self.token} if self.token else h

    def _proj(self, tail=""):
        return f"{self.ref.base}/api/v4/projects/{quote(self.ref.full_name, safe='')}{tail}"

    def repo_info(self):
        d = self.json(self._proj())
        return {"default_branch": d.get("default_branch") or "main", "private": d.get("visibility") != "public"}

    def head_sha(self, branch):
        return ((self.json(self._proj(f"/repository/branches/{quote(branch, safe='')}")) or {}).get("commit") or {}).get("id", "")

    def list_dir(self, path, ref):
        d = self.json(self._proj(f"/repository/tree?path={quote(path)}&ref={quote(ref, safe='')}&per_page=100"), missing_ok=True)
        return [(e["name"], e.get("type") == "tree") for e in d] if isinstance(d, list) else []

    def read_file(self, path, ref):
        b = self.call(self._proj(f"/repository/files/{quote(path, safe='')}/raw?ref={quote(ref, safe='')}"), None, MAX_FILE, missing_ok=True)
        return None if b is None else b.decode("utf-8", "replace")


class Bitbucket(Client):
    def headers(self):
        h = super().headers()
        return {**h, "Authorization": f"Bearer {self.token}"} if self.token else h

    def _repo(self, tail=""):
        return f"https://api.bitbucket.org/2.0/repositories/{self.ref.full_name}{tail}"

    def repo_info(self):
        d = self.json(self._repo())
        return {"default_branch": (d.get("mainbranch") or {}).get("name") or "main", "private": bool(d.get("is_private"))}

    def head_sha(self, branch):
        return ((self.json(self._repo(f"/refs/branches/{quote(branch, safe='')}")) or {}).get("target") or {}).get("hash", "")

    def list_dir(self, path, ref):
        d = self.json(self._repo(f"/src/{quote(ref, safe='')}/{quote(path)}{'/' if path else ''}?pagelen=100"), missing_ok=True)
        out = []
        for e in (d or {}).get("values", []):
            out.append((e["path"].rsplit("/", 1)[-1], e.get("type") == "commit_directory"))
        return out

    def read_file(self, path, ref):
        b = self.call(self._repo(f"/src/{quote(ref, safe='')}/{quote(path)}"), None, MAX_FILE, missing_ok=True)
        return None if b is None else b.decode("utf-8", "replace")


class Gitea(Client):
    def headers(self):
        h = super().headers()
        return {**h, "Authorization": f"token {self.token}"} if self.token else h

    def _repo(self, tail=""):
        return f"{self.ref.base}/api/v1/repos/{self.ref.full_name}{tail}"

    def repo_info(self):
        d = self.json(self._repo())
        return {"default_branch": d.get("default_branch") or "main", "private": bool(d.get("private"))}

    def head_sha(self, branch):
        return ((self.json(self._repo(f"/branches/{quote(branch, safe='')}")) or {}).get("commit") or {}).get("id", "")

    def list_dir(self, path, ref):
        d = self.json(self._repo(f"/contents/{quote(path)}?ref={quote(ref, safe='')}"), missing_ok=True)
        return [(e["name"], e.get("type") == "dir") for e in d] if isinstance(d, list) else []

    def read_file(self, path, ref):
        b = self.call(self._repo(f"/raw/{quote(path)}?ref={quote(ref, safe='')}"), None, MAX_FILE, missing_ok=True)
        return None if b is None else b.decode("utf-8", "replace")


CLIENTS = {"github": GitHub, "gitlab": GitLab, "bitbucket": Bitbucket, "gitea": Gitea}


# ---------- определение стека ----------

DOCKERFILE_NAMES = ("Dockerfile", "dockerfile", "Containerfile")
INFRA_DIRS = ("docker", ".docker", "deploy", "build")                  # Dockerfile лежит здесь, а контекст сборки — корень репозитория
APP_DIRS = ("backend", "server", "api", "app", "apps", "web", "frontend", "service", "services", "packages", "src")
LANG_MARKERS = [("node", ("package.json",)), ("python", ("requirements.txt", "pyproject.toml", "Pipfile", "setup.py")),
                ("go", ("go.mod",)), ("java", ("pom.xml", "build.gradle", "build.gradle.kts")), ("ruby", ("Gemfile",)),
                ("php", ("composer.json",)), ("rust", ("Cargo.toml",)), ("elixir", ("mix.exs",))]
BUILDPACK_LANGS = {"node", "python", "go", "java", "ruby", "php", "dotnet", "static"}
DEFAULT_PORT = {"node": 3000, "python": 8000, "go": 8080, "java": 8080, "ruby": 3000, "php": 8080, "dotnet": 8080, "static": 8080}
NODE_FRAMEWORKS = [("next", "Next.js"), ("nuxt", "Nuxt"), ("@nestjs/core", "NestJS"), ("express", "Express"), ("fastify", "Fastify"),
                   ("koa", "Koa"), ("@angular/core", "Angular"), ("react", "React"), ("vue", "Vue"), ("svelte", "Svelte")]
TEXT_FRAMEWORKS = {"python": [("fastapi", "FastAPI"), ("django", "Django"), ("flask", "Flask"), ("aiohttp", "aiohttp")],
                    "java": [("spring-boot", "Spring Boot"), ("quarkus", "Quarkus"), ("micronaut", "Micronaut")],
                    "php": [("laravel/framework", "Laravel"), ("symfony/", "Symfony")],
                    "ruby": [("rails", "Rails"), ("sinatra", "Sinatra")],
                    "go": [("gin-gonic", "Gin"), ("labstack/echo", "Echo"), ("gofiber", "Fiber")]}
FRONTEND_ONLY = {"React", "Vue", "Svelte", "Angular"}
SERVICE_PATTERNS = (
    ("database", re.compile(r"(DATABASE_URL|(^|_)DB(_|$)|DATASOURCE|POSTGRES|^PG[A-Z]*(HOST|USER|PASSWORD|DATABASE)|MYSQL|MARIADB|MONGO|JDBC)", re.I)),
    ("storage", re.compile(r"(MINIO|(^|_)S3(_|$)|AWS_|BUCKET|(^|_)R2_)", re.I)),
    ("cache", re.compile(r"(REDIS|VALKEY|MEMCACHE)", re.I)),
    ("queue", re.compile(r"(RABBIT|AMQP|KAFKA|(^|_)NATS|(^|_)SQS)", re.I)),
    ("search", re.compile(r"(ELASTIC|OPENSEARCH|MEILI|TYPESENSE)", re.I)),
)


def detect_services(names: list[str]) -> list[dict]:
    """По именам переменных из примера .env — какие внешние сервисы нужны приложению (платформа их не создаёт)."""
    out = []
    for code, rx in SERVICE_PATTERNS:
        hit = [n for n in names if rx.search(n)]
        if hit:
            out.append({"service": code, "names": hit[:8]})
    return out


ENV_FILES = (".env.example", ".env.sample", ".env.template", "example.env", ".env.dist")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return s if re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", s or "") else "app"


def parse_expose(dockerfile: str) -> Optional[int]:
    for line in dockerfile.splitlines():
        m = re.match(r"^\s*EXPOSE\s+(.+)$", line, re.I)
        if m:
            for tok in m.group(1).split():
                p = re.match(r"^(\d{2,5})(?:/(?:tcp|udp))?$", tok)
                if p and 1 <= int(p.group(1)) <= 65535:
                    return int(p.group(1))
    return None


def parse_env_names(text: str) -> list[str]:
    """Только имена переменных (значения из примера не читаются и не сохраняются)."""
    names = []
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) not in names:
            names.append(m.group(1))
    return names[:50]


class Probe:
    """Чтение репозитория с кэшем: каждая папка и файл запрашиваются не более одного раза."""

    def __init__(self, client, ref_name: str):
        self.client, self.ref, self._dirs, self._files = client, ref_name, {}, {}

    def ls(self, path: str) -> dict:
        if path not in self._dirs:
            self._dirs[path] = dict(self.client.list_dir(path, self.ref))
        return self._dirs[path]

    def cat(self, path: str) -> Optional[str]:
        if path not in self._files:
            self._files[path] = self.client.read_file(path, self.ref)
        return self._files[path]


def _find_language(probe: Probe, base: str) -> tuple[Optional[str], Optional[str], list[str]]:
    """(язык, фреймворк, заметки) для папки base."""
    names = probe.ls(base)
    join = lambda n: f"{base}/{n}" if base else n
    lang = next((l for l, marks in LANG_MARKERS if any(m in names and not names[m] for m in marks)), None)
    if lang is None and any(n.endswith((".csproj", ".sln", ".fsproj")) for n in names):
        lang = "dotnet"
    if lang is None and "index.html" in names:
        lang = "static"
    notes, framework = [], None
    if lang == "node":
        raw = probe.cat(join("package.json")) or "{}"
        try:
            pkg = json.loads(raw)
        except ValueError:
            pkg = {}
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        framework = next((label for dep, label in NODE_FRAMEWORKS if dep in deps), None)
        if "start" not in (pkg.get("scripts") or {}):
            notes.append("no_start_script")
    elif lang in TEXT_FRAMEWORKS:
        marker = {"python": ("requirements.txt", "pyproject.toml", "Pipfile"), "java": ("pom.xml", "build.gradle", "build.gradle.kts"),
                  "php": ("composer.json",), "ruby": ("Gemfile",), "go": ("go.mod",)}[lang]
        text = "\n".join((probe.cat(join(m)) or "") for m in marker if m in names).lower()
        framework = next((label for needle, label in TEXT_FRAMEWORKS[lang] if needle in text), None)
        if lang == "python" and "Procfile" not in names:
            notes.append("python_no_procfile")
    return lang, framework, notes


def _find_dockerfiles(probe: Probe, preferred: str = "") -> list[dict]:
    found = []
    root = probe.ls("")
    for n in DOCKERFILE_NAMES:
        if n in root and not root[n]:
            found.append({"sub_path": "", "dockerfile_path": n})
    for n in root:
        if (re.fullmatch(r"(?i)dockerfile\.[\w.-]+", n) or re.fullmatch(r"[\w.-]+\.Dockerfile", n)) and not root[n]:
            found.append({"sub_path": "", "dockerfile_path": n})
    dirs = [d for d in INFRA_DIRS + APP_DIRS if d in root and root[d]]
    if preferred and preferred.split("/")[0] in root and preferred.split("/")[0] not in dirs:
        dirs.insert(0, preferred.split("/")[0])
    for d in dirs[:8]:
        inner = probe.ls(d)
        for n in DOCKERFILE_NAMES:
            if n in inner and not inner[n]:
                if d in INFRA_DIRS:
                    found.append({"sub_path": "", "dockerfile_path": f"{d}/{n}"})
                else:
                    found.append({"sub_path": d, "dockerfile_path": n})
                break
    if not found:                                  # глубже на один уровень: src/app/Dockerfile, services/api/Dockerfile, apps/web/Dockerfile
        for d in dirs[:4]:
            for sub in [n for n, is_dir in probe.ls(d).items() if is_dir][:5]:
                inner = probe.ls(f"{d}/{sub}")
                name = next((n for n in DOCKERFILE_NAMES if n in inner and not inner[n]), None)
                if name:
                    found.append({"sub_path": f"{d}/{sub}", "dockerfile_path": name})
    return found


def platform_token(ref: "RepoRef") -> Optional[str]:
    """Токен чтения самой платформы — только для публичных хостов по умолчанию (адрес не переопределён), чтобы он не мог утечь на чужой сервер."""
    if ref.git_url is not None:
        return None
    return {"github": settings.scm_github_token, "gitlab": settings.scm_gitlab_token}.get(ref.provider) or None


SERVER_NODE_DEPS = ("express", "fastify", "koa", "@nestjs/core", "next", "nuxt", "hapi", "@hapi/hapi")
PY_ENTRY_FILES = ("main.py", "app.py", "server.py", "run.py", "wsgi.py")


def _major(text: str, lo: int, hi: int) -> Optional[str]:
    m = re.search(r"(\d{1,2})", text or "")
    return m.group(1) if m and lo <= int(m.group(1)) <= hi else None


def gather_facts(probe: "Probe", base: str, lang: str) -> dict:
    """Сведения о проекте, нужные шаблону Dockerfile (менеджер пакетов, скрипты, точка входа, версия среды). Читаются только имена и версии."""
    names = probe.ls(base)
    join = lambda n: f"{base}/{n}" if base else n
    facts: dict = {}
    if lang == "node":
        try:
            pkg = json.loads(probe.cat(join("package.json")) or "{}")
        except ValueError:
            pkg = {}
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        scripts = pkg.get("scripts") or {}
        pm = "yarn" if "yarn.lock" in names else "pnpm" if "pnpm-lock.yaml" in names else "npm"
        facts.update(package_manager=pm, has_lock=any(n in names for n in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")),
                     has_build="build" in scripts, has_start="start" in scripts)
        if isinstance(pkg.get("main"), str) and re.fullmatch(r"[A-Za-z0-9._/-]{1,60}", pkg["main"]) and ".." not in pkg["main"]:
            facts["start_command"] = pkg["main"]
        v = _major(str((pkg.get("engines") or {}).get("node", "")), 16, 24)
        if v:
            facts["runtime_version"] = v
        frontend = any(d in deps for d in ("react", "vue", "svelte", "@angular/core"))
        if frontend and not any(d in deps for d in SERVER_NODE_DEPS) and "build" in scripts:
            facts["static_site"] = True
            if "react-scripts" in deps:
                facts["build_output"] = "build"
            elif "vite" in deps or "@vue/cli-service" in deps:
                facts["build_output"] = "dist"
            else:
                facts["build_output"], facts["build_output_unknown"] = "dist", True
    elif lang == "python":
        deps_kind = "requirements" if "requirements.txt" in names else "pyproject" if "pyproject.toml" in names else "pipfile" if "Pipfile" in names else None
        if deps_kind:
            facts["deps"] = deps_kind
        text = "\n".join((probe.cat(join(f)) or "") for f in ("requirements.txt", "pyproject.toml", "Pipfile") if f in names).lower()
        procfile = probe.cat(join("Procfile")) if "Procfile" in names else None
        web = re.search(r"(?m)^web:\s*(.+)$", procfile or "")
        entry = next((f[:-3] for f in PY_ENTRY_FILES if f in names), None)
        extra: list[str] = []
        if web and "\x00" not in web.group(1):
            facts["start_command"] = web.group(1).strip()
        elif "fastapi" in text:
            facts["start_command"], facts["start_guess"] = f"uvicorn {entry or 'main'}:app --host 0.0.0.0 --port $PORT", True
            extra = [] if "uvicorn" in text else ["uvicorn"]
        elif "flask" in text:
            facts["start_command"], facts["start_guess"] = f"gunicorn -b 0.0.0.0:$PORT {entry or 'app'}:app", True
            extra = [] if "gunicorn" in text else ["gunicorn"]
        elif "django" in text and "manage.py" in names:
            project = next((d for d, is_dir in names.items() if is_dir and d not in ("static", "templates", "tests", "docs")
                            and "wsgi.py" in probe.ls(join(d))), None)
            if project:
                facts["start_command"], facts["start_guess"] = f"gunicorn -b 0.0.0.0:$PORT {project}.wsgi", True
                extra = [] if "gunicorn" in text else ["gunicorn"]
            else:
                facts["start_command"], facts["start_guess"] = "python manage.py runserver 0.0.0.0:$PORT", True
        elif entry:
            facts["start_command"] = f"python {entry}.py"
        else:
            facts["start_command"], facts["start_guess"] = "python main.py", True
        facts["extra_packages"] = extra
    elif lang == "go":
        mod = probe.cat(join("go.mod")) or ""
        m = re.search(r"(?m)^go\s+(\d+\.\d+)", mod)
        if m:
            facts["runtime_version"] = m.group(1)
        if "main.go" in names:
            facts["main_package"] = "."
        else:
            cmds = [d for d, is_dir in probe.ls(join("cmd")).items() if is_dir] if "cmd" in names else []
            if len(cmds) == 1 and re.fullmatch(r"[A-Za-z0-9._-]{1,60}", cmds[0]):
                facts["main_package"] = f"./cmd/{cmds[0]}"
            else:
                facts["main_package"], facts["main_unknown"] = ".", True
    elif lang == "php":
        facts["docroot"] = next((d for d in ("public", "web", "www", "htdocs") if d in names and names[d]), "")
    elif lang == "ruby":
        gem = (probe.cat(join("Gemfile")) or "").lower()
        facts["framework"] = "rails" if re.search(r"gem\s+['\"]rails['\"]", gem) else ("sinatra" if "sinatra" in gem else "rack")
        m = re.search(r"(?m)^web:\s*(.+)$", probe.cat(join("Procfile")) or "") if "Procfile" in names else None
        if m and len(m.group(1)) < 200 and "$PORT" in m.group(1):
            facts["procfile_web"] = m.group(1).strip()
    elif lang == "dotnet":
        projs = sorted(n for n in names if n.endswith((".csproj", ".fsproj")))
        if projs:
            facts["project"] = projs[0]
        else:                                           # решение (.sln) с проектами в подпапках: берём первый веб-проект
            for d in [n for n, is_dir in names.items() if is_dir][:8]:
                inner = sorted(n for n in probe.ls(join(d)) if n.endswith((".csproj", ".fsproj")))
                if inner:
                    facts["project"] = f"{d}/{inner[0]}"
                    break
        if facts.get("project"):                          # версия среды по TargetFramework проекта (net8.0, net10.0)
            m = re.search(r"<TargetFramework>net(\d{1,2}\.\d)</TargetFramework>", probe.cat(join(facts["project"])) or "")
            if m and 6 <= float(m.group(1)) <= 12:
                facts["runtime_version"] = m.group(1)
    elif lang == "rust":
        m = re.search(r'(?ms)^\[package\].*?^name\s*=\s*"([^"]+)"', probe.cat(join("Cargo.toml")) or "")
        if m:
            facts["binary"] = m.group(1)
    elif lang == "java":
        facts["build_tool"] = "maven" if "pom.xml" in names else "gradle"
        text = probe.cat(join("pom.xml")) if "pom.xml" in names else None
        m = re.search(r"<(?:java\.version|maven\.compiler\.release|maven\.compiler\.source)>\s*(\d{1,2})", text or "")
        if m and m.group(1) in ("11", "17", "21"):
            facts["runtime_version"] = m.group(1)
    return facts


def inspect_repo(raw_url: str, token: Optional[str] = None, provider_hint: Optional[str] = None,
                 branch: Optional[str] = None, client_factory=None) -> dict:
    """Предложение настроек для нового проекта; ничего не создаёт. InspectError — понятная причина отказа."""
    ref = parse_repo_url(raw_url, provider_hint)
    client = (client_factory or CLIENTS[ref.provider])(ref, token or platform_token(ref))
    info = client.repo_info()
    branch = branch or ref.ref_hint or info["default_branch"]
    head = client.head_sha(branch)
    if not head:
        raise InspectError(f"Ветка {branch} не найдена в репозитории.")
    probe = Probe(client, branch)
    root = probe.ls("")
    if not root:
        raise InspectError("Репозиторий пуст: в выбранной ветке нет файлов.")
    warnings: list[dict] = []
    dockerfiles = _find_dockerfiles(probe, ref.path_hint)
    chosen = dockerfiles[0] if dockerfiles else None
    if ref.path_hint:
        chosen = next((d for d in dockerfiles if d["sub_path"] == ref.path_hint or d["dockerfile_path"].startswith(ref.path_hint)), chosen)
    context = chosen["sub_path"] if chosen else ref.path_hint
    lang, framework, notes = _find_language(probe, context)
    alternatives = {"dockerfiles": dockerfiles, "subdirs": []}
    if lang is None and not chosen:               # монорепозиторий: ищем приложение в типичных подпапках
        skip = {".github", ".git", ".vscode", ".idea", "docs", "doc", "test", "tests", "examples", "example", "node_modules", "vendor", "scripts", "assets", "images"}
        known = [d for d in APP_DIRS if d in root and root[d]][:5]
        others = [d for d, is_dir in root.items() if is_dir and d not in known and d not in skip and not d.startswith(".")][:8]     # любая другая папка первого уровня
        for d in known + others:
            l2, f2, n2 = _find_language(probe, d)
            if l2:
                alternatives["subdirs"].append({"sub_path": d, "language": l2, "framework": f2})
        if alternatives["subdirs"]:
            first = alternatives["subdirs"][0]
            context, lang, framework = first["sub_path"], first["language"], first["framework"]
            notes = _find_language(probe, context)[2]
            warnings.append({"code": "monorepo", "message": f"Похоже на монорепозиторий: выбрана подпапка «{context}». Проверьте, что это нужное приложение."})
    port, port_source = None, "default"
    template = None
    if not chosen and lang in dockergen.SUPPORTED:      # Dockerfile нет, но стек знакомый: предлагаем свой (его можно править в мастере)
        gfacts = gather_facts(probe, context, lang)
        try:
            gen = dockergen.render(lang, gfacts, {})
            template = {"language": lang, "facts": gfacts, **gen}
        except dockergen.DockerfileError:
            template = None
    if template:
        build_method, dockerfile_path, sub_path = "dockerfile", "Dockerfile", context
        port, port_source = template["params"]["port"], "template"
    elif chosen:
        build_method, dockerfile_path, sub_path = "dockerfile", chosen["dockerfile_path"], chosen["sub_path"]
        text = probe.cat(f"{sub_path}/{dockerfile_path}" if sub_path else dockerfile_path) or ""
        port = parse_expose(text)
        port_source = "EXPOSE" if port else "default"
        if len(dockerfiles) > 1:
            warnings.append({"code": "many_dockerfiles", "message": f"В репозитории несколько Dockerfile ({len(dockerfiles)}): выбран «{chosen['dockerfile_path']}». Можно выбрать другой."})
    else:
        build_method, dockerfile_path, sub_path = "buildpacks", "Dockerfile", context
        if lang is None:
            warnings.append({"code": "unknown_stack", "message": "Не удалось определить язык проекта и Dockerfile не найден. Добавьте Dockerfile в репозиторий или укажите подпапку вручную."})
        elif lang not in BUILDPACK_LANGS:
            warnings.append({"code": "no_buildpack", "message": f"Для языка «{lang}» нет готового сборщика: добавьте в репозиторий Dockerfile."})
    if port is None:
        port = DEFAULT_PORT.get(lang or "", 8080) if build_method == "dockerfile" else 8080
        if build_method == "buildpacks":
            port_source = "PORT"
    bp_warnings: list[dict] = []                   # предупреждения на случай, если пользователь выберет buildpacks (показываются при выборе)
    for n in notes:
        msgs = {"no_start_script": "В package.json нет скрипта start: сборщик может не понять, как запускать приложение.",
                "python_no_procfile": "Для Python нужен файл Procfile с командой запуска (например, web: uvicorn main:app --host 0.0.0.0 --port $PORT)."}
        bp_warnings.append({"code": n, "message": msgs[n]})
    if framework in FRONTEND_ONLY:
        bp_warnings.append({"code": "frontend_only", "message": f"{framework} без сервера: надёжнее Dockerfile (сборка + nginx) — сборщик отдаст только исходники."})
    if build_method == "buildpacks":
        warnings += bp_warnings
    if any(n in root for n in ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml")):
        warnings.append({"code": "compose", "message": "Найден docker-compose: платформа выкатывает один сервис. Выберите Dockerfile нужного сервиса."})
    if info["private"] and not token:
        warnings.append({"code": "private_no_token", "message": "Репозиторий приватный: для сборки платформе понадобится токен доступа."})
    env_names: list[str] = []
    for base in dict.fromkeys([context, ""]):
        names = probe.ls(base)
        for fn in ENV_FILES:
            if fn in names and not names[fn]:
                env_names = parse_env_names(probe.cat(f"{base}/{fn}" if base else fn) or "")
                break
        if env_names:
            break
    return {"provider": ref.provider, "repo_full_name": ref.full_name, "git_url": ref.git_url, "private": info["private"],
            "default_branch": info["default_branch"], "branch": branch, "head_sha": head,
            "slug": slugify(ref.full_name.rsplit("/", 1)[-1]), "language": lang, "framework": framework,
            "build_method": build_method, "dockerfile_path": dockerfile_path, "sub_path": sub_path,
            "port": port, "port_source": port_source, "env_hints": env_names, "needs_services": detect_services(env_names), "warnings": warnings,
            "dockerfile_generated": bool(template), "dockerfile_template": template, "buildpacks_warnings": bp_warnings,
            "alternatives": alternatives}
