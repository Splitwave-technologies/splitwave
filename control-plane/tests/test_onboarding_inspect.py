"""Мастер подключения: разбор адреса, клиенты провайдеров, определение стека, защита от SSRF."""
import json

import httpx
import pytest

from app.services import repoinspect as ri
from app.services.repoinspect import InspectError, parse_repo_url


# ---------- адрес репозитория ----------

@pytest.mark.parametrize("raw,provider,full,git_url,ref,path", [
    ("https://github.com/acme/shop", "github", "acme/shop", None, None, ""),
    ("https://github.com/acme/shop.git", "github", "acme/shop", None, None, ""),
    ("github.com/acme/shop/", "github", "acme/shop", None, None, ""),
    ("acme/shop", "github", "acme/shop", None, None, ""),
    ("git@github.com:acme/shop.git", "github", "acme/shop", None, None, ""),
    ("https://github.com/acme/shop/tree/dev/services/api", "github", "acme/shop", None, "dev", "services/api"),
    ("https://gitlab.com/grp/sub/shop", "gitlab", "grp/sub/shop", None, None, ""),
    ("https://gitlab.com/grp/sub/shop/-/tree/main/backend", "gitlab", "grp/sub/shop", None, "main", "backend"),
    ("https://gitlab.example.org/team/shop.git", "gitlab", "team/shop", "https://gitlab.example.org/team/shop.git", None, ""),
    ("https://bitbucket.org/acme/shop", "bitbucket", "acme/shop", None, None, ""),
    ("https://codeberg.org/acme/shop", "gitea", "acme/shop", "https://codeberg.org/acme/shop.git", None, ""),
    ("https://forgejo.mycorp.io/acme/shop/src/branch/dev/api", "gitea", "acme/shop", "https://forgejo.mycorp.io/acme/shop.git", "dev", "api"),
])
def test_parse_repo_url(raw, provider, full, git_url, ref, path):
    r = parse_repo_url(raw)
    assert (r.provider, r.full_name, r.git_url, r.ref_hint, r.path_hint) == (provider, full, git_url, ref, path)


@pytest.mark.parametrize("raw,hint", [
    ("", None), ("https://github.com/onlyowner", None), ("https://user:tok@github.com/a/b", None),
    ("http://github.com/a/b", None), ("https://git.mycorp.io/a/b", None), ("https://github.example.com/a/b", "github"),
    ("https://bitbucket.mycorp.io/a/b", "bitbucket"), ("https://github.com/a/../b", None), ("https://github.com/a/b c", None),
])
def test_parse_repo_url_rejects(raw, hint):
    with pytest.raises(InspectError):
        parse_repo_url(raw, hint)


def test_provider_hint_for_unknown_host():
    r = parse_repo_url("https://git.mycorp.io/a/b", "gitea")
    assert r.provider == "gitea" and r.git_url == "https://git.mycorp.io/a/b.git"
    with pytest.raises(InspectError):
        parse_repo_url("https://git.mycorp.io/a/b", "svn")


def test_slugify():
    assert ri.slugify("My_Shop.API") == "my-shop-api"
    assert ri.slugify("___") == "app"
    assert len(ri.slugify("a" * 80)) <= 40


def test_parse_expose_and_env():
    assert ri.parse_expose("FROM x\nEXPOSE 3000/tcp 9229\n") == 3000
    assert ri.parse_expose("FROM x\nexpose 8080\n") == 8080
    assert ri.parse_expose("FROM x\nEXPOSE ${PORT}\n") is None
    assert ri.parse_env_names("# c\nA=1\nexport B=2\nA=again\n  C = 3\nnot env\n") == ["A", "B", "C"]


# ---------- клиенты провайдеров (HTTP подменён) ----------

class Recorder:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def __call__(self, url, headers, limit=ri.MAX_JSON):
        self.calls.append((url, dict(headers), limit))
        for part, (status, body) in self.replies.items():
            if part in url:
                return status, (body if isinstance(body, bytes) else json.dumps(body).encode())
        return 404, b"{}"


def make(provider, token=None, url=None):
    ref = parse_repo_url(url or {"github": "https://github.com/acme/shop", "gitlab": "https://gitlab.com/grp/shop",
                                 "bitbucket": "https://bitbucket.org/acme/shop", "gitea": "https://git.x.io/acme/shop"}[provider], provider)
    return ri.CLIENTS[provider](ref, token)


def test_github_client(monkeypatch):
    rec = Recorder({"/commits/main": (200, {"sha": "a" * 40}), "/contents/?": (200, [{"name": "src", "type": "dir"}, {"name": "go.mod", "type": "file"}]),
                    "/contents/go.mod": (200, b"module x"), "/repos/acme/shop": (200, {"default_branch": "trunk", "private": True})})
    monkeypatch.setattr(ri, "fetch", rec)
    c = make("github", "tok")
    assert c.repo_info() == {"default_branch": "trunk", "private": True}
    assert c.head_sha("main") == "a" * 40
    assert c.list_dir("", "main") == [("src", True), ("go.mod", False)]
    assert c.read_file("go.mod", "main") == "module x"
    assert all(h["Authorization"] == "Bearer tok" for _, h, _ in rec.calls)
    assert rec.calls[-1][1]["Accept"] == "application/vnd.github.raw+json" and rec.calls[-1][2] == ri.MAX_FILE


def test_gitlab_client(monkeypatch):
    rec = Recorder({"/repository/branches/main": (200, {"commit": {"id": "b" * 40}}),
                    "/repository/tree": (200, [{"name": "app", "type": "tree"}, {"name": "Dockerfile", "type": "blob"}]),
                    "/repository/files/": (200, b"FROM x"), "/projects/grp%2Fshop": (200, {"default_branch": "main", "visibility": "private"})})
    monkeypatch.setattr(ri, "fetch", rec)
    c = make("gitlab", "glpat")
    assert c.repo_info() == {"default_branch": "main", "private": True}
    assert c.head_sha("main") == "b" * 40
    assert c.list_dir("", "main") == [("app", True), ("Dockerfile", False)]
    assert c.read_file("deploy/Dockerfile", "main") == "FROM x"
    assert any("files/deploy%2FDockerfile/raw" in u for u, _, _ in rec.calls)
    assert all(h["PRIVATE-TOKEN"] == "glpat" and "Authorization" not in h for _, h, _ in rec.calls)


def test_bitbucket_client(monkeypatch):
    rec = Recorder({"/refs/branches/main": (200, {"target": {"hash": "c" * 40}}),
                    "/src/main/?": (200, {"values": [{"path": "api", "type": "commit_directory"}, {"path": "Dockerfile", "type": "commit_file"}]}),
                    "/src/main/Dockerfile": (200, b"FROM x"), "/repositories/acme/shop": (200, {"mainbranch": {"name": "master"}, "is_private": False})})
    monkeypatch.setattr(ri, "fetch", rec)
    c = make("bitbucket", "t")
    assert c.repo_info() == {"default_branch": "master", "private": False}
    assert c.head_sha("main") == "c" * 40
    assert c.list_dir("", "main") == [("api", True), ("Dockerfile", False)]
    assert c.read_file("Dockerfile", "main") == "FROM x"


def test_gitea_client(monkeypatch):
    rec = Recorder({"/branches/main": (200, {"commit": {"id": "d" * 40}}), "/contents/?": (200, [{"name": "x", "type": "dir"}]),
                    "/raw/Dockerfile": (200, b"FROM x"), "/repos/acme/shop": (200, {"default_branch": "main", "private": True})})
    monkeypatch.setattr(ri, "fetch", rec)
    c = make("gitea", "gt")
    assert c.repo_info()["private"] is True and c.head_sha("main") == "d" * 40
    assert c.list_dir("", "main") == [("x", True)] and c.read_file("Dockerfile", "main") == "FROM x"
    assert all(h["Authorization"] == "token gt" for _, h, _ in rec.calls)
    assert rec.calls[0][0].startswith("https://git.x.io/api/v1/repos/acme/shop")


@pytest.mark.parametrize("status,token,needle", [
    (404, None, "токен"), (404, "t", "нет доступа"), (401, "t", "недействителен"), (403, None, "лимит"), (429, None, "ограничил"),
    (301, None, "перенесён"), (500, None, "ошибкой 500"),
])
def test_provider_errors_are_explained(monkeypatch, status, token, needle):
    monkeypatch.setattr(ri, "fetch", lambda *a, **k: (status, b"{}"))
    with pytest.raises(InspectError) as e:
        make("github", token).repo_info()
    assert needle in e.value.message.lower()


def test_missing_file_or_dir_is_not_an_error(monkeypatch):
    monkeypatch.setattr(ri, "fetch", lambda *a, **k: (404, b"{}"))
    c = make("github")
    assert c.list_dir("nope", "main") == [] and c.read_file("nope", "main") is None


def test_call_budget(monkeypatch):
    monkeypatch.setattr(ri, "fetch", lambda *a, **k: (200, b"[]"))
    c = make("github")
    with pytest.raises(InspectError):
        for _ in range(ri.CALL_BUDGET + 1):
            c.list_dir("x", "main")


# ---------- SSRF и ограничения сети ----------

def test_fetch_blocks_private_hosts(monkeypatch):
    from app.services import safeurl
    monkeypatch.setattr(safeurl, "resolve", lambda host: ["10.0.0.5"])
    with pytest.raises(InspectError) as e:
        ri.fetch("https://git.internal.corp/api/v1/repos/a/b", {})
    assert "недоступен" in e.value.message
    monkeypatch.setattr(safeurl, "resolve", lambda host: ["169.254.169.254"])
    with pytest.raises(InspectError):
        ri.fetch("https://metadata.example/x", {})


def test_fetch_allows_private_when_configured(monkeypatch):
    from app.config import settings
    from app.services import safeurl
    monkeypatch.setattr(safeurl, "resolve", lambda host: ["10.0.0.5"])
    monkeypatch.setattr(settings, "scm_allow_private_hosts", True)
    _mock_http(monkeypatch, lambda req: httpx.Response(200, content=b"ok"))
    assert ri.fetch("https://git.internal.corp/x", {}) == (200, b"ok")


_REAL_CLIENT = httpx.Client


def _mock_http(monkeypatch, handler):
    monkeypatch.setattr(ri.httpx, "Client", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


def test_fetch_does_not_follow_redirects_and_caps_size(monkeypatch):
    from app.services import safeurl
    monkeypatch.setattr(safeurl, "resolve", lambda host: ["93.184.216.34"])
    _mock_http(monkeypatch, lambda req: httpx.Response(302, headers={"location": "http://169.254.169.254/"}))
    assert ri.fetch("https://github.com/x", {})[0] == 302            # редирект не выполняется
    _mock_http(monkeypatch, lambda req: httpx.Response(200, content=b"A" * 500_000))
    status, body = ri.fetch("https://github.com/x", {}, limit=1000)
    assert status == 200 and len(body) == 1000


def test_fetch_network_error_is_reported(monkeypatch):
    from app.services import safeurl
    monkeypatch.setattr(safeurl, "resolve", lambda host: ["93.184.216.34"])

    def boom(req):
        raise httpx.ConnectError("down")
    _mock_http(monkeypatch, boom)
    with pytest.raises(InspectError) as e:
        ri.fetch("https://github.com/x", {})
    assert e.value.status == 502


# ---------- определение стека по содержимому репозитория ----------

class FakeRepo:
    """Репозиторий в памяти: путь -> текст файла; интерфейс как у клиентов провайдеров."""

    def __init__(self, files, private=False, branch="main", head="e" * 40):
        self.files, self.private, self.branch, self.head = files, private, branch, head

    def __call__(self, ref, token):
        return self

    def repo_info(self):
        return {"default_branch": self.branch, "private": self.private}

    def head_sha(self, branch):
        return self.head if branch == self.branch else ""

    def list_dir(self, path, ref):
        prefix = path + "/" if path else ""
        out = {}
        for p in self.files:
            if p.startswith(prefix):
                rest = p[len(prefix):]
                out[rest.split("/")[0]] = "/" in rest
        return list(out.items())

    def read_file(self, path, ref):
        return self.files.get(path)


def run(files, **kw):
    return ri.inspect_repo("https://github.com/acme/My_Shop", client_factory=FakeRepo(files, **kw))


def codes(p):
    return {w["code"] for w in p["warnings"]}


def test_node_express_gets_a_generated_dockerfile():
    p = run({"package.json": json.dumps({"dependencies": {"express": "4"}, "scripts": {"start": "node ."}}), "index.js": "x", ".env.example": "DB_URL=x\nAPI_KEY=secret-value\n"})
    assert (p["language"], p["framework"], p["build_method"], p["dockerfile_generated"]) == ("node", "Express", "dockerfile", True)
    t = p["dockerfile_template"]
    assert "FROM node:20-alpine" in t["content"] and 'CMD ["sh", "-c", "npm start"]' in t["content"] and "USER node" in t["content"]
    assert p["slug"] == "my-shop" and p["port"] == 3000 and p["port_source"] == "template" and t["params"]["port"] == 3000
    assert p["env_hints"] == ["DB_URL", "API_KEY"] and "secret-value" not in json.dumps(p)
    assert p["head_sha"] == "e" * 40 and p["branch"] == "main" and p["git_url"] is None and codes(p) == set() and p["buildpacks_warnings"] == []


def test_node_without_start_script_is_noted_for_both_methods():
    p = run({"package.json": json.dumps({"dependencies": {"express": "4"}, "main": "server.js"})})
    assert p["dockerfile_generated"] and any("start" in n for n in p["dockerfile_template"]["notes"])
    assert 'CMD ["sh", "-c", "node server.js"]' in p["dockerfile_template"]["content"]
    assert [w["code"] for w in p["buildpacks_warnings"]] == ["no_start_script"]


def test_frontend_only_without_server_becomes_a_static_nginx_build():
    p = run({"package.json": json.dumps({"dependencies": {"react": "18", "react-scripts": "5"}, "scripts": {"build": "x", "start": "x"}}), "package-lock.json": "{}"})
    c = p["dockerfile_template"]["content"]
    assert p["framework"] == "React" and p["port"] == 8080 and "nginx-unprivileged" in c and "COPY --from=build /app/build" in c and "npm ci" in c
    assert "frontend_only" in {w["code"] for w in p["buildpacks_warnings"]}


def test_dockerfile_in_root_with_expose():
    p = run({"Dockerfile": "FROM python:3.12\nEXPOSE 9000\n", "requirements.txt": "fastapi\n", "main.py": "x"})
    assert (p["build_method"], p["dockerfile_path"], p["sub_path"], p["port"], p["port_source"]) == ("dockerfile", "Dockerfile", "", 9000, "EXPOSE")
    assert p["language"] == "python" and p["framework"] == "FastAPI"


def test_dockerfile_without_expose_uses_language_default():
    p = run({"Dockerfile": "FROM golang\n", "go.mod": "module x\nrequire github.com/gin-gonic/gin v1\n"})
    assert p["port"] == 8080 and p["port_source"] == "default" and p["framework"] == "Gin"


def test_dockerfile_in_infra_dir_uses_repo_root_as_context():
    p = run({"docker/Dockerfile": "FROM x\nEXPOSE 3000\n", "package.json": "{}", "src/a.js": "x"})
    assert (p["build_method"], p["sub_path"], p["dockerfile_path"], p["port"]) == ("dockerfile", "", "docker/Dockerfile", 3000)


def test_dockerfile_in_app_subdir_becomes_context():
    p = run({"backend/Dockerfile": "FROM x\nEXPOSE 5000\n", "backend/requirements.txt": "flask", "README.md": "x"})
    assert (p["sub_path"], p["dockerfile_path"], p["port"], p["language"], p["framework"]) == ("backend", "Dockerfile", 5000, "python", "Flask")


def test_many_dockerfiles_and_compose_warn():
    p = run({"Dockerfile": "FROM x", "Dockerfile.prod": "FROM x", "docker-compose.yml": "services: {}", "package.json": "{}"})
    assert {"many_dockerfiles", "compose"} <= codes(p)
    assert p["dockerfile_path"] == "Dockerfile" and {"dockerfile_path": "Dockerfile.prod", "sub_path": ""} in p["alternatives"]["dockerfiles"]


def test_monorepo_picks_first_app_dir():
    p = run({"backend/package.json": "{}", "frontend/package.json": "{}", "README.md": "x"})
    assert "monorepo" in codes(p) and p["sub_path"] == "backend" and p["build_method"] == "dockerfile" and p["dockerfile_generated"]
    assert {d["sub_path"] for d in p["alternatives"]["subdirs"]} == {"backend", "frontend"}


def test_url_path_hint_selects_subfolder():
    files = {"api/Dockerfile": "FROM x\nEXPOSE 1111", "web/Dockerfile": "FROM x\nEXPOSE 2222"}
    p = ri.inspect_repo("https://github.com/acme/shop/tree/main/web", client_factory=FakeRepo(files))
    assert p["sub_path"] == "web" and p["port"] == 2222


def test_python_unknown_stack_unsupported_language_and_private():
    p = run({"requirements.txt": "django"})
    assert p["dockerfile_generated"] and [w["code"] for w in p["buildpacks_warnings"]] == ["python_no_procfile"]
    p = run({"README.md": "hi"})
    assert p["language"] is None and "unknown_stack" in codes(p) and not p["dockerfile_generated"] and p["dockerfile_template"] is None
    p = run({"Cargo.toml": ""})
    assert "no_buildpack" in codes(p) and not p["dockerfile_generated"]                  # для Rust шаблона нет
    assert "private_no_token" in codes(run({"go.mod": "module x"}, private=True))


def test_repo_dockerfile_always_wins_over_a_generated_one():
    p = run({"Dockerfile": "FROM node\nEXPOSE 7000\n", "package.json": "{}"})
    assert p["build_method"] == "dockerfile" and p["dockerfile_template"] is None and not p["dockerfile_generated"] and p["port"] == 7000


def test_facts_for_each_stack():
    py = run({"requirements.txt": "fastapi\nuvicorn\n", "main.py": "x"})["dockerfile_template"]
    assert py["facts"]["start_command"] == "uvicorn main:app --host 0.0.0.0 --port $PORT" and py["facts"]["extra_packages"] == []
    fl = run({"requirements.txt": "flask", "app.py": "x"})["dockerfile_template"]
    assert fl["facts"]["start_command"] == "gunicorn -b 0.0.0.0:$PORT app:app" and fl["facts"]["extra_packages"] == ["gunicorn"]
    pf = run({"requirements.txt": "x", "Procfile": "web: python serve.py --port $PORT\nworker: x\n"})["dockerfile_template"]
    assert pf["facts"]["start_command"] == "python serve.py --port $PORT" and "start_guess" not in pf["facts"]
    dj = run({"requirements.txt": "django", "manage.py": "x", "mysite/wsgi.py": "x", "mysite/settings.py": "x"})["dockerfile_template"]
    assert dj["facts"]["start_command"] == "gunicorn -b 0.0.0.0:$PORT mysite.wsgi"
    go = run({"go.mod": "module x\n\ngo 1.23.1\n", "cmd/api/main.go": "x"})["dockerfile_template"]
    assert go["facts"] == {"runtime_version": "1.23", "main_package": "./cmd/api"} and "FROM golang:1.23-alpine" in go["content"]
    assert run({"go.mod": "module x\n", "main.go": "x"})["dockerfile_template"]["facts"]["main_package"] == "."
    assert run({"go.mod": "module x\n", "cmd/a/main.go": "x", "cmd/b/main.go": "x"})["dockerfile_template"]["facts"]["main_unknown"] is True
    mv = run({"pom.xml": "<project><properties><java.version>17</java.version></properties></project>"})["dockerfile_template"]
    assert mv["facts"] == {"build_tool": "maven", "runtime_version": "17"}
    nd = run({"package.json": json.dumps({"engines": {"node": ">=18.0.0"}, "scripts": {"start": "x"}}), "yarn.lock": ""})["dockerfile_template"]
    assert nd["facts"]["package_manager"] == "yarn" and nd["facts"]["runtime_version"] == "18" and "FROM node:18-alpine" in nd["content"]
    st = run({"index.html": "<html>"})["dockerfile_template"]
    assert st["language"] == "static" and "nginx-unprivileged" in st["content"]



def test_empty_repo_and_missing_branch():
    with pytest.raises(InspectError):
        run({})
    with pytest.raises(InspectError):
        ri.inspect_repo("https://github.com/acme/shop", branch="nope", client_factory=FakeRepo({"go.mod": "x"}))


def test_self_hosted_gitea_gets_explicit_git_url():
    p = ri.inspect_repo("https://git.mycorp.io/acme/shop", provider_hint="gitea", client_factory=FakeRepo({"go.mod": "x"}))
    assert p["provider"] == "gitea" and p["git_url"] == "https://git.mycorp.io/acme/shop.git"


def test_dockerfile_one_level_deeper_in_app_dirs():
    """Регрессия: paulbouwer/hello-kubernetes держит Dockerfile в src/app/ — раньше «язык не определён»."""
    p = run({"src/app/Dockerfile": "FROM node\nEXPOSE 8081\n", "src/app/package.json": "{}", "src/app/server.js": "x", "README.md": "x"})
    assert (p["build_method"], p["sub_path"], p["dockerfile_path"], p["port"], p["language"]) == ("dockerfile", "src/app", "Dockerfile", 8081, "node")
    p = run({"services/api/Dockerfile": "FROM python\nEXPOSE 5000\n", "services/web/package.json": "{}"})
    assert (p["sub_path"], p["port"]) == ("services/api", 5000)
    assert run({"docs/guide/Dockerfile": "FROM x", "README.md": "x"})["build_method"] == "buildpacks"     # не app-каталог — не ищем


def test_platform_token_is_used_only_for_default_public_hosts(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "scm_github_token", "ghp_platform")
    monkeypatch.setattr(settings, "scm_gitlab_token", "glpat_platform")
    pt = ri.platform_token
    assert pt(parse_repo_url("https://github.com/a/b")) == "ghp_platform"
    assert pt(parse_repo_url("https://gitlab.com/g/s/b")) == "glpat_platform"
    assert pt(parse_repo_url("https://gitlab.mycorp.io/a/b")) is None            # чужой сервер: токен платформы не отправляем
    assert pt(parse_repo_url("https://git.mycorp.io/a/b", "gitea")) is None
    assert pt(parse_repo_url("https://bitbucket.org/a/b")) is None
    monkeypatch.setattr(settings, "scm_github_token", "")
    assert pt(parse_repo_url("https://github.com/a/b")) is None


def test_inspect_passes_platform_token_but_user_token_wins(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "scm_github_token", "ghp_platform")
    seen = []
    repo = FakeRepo({"go.mod": "x"})
    ri.inspect_repo("https://github.com/a/b", client_factory=lambda ref, tok: (seen.append(tok), repo)[1])
    ri.inspect_repo("https://github.com/a/b", token="ghp_user", client_factory=lambda ref, tok: (seen.append(tok), repo)[1])
    assert seen == ["ghp_platform", "ghp_user"]


@pytest.mark.parametrize("location,expected", [
    (b"https://gitlab.com/api/v4/projects/new-group%2Fshop", "gitlab.com/new-group/shop"),
    (b"https://git.mycorp.io/api/v1/repos/acme/shop-renamed", "git.mycorp.io/acme/shop-renamed"),
    (b"https://api.github.com/repositories/123456", ""),                                   # у GitHub только числовой id — подсказки нет
    (b"javascript:alert(1)", ""), (b"https://u:p@evil.io/a/b", ""), (b"", ""), (b"\xff\xfe", ""), (b"https://x.io/a/<b>", ""),
])
def test_moved_repository_hint_is_safe_and_readable(location, expected):
    from app.services import repoinspect as ri
    hint = ri._moved_hint(location)
    assert hint == (f": {expected}" if expected else "")


def test_fetch_returns_the_location_of_a_redirect(monkeypatch):
    from app.services import repoinspect as ri
    monkeypatch.setattr(ri.safeurl, "resolve", lambda host: ["93.184.216.34"])
    real = httpx.Client
    monkeypatch.setattr(ri.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(301, headers={"location": "https://gitlab.com/api/v4/projects/a%2Fb"}, content=b"x" * 5000)), **kw))
    assert ri.fetch("https://gitlab.com/api/v4/projects/old", {}) == (301, b"https://gitlab.com/api/v4/projects/a%2Fb")
    with pytest.raises(ri.InspectError, match=r"gitlab\.com/a/b"):
        ri.GitLab(ri.parse_repo_url("https://gitlab.com/old/x"), None).call("https://gitlab.com/api/v4/projects/old")


def test_application_in_an_arbitrarily_named_folder_is_found():
    p = run({"billing-service/App.csproj": "<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup></Project>", "README.md": "x"})
    assert p["dockerfile_template"]["params"]["runtime_version"] == "10.0"          # версия среды берётся из проекта
    assert p["language"] == "dotnet" and p["sub_path"] == "billing-service" and p["dockerfile_generated"] and "monorepo" in codes(p)
    assert "ENTRYPOINT [\"dotnet\", \"App.dll\"]" in p["dockerfile_template"]["content"]
    q = run({"docs/package.json": "{}", "api2/go.mod": "module x\ngo 1.22\n", "api2/main.go": "package main", "README.md": "x"})
    assert q["language"] == "go" and q["sub_path"] == "api2"                       # docs пропускается, приложение найдено
