"""Вебхуки GitLab / Bitbucket / Gitea и общий разбор событий."""
import hashlib
import hmac
import json

import pytest

from app import plugins
from app.db.base import SessionLocal
from app.db.models import Project
from app.routers import webhooks
from app.services import scm

ENV = {"name": "prod", "namespace": "shop", "deployment_name": "shop", "container_name": "shop", "branch": "main"}


@pytest.fixture
def calls(monkeypatch):
    out = []
    monkeypatch.setattr(webhooks, "run_build_and_deploy", lambda *a, **k: out.append(a))
    return out


def mk(client, admin, provider, repo, **extra):
    body = {"slug": "shop", "repo_full_name": repo, "provider": provider, "environments": [ENV], **extra}
    return client.post("/api/projects", json=body, headers=admin)


def sig_hdr(provider, secret, raw):
    mac = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return {"gitlab": {"X-Gitlab-Token": secret}, "bitbucket": {"X-Hub-Signature": "sha256=" + mac},
            "gitea": {"X-Gitea-Signature": mac}}[provider]


def send(client, provider, payload, extra_headers, slug="shop", secret=None, raw=None):
    raw = raw or json.dumps(payload).encode()
    h = {**extra_headers, **sig_hdr(provider, secret if secret is not None else scm.webhook_secret(slug), raw), "Content-Type": "application/json"}
    return client.post(f"/webhook/{provider}/{slug}", content=raw, headers=h)


GITLAB_PUSH = {"object_kind": "push", "ref": "refs/heads/main", "after": "a" * 40, "checkout_sha": "a" * 40,
               "project": {"path_with_namespace": "acme/team/shop", "git_http_url": "https://gitlab.example.com/acme/team/shop.git"}}
BB_PUSH = {"repository": {"full_name": "acme/shop"}, "push": {"changes": [
    {"new": {"type": "branch", "name": "main", "target": {"hash": "b" * 40}}}, {"new": {"type": "tag", "name": "v1", "target": {"hash": "c" * 40}}}]}}
GITEA_PUSH = {"ref": "refs/heads/main", "after": "d" * 40, "repository": {"full_name": "acme/shop", "clone_url": "https://git.example.com/acme/shop.git"}}


def test_gitlab_push_with_nested_group_deploys(client, admin, calls):
    assert mk(client, admin, "gitlab", "acme/team/shop", git_url="https://gitlab.example.com/acme/team/shop.git").status_code == 201
    r = send(client, "gitlab", GITLAB_PUSH, {"X-Gitlab-Event": "Push Hook"})
    assert r.status_code == 200 and r.json()["accepted"] and r.json()["environments"] == ["prod"]
    assert calls[0][2] == "https://gitlab.example.com/acme/team/shop.git" and calls[0][3] == "a" * 40


def test_bitbucket_push_only_branches_and_gitea_push(client, admin, calls):
    mk(client, admin, "bitbucket", "acme/shop")
    r = send(client, "bitbucket", BB_PUSH, {"X-Event-Key": "repo:push"})
    assert r.json()["accepted"] and len(calls) == 1 and calls[0][3] == "b" * 40 and calls[0][2] == "https://bitbucket.org/acme/shop.git"
    client.delete("/api/projects/shop", headers=admin)
    mk(client, admin, "gitea", "acme/shop", git_url="https://git.example.com/acme/shop.git")
    r = send(client, "gitea", GITEA_PUSH, {"X-Gitea-Event": "push"})
    assert r.json()["accepted"] and calls[-1][3] == "d" * 40


@pytest.mark.parametrize("provider,payload,hdr", [("gitlab", GITLAB_PUSH, {"X-Gitlab-Event": "Push Hook"}),
                                                 ("bitbucket", BB_PUSH, {"X-Event-Key": "repo:push"}), ("gitea", GITEA_PUSH, {"X-Gitea-Event": "push"})])
def test_bad_or_foreign_signature_is_rejected(client, admin, calls, provider, payload, hdr):
    repo = {"gitlab": "acme/team/shop", "bitbucket": "acme/shop", "gitea": "acme/shop"}[provider]
    mk(client, admin, provider, repo, **({"git_url": "https://x.example.com/a/b.git"} if provider == "gitea" else {}))
    assert send(client, provider, payload, hdr, secret="wrong").status_code == 401
    assert send(client, provider, payload, hdr, secret=scm.webhook_secret("other-project")).status_code == 401     # секрет другого проекта не подходит
    assert calls == []


def test_secrets_are_per_project_and_deterministic():
    assert scm.webhook_secret("a") != scm.webhook_secret("b") and scm.webhook_secret("a") == scm.webhook_secret("a") and len(scm.webhook_secret("a")) == 40


def test_event_for_a_different_repository_is_ignored(client, admin, calls):
    mk(client, admin, "gitlab", "acme/team/shop")
    other = {**GITLAB_PUSH, "project": {"path_with_namespace": "evil/repo", "git_http_url": "https://x"}}
    r = send(client, "gitlab", other, {"X-Gitlab-Event": "Push Hook"})
    assert r.json()["skipped"] is True and calls == []


def test_branch_delete_and_non_push_events_are_ignored(client, admin, calls):
    mk(client, admin, "gitlab", "acme/team/shop")
    gone = {**GITLAB_PUSH, "after": "0" * 40, "checkout_sha": None}
    assert send(client, "gitlab", gone, {"X-Gitlab-Event": "Push Hook"}).json()["skipped"] is True
    assert send(client, "gitlab", {"object_kind": "issue"}, {"X-Gitlab-Event": "Issue Hook"}).json()["skipped"] is True
    assert calls == []


def test_provider_must_match_project_and_unknown_provider_404(client, admin, calls):
    mk(client, admin, "gitlab", "acme/team/shop")
    assert send(client, "gitea", GITEA_PUSH, {"X-Gitea-Event": "push"}).status_code == 404
    assert client.post("/webhook/svn/shop", content=b"{}").status_code == 404


def test_same_repo_name_can_exist_on_two_providers(client, admin):
    assert mk(client, admin, "gitlab", "acme/shop").status_code == 201
    body = {"slug": "shop2", "repo_full_name": "acme/shop", "provider": "bitbucket", "environments": [dict(ENV)]}
    assert client.post("/api/projects", json=body, headers=admin).status_code == 201
    body["slug"] = "shop3"
    assert client.post("/api/projects", json=body, headers=admin).status_code == 409


def test_project_validation(client, admin):
    assert mk(client, admin, "svn", "a/b").status_code == 422
    assert mk(client, admin, "gitea", "a/b").status_code == 422                                      # нужен git_url
    assert mk(client, admin, "gitlab", "a/b", git_url="http://insecure.example.com/a/b.git").status_code == 422
    assert mk(client, admin, "gitlab", "a/b", git_url="https://u:p@gitlab.example.com/a/b.git").status_code == 422


def test_source_token_and_webhook_info(client, admin, make_token):
    mk(client, admin, "gitlab", "acme/team/shop", git_token="glpat-SECRETVALUE")
    src = client.get("/api/projects/shop/source", headers=admin).json()
    assert src["provider"] == "gitlab" and src["has_git_token"] is True and "glpat" not in json.dumps(src)
    db = SessionLocal()
    p = db.query(Project).one()
    assert b"glpat" not in p.git_token_enc                                                           # в базе зашифрован
    db.close()
    info = client.get("/api/projects/shop/webhook", headers=admin).json()
    assert info["path"] == "/webhook/gitlab/shop" and info["secret"] == scm.webhook_secret("shop")
    dev = make_token("developer")
    assert client.get("/api/projects/shop/webhook", headers=dev).status_code == 403                  # секрет вебхука — только управляющим
    assert client.put("/api/projects/shop/git-token", json={"token": None}, headers=admin).json() == {"has_git_token": False}
    assert client.get("/api/projects/shop/source", headers=admin).json()["has_git_token"] is False


def test_private_repo_credentials_reach_kpack(client, admin, monkeypatch):
    seen = {}
    monkeypatch.setattr(webhooks.kpack, "ensure_git_credentials", lambda *a: seen.setdefault("args", a))
    monkeypatch.setattr(webhooks.kpack, "get_status", lambda *a, **k: {"latest_image": "img"})
    monkeypatch.setattr(webhooks.kpack, "trigger_build", lambda *a, **k: False)
    monkeypatch.setattr(webhooks.secrets_svc if hasattr(webhooks, "secrets_svc") else __import__("app.services.secrets", fromlist=["x"]), "sync_to_cluster", lambda *a, **k: 0)
    monkeypatch.setattr(webhooks.deploy, "deploy_image", lambda *a, **k: None)
    mk(client, admin, "gitlab", "acme/team/shop", git_token="glpat-SECRETVALUE", git_url="https://gitlab.example.com/acme/team/shop.git")
    db = SessionLocal()
    from app.db.models import Environment
    p, e = db.query(Project).one(), db.query(Environment).one()
    pid, eid = p.id, e.id
    db.close()
    webhooks.run_build_and_deploy(pid, eid, "https://gitlab.example.com/acme/team/shop.git", "a" * 40, "t")
    slug, url, user, token, ns = seen["args"]
    assert (slug, user, token) == ("shop", "oauth2", "glpat-SECRETVALUE") and url.startswith("https://gitlab.example.com")


def test_pull_request_events_are_normalized(client, admin, monkeypatch):
    got = []
    monkeypatch.setattr(plugins, "PULL_REQUEST_HANDLER", lambda ev, bg: got.append(ev) or {"accepted": True})
    mk(client, admin, "gitlab", "acme/team/shop")
    mr = {"object_kind": "merge_request", "user": {"username": "bob"}, "project": {"path_with_namespace": "acme/team/shop", "git_http_url": "https://g/x.git"},
          "object_attributes": {"action": "open", "iid": 9, "source_branch": "feat", "last_commit": {"id": "e" * 40}, "source": {"path_with_namespace": "acme/team/shop"}}}
    assert send(client, "gitlab", mr, {"X-Gitlab-Event": "Merge Request Hook"}).json() == {"accepted": True}
    ev = got[0]
    assert (ev["provider"], ev["action"], ev["number"], ev["branch"], ev["sha"], ev["head_repo"], ev["repo"]) == ("gitlab", "open", 9, "feat", "e" * 40, "acme/team/shop", "acme/team/shop")
    bb = scm.parse_pull_request("bitbucket", {"x-event-key": "pullrequest:fulfilled"}, {"pullrequest": {"id": 3, "source": {"branch": {"name": "f"}, "commit": {"hash": "a" * 40}, "repository": {"full_name": "fork/shop"}},
                                                   "destination": {"repository": {"full_name": "acme/shop"}}, "author": {"nickname": "z"}}})
    assert bb["action"] == "close" and bb["head_repo"] == "fork/shop" and bb["repo"] == "acme/shop"
    gt = scm.parse_pull_request("gitea", {"x-gitea-event": "pull_request"}, {"action": "synchronized", "number": 4, "repository": {"full_name": "acme/shop", "clone_url": "u"},
                                "pull_request": {"head": {"ref": "b", "sha": "f" * 40, "repo": {"full_name": "acme/shop"}}, "user": {"login": "q"}}})
    assert gt["action"] == "update" and gt["number"] == 4
