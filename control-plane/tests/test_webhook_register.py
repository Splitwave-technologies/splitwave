"""Автоматическое создание webhook: запросы к четырём провайдерам, идемпотентность, ошибки, SSRF, API и мастер."""
import json

import httpx
import pytest

from app.config import settings
from app.db.base import SessionLocal
from app.db.models import AuditEvent, Project
from app.services import provision, safeurl, scm
from app.services import webhook_register as hr
from app.services.repoinspect import parse_repo_url

_REAL = httpx.Client
URL = "https://dsp.example.com"


class Net:
    """Подмена сети: handler(request) -> Response; все запросы записываются."""

    def __init__(self, monkeypatch, handler):
        self.reqs = []

        def h(req):
            self.reqs.append(req)
            return handler(req)
        monkeypatch.setattr(hr.httpx, "Client", lambda **kw: _REAL(transport=httpx.MockTransport(h), **kw))
        monkeypatch.setattr(safeurl, "resolve", lambda host: ["93.184.216.34"])


def body(req):
    return json.loads(req.content or b"null")


def json_resp(data, status=200):
    return httpx.Response(status, json=data)


# ---------- GitHub ----------

def test_github_creates_a_hook_with_events_secret_and_bearer_token(monkeypatch):
    net = Net(monkeypatch, lambda r: json_resp([]) if r.method == "GET" else json_resp({"id": 1}, 201))
    out = hr.register("github", parse_repo_url("https://github.com/acme/shop"), "ghp_tok", URL, "/webhook/github", "s3cret")
    assert out == {"result": "created", "url": f"{URL}/webhook/github"}
    get, post = net.reqs
    assert str(get.url).startswith("https://api.github.com/repos/acme/shop/hooks") and get.headers["authorization"] == "Bearer ghp_tok"
    b = body(post)
    assert b["events"] == ["push", "pull_request"] and b["active"] is True and b["config"] == {"url": f"{URL}/webhook/github", "content_type": "json", "secret": "s3cret", "insecure_ssl": "0"}
    assert "ghp_tok" not in post.content.decode()                                          # токен только в заголовке


def test_github_updates_an_existing_hook_instead_of_duplicating(monkeypatch):
    existing = [{"id": 77, "config": {"url": f"{URL}/webhook/github"}}, {"id": 5, "config": {"url": "https://other"}}]
    net = Net(monkeypatch, lambda r: json_resp(existing) if r.method == "GET" else json_resp({"id": 77}))
    out = hr.register("github", parse_repo_url("acme/shop"), "t", URL, "/webhook/github", "new-secret")
    assert out["result"] == "updated"
    assert [r.method for r in net.reqs] == ["GET", "PATCH"] and str(net.reqs[1].url).endswith("/hooks/77") and body(net.reqs[1])["config"]["secret"] == "new-secret"


def test_http_platform_url_disables_ssl_verification_flag(monkeypatch):
    net = Net(monkeypatch, lambda r: json_resp([]) if r.method == "GET" else json_resp({}, 201))
    hr.register("github", parse_repo_url("acme/shop"), "t", "http://203.0.113.5:8080", "/webhook/github", "s")
    assert body(net.reqs[1])["config"]["insecure_ssl"] == "1"


# ---------- GitLab ----------

def test_gitlab_create_and_update_with_nested_group(monkeypatch):
    net = Net(monkeypatch, lambda r: json_resp([]) if r.method == "GET" else json_resp({"id": 1}, 201))
    ref = parse_repo_url("https://gitlab.com/grp/sub/shop")
    assert hr.register("gitlab", ref, "glpat", URL, "/webhook/gitlab/shop", "sec")["result"] == "created"
    get, post = net.reqs
    assert "/api/v4/projects/grp%2Fsub%2Fshop/hooks" in str(get.url) and get.headers["private-token"] == "glpat" and "authorization" not in get.headers
    b = body(post)
    assert b == {"url": f"{URL}/webhook/gitlab/shop", "token": "sec", "push_events": True, "merge_requests_events": True, "enable_ssl_verification": True}
    net2 = Net(monkeypatch, lambda r: json_resp([{"id": 9, "url": f"{URL}/webhook/gitlab/shop"}]) if r.method == "GET" else json_resp({}))
    assert hr.register("gitlab", ref, "glpat", URL, "/webhook/gitlab/shop", "sec")["result"] == "updated" and net2.reqs[1].method == "PUT"


# ---------- Bitbucket ----------

def test_bitbucket_create_and_update(monkeypatch):
    net = Net(monkeypatch, lambda r: json_resp({"values": []}) if r.method == "GET" else json_resp({}, 201))
    ref = parse_repo_url("https://bitbucket.org/acme/shop")
    assert hr.register("bitbucket", ref, "bb", URL, "/webhook/bitbucket/shop", "sec")["result"] == "created"
    b = body(net.reqs[1])
    assert b["secret"] == "sec" and "repo:push" in b["events"] and "pullrequest:fulfilled" in b["events"] and b["url"] == f"{URL}/webhook/bitbucket/shop"
    net2 = Net(monkeypatch, lambda r: json_resp({"values": [{"uuid": "{abc-1}", "url": f"{URL}/webhook/bitbucket/shop"}]}) if r.method == "GET" else json_resp({}))
    assert hr.register("bitbucket", ref, "bb", URL, "/webhook/bitbucket/shop", "sec")["result"] == "updated"
    assert net2.reqs[1].method == "PUT" and "%7Babc-1%7D" in str(net2.reqs[1].url)               # фигурные скобки uuid закодированы


# ---------- Gitea / Forgejo ----------

def test_gitea_self_hosted(monkeypatch):
    net = Net(monkeypatch, lambda r: json_resp([]) if r.method == "GET" else json_resp({}, 201))
    ref = parse_repo_url("https://git.mycorp.io/acme/shop", "gitea")
    assert hr.register("gitea", ref, "gt", URL, "/webhook/gitea/shop", "sec")["result"] == "created"
    assert str(net.reqs[0].url) == "https://git.mycorp.io/api/v1/repos/acme/shop/hooks" and net.reqs[0].headers["authorization"] == "token gt"
    assert body(net.reqs[1]) == {"type": "gitea", "active": True, "events": ["push", "pull_request"], "config": {"url": f"{URL}/webhook/gitea/shop", "content_type": "json", "secret": "sec"}}


# ---------- ошибки ----------

@pytest.mark.parametrize("status,needle", [(401, "нет права"), (403, "нет права"), (404, "не найден"), (500, "отказал (500)")])
@pytest.mark.parametrize("provider,url", [("github", "acme/x"), ("gitlab", "https://gitlab.com/g/x"), ("bitbucket", "https://bitbucket.org/a/x"), ("gitea", "https://git.mycorp.io/a/x")])
def test_provider_errors_are_explained(monkeypatch, provider, url, status, needle):
    Net(monkeypatch, lambda r: json_resp({"message": "boom"}, status))
    with pytest.raises(hr.RegisterError) as e:
        hr.register(provider, parse_repo_url(url, provider), "t", URL, "/p", "s")
    assert needle in str(e.value)
    if status in (401, 403):
        assert hr.RIGHTS[provider] in str(e.value)                                              # подсказка, какое право нужно у токена


def test_network_error_and_ssrf_and_missing_secret_and_bad_urls(monkeypatch):
    def boom(req):
        raise httpx.ConnectError("down")
    Net(monkeypatch, boom)
    with pytest.raises(hr.RegisterError, match="не отвечает"):
        hr.register("github", parse_repo_url("acme/x"), "t", URL, "/p", "s")
    monkeypatch.setattr(safeurl, "resolve", lambda host: ["10.0.0.5"])
    with pytest.raises(hr.RegisterError, match="недоступен"):
        hr.register("gitea", parse_repo_url("https://git.internal/acme/x", "gitea"), "t", URL, "/p", "s")
    with pytest.raises(hr.RegisterError, match="секрет"):
        hr.register("github", parse_repo_url("acme/x"), "t", URL, "/p", "")
    for bad in ("", "ftp://x.io", "https://user:pw@x.io", "https://x.io/path?q=1", "not a url"):
        monkeypatch.setattr(settings, "public_url", "")
        with pytest.raises(hr.RegisterError, match="внешний адрес"):
            hr.register("github", parse_repo_url("acme/x"), "t", bad, "/p", "s")


def test_public_url_defaults_to_the_setting_and_warns_when_unreachable(monkeypatch):
    Net(monkeypatch, lambda r: json_resp([]) if r.method == "GET" else json_resp({}, 201))
    monkeypatch.setattr(settings, "public_url", "https://dsp.corp.com/")
    assert hr.register("github", parse_repo_url("acme/x"), "t", None, "/p", "s")["url"] == "https://dsp.corp.com/p"
    for local in ("http://localhost:8080", "http://127.0.0.1", "http://192.168.1.5", "https://dsp.local", "http://10.0.0.7:8295"):
        out = hr.register("github", parse_repo_url("acme/x"), "t", local, "/p", "s")
        assert "warning" in out, local
    assert "warning" not in hr.register("github", parse_repo_url("acme/x"), "t", "https://dsp.example.com", "/p", "s")


# ---------- API ----------

@pytest.fixture
def proj(client, admin):
    r = client.post("/api/projects", json={"slug": "shop", "repo_full_name": "acme/shop", "provider": "gitea", "git_url": "https://git.mycorp.io/acme/shop.git",
                                           "environments": [{"name": "prod", "namespace": "apps", "deployment_name": "shop", "container_name": "shop"}]}, headers=admin)
    assert r.status_code == 201
    return "shop"


def test_endpoint_registers_with_the_project_secret_and_never_stores_the_token(client, admin, proj, monkeypatch):
    seen = {}

    def fake(provider, ref, token, public_url, path, secret):
        seen.update(provider=provider, base=ref.base, full=ref.full_name, token=token, url=public_url, path=path, secret=secret)
        return {"result": "created", "url": public_url + path}
    monkeypatch.setattr(hr, "register", fake)
    r = client.post("/api/onboarding/shop/webhook", json={"token": "gt-SECRET-TOKEN", "public_url": URL}, headers=admin)
    assert r.status_code == 200 and r.json() == {"ok": True, "result": "created", "url": f"{URL}/webhook/gitea/shop"}
    assert seen["provider"] == "gitea" and seen["base"] == "https://git.mycorp.io" and seen["full"] == "acme/shop" and seen["secret"] == scm.webhook_secret("shop")
    assert "gt-SECRET-TOKEN" not in r.text
    db = SessionLocal()
    ev = [e for e in db.query(AuditEvent).all() if e.action == "webhook_registered"]
    assert ev and "gt-SECRET-TOKEN" not in json.dumps([e.detail for e in db.query(AuditEvent).all()], default=str)
    p = db.query(Project).one(); assert "gt-SECRET-TOKEN" not in str(p.git_token_enc) and p.git_token_enc is None
    db.close()


def test_github_projects_use_the_shared_platform_secret(client, admin, monkeypatch):
    client.post("/api/projects", json={"slug": "gh", "repo_full_name": "acme/gh", "environments": [{"name": "prod", "namespace": "a", "deployment_name": "gh", "container_name": "gh"}]}, headers=admin)
    seen = {}
    monkeypatch.setattr(hr, "register", lambda provider, ref, token, url, path, secret: seen.update(secret=secret, path=path) or {"result": "created", "url": "u"})
    client.post("/api/onboarding/gh/webhook", json={"token": "t", "public_url": URL}, headers=admin)
    assert seen == {"secret": settings.github_webhook_secret, "path": "/webhook/github"}


def test_endpoint_reports_failures_without_crashing_and_checks_permissions(client, admin, proj, monkeypatch, make_token):
    def fail(*a, **k):
        raise hr.RegisterError("Токен не принят")
    monkeypatch.setattr(hr, "register", fail)
    r = client.post("/api/onboarding/shop/webhook", json={"token": "t", "public_url": URL}, headers=admin)
    assert r.status_code == 200 and r.json() == {"ok": False, "error": "Токен не принят"}
    monkeypatch.setattr(hr, "register", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret internals")))
    r = client.post("/api/onboarding/shop/webhook", json={"token": "t", "public_url": URL}, headers=admin)
    assert r.json()["ok"] is False and "internals" not in r.text                               # внутренности не раскрываем
    assert client.post("/api/onboarding/shop/webhook", json={"token": "t"}, headers=make_token("developer")).status_code == 403
    assert client.post("/api/onboarding/nope/webhook", json={"token": "t"}, headers=admin).status_code == 404
    assert client.post("/api/onboarding/shop/webhook", json={"token": ""}, headers=admin).status_code == 422


# ---------- мастер ----------

def test_wizard_registers_the_hook_and_a_failure_does_not_undo_the_project(client, admin, monkeypatch):
    from app.routers import onboarding
    monkeypatch.setattr(provision, "apply", lambda docs, cluster=None: [{"kind": d["kind"], "name": d["metadata"]["name"], "result": "created"} for d in docs])
    monkeypatch.setattr(onboarding.deps, "schedule_redeploy", lambda *a, **k: {"accepted": True})
    monkeypatch.setattr(hr, "register", lambda *a, **k: {"result": "created", "url": "https://dsp.example.com/webhook/github"})
    b = {"slug": "w1", "repo_full_name": "acme/w1", "build_method": "dockerfile", "port": 8080, "webhook_token": "tok", "public_url": URL}
    r = client.post("/api/onboarding/create", json=b, headers=admin)
    assert r.status_code == 201 and r.json()["webhook_registered"] == {"ok": True, "result": "created", "url": "https://dsp.example.com/webhook/github"}
    assert "tok" not in json.dumps(r.json()["webhook"]) and "webhook_token" not in r.text

    def fail(*a, **k):
        raise hr.RegisterError("нет права")
    monkeypatch.setattr(hr, "register", fail)
    r = client.post("/api/onboarding/create", json=dict(b, slug="w2", repo_full_name="acme/w2"), headers=admin)
    assert r.status_code == 201 and r.json()["webhook_registered"] == {"ok": False, "error": "нет права"}
    assert SessionLocal().query(Project).count() == 2
    r = client.post("/api/onboarding/create", json=dict(b, slug="w3", repo_full_name="acme/w3", webhook_token=None), headers=admin)
    assert r.status_code == 201 and "webhook_registered" not in r.json()                       # без токена — как раньше (инструкция)
