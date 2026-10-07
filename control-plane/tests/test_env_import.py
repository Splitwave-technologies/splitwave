"""Импорт .env в секреты: разбор синтаксиса, предпросмотр, применение, область (среда/все), права, чтение из репозитория, отсутствие значений в ответах и аудите."""
import json

import pytest

from app.db.base import SessionLocal
from app.db.models import AuditEvent, Project, Secret, SecretVersion
from app.services import crypto, dotenv, repoinspect

V = "Sup3r-Secret-Value-123"


def parsed(text):
    r = dotenv.parse(text)
    return r.entries, r.notes, r.problems


# ---------- разбор ----------

@pytest.mark.parametrize("text,expected", [
    ("A=1", {"A": "1"}),
    ("export A=1\nexport  B = 2", {"A": "1", "B": "2"}),
    ('A="x y"', {"A": "x y"}),
    ("A='x $y \\n'", {"A": "x $y \\n"}),                                   # одинарные кавычки — буквально
    ('A="l1\\nl2\\t\\"q\\" \\\\ \\$"', {"A": 'l1\nl2\t"q" \\ $'}),           # экранирование в двойных
    ("A=val # comment", {"A": "val"}),
    ("A=val#notcomment", {"A": "val#notcomment"}),                         # «#» без пробела — часть значения
    ('A="v # kept" # tail', {"A": "v # kept"}),
    ("A=postgres://u:p@h:5432/db?x=1&y=2", {"A": "postgres://u:p@h:5432/db?x=1&y=2"}),
    ("A=a=b=c", {"A": "a=b=c"}),
    ("﻿A=1\r\nB=2\r\n", {"A": "1", "B": "2"}),                        # BOM и Windows-переводы строк
    ("# only\n\n   \n  # indented", {}),
    ("A=1\nA=2", {"A": "2"}),                                              # последнее значение побеждает
    ('A="multi\nline\nvalue"\nB=2', {"A": "multi\nline\nvalue", "B": "2"}),
    ("A='multi\nline'", {"A": "multi\nline"}),
    ("A=${B}/x\nB=1", {"A": "${B}/x", "B": "1"}),                          # подстановки не раскрываются
    ("A=  spaced value  ", {"A": "spaced value"}),
    ("_UNDER=1\nlower_case=2\nA1=3", {"_UNDER": "1", "lower_case": "2", "A1": "3"}),
])
def test_syntax(text, expected):
    assert parsed(text)[0] == expected


def test_notes_and_problems():
    entries, notes, problems = parsed('A=1\nA=2\nB=${A}\nC="x\ny"\nD=\nE\n9X=1\nF="a"junk\nG=ok')
    assert notes["A"] == ["duplicate"] and notes["B"] == ["interpolation"] and notes["C"] == ["multiline"]
    assert {(p["line"], p["reason"]) for p in problems} == {(6, "empty"), (7, "bad_line"), (8, "bad_name"), (9, "text_after_quote")}   # C занимает строки 4–5
    assert entries["G"] == "ok" and "D" not in entries and "F" not in entries


def test_unterminated_quote_stops_parsing_and_reports_only_the_line():
    e, _, problems = parsed('A=1\nB="never closed\nC=3')
    assert e == {"A": "1"} and problems == [{"line": 2, "key": "B", "reason": "unterminated_quote"}]
    e, _, problems = parsed("A='never closed\nC=3")
    assert e == {} and problems[0]["reason"] == "unterminated_quote"


def test_limits():
    assert parsed("A=" + "x" * (dotenv.MAX_TEXT))[2] == [{"line": 0, "key": None, "reason": "too_large"}]
    big = "A=" + "x" * (65 * 1024)
    assert parsed(big)[2][0]["reason"] == "value_too_large"
    many = "\n".join(f"K{i}=v" for i in range(dotenv.MAX_KEYS + 5))
    e, _, problems = parsed(many)
    assert len(e) == dotenv.MAX_KEYS and sum(p["reason"] == "too_many_keys" for p in problems) == 5


def test_problems_never_contain_values():
    _, _, problems = parsed(f"BAD NAME={V}\n9X={V}\nA=\"{V}\"junk\nB=\"{V}")
    assert problems and V not in json.dumps(problems)
    assert all(p["key"] is None or p["key"].isidentifier() for p in problems)


# ---------- API ----------

def imp(client, headers, body, environment=None, slug="demo"):
    q = f"?environment={environment}" if environment else ""
    return client.post(f"/api/projects/{slug}/secrets/import{q}", json=body, headers=headers)


def keys(slug="demo", scope="*"):
    db = SessionLocal()
    try:
        p = db.query(Project).filter_by(slug=slug).one()
        return {s.key: s for s in db.query(Secret).filter_by(project_id=p.id, scope=scope).all()}
    finally:
        db.close()


def test_dry_run_changes_nothing_and_apply_creates_encrypted_secrets(client, project, admin):
    text = f"DB_PASSWORD={V}\nAPI_KEY='k-{V}'\nEMPTY=\n"
    r = imp(client, admin, {"content": text, "dry_run": True})
    assert r.status_code == 200 and r.json()["applied"] is False
    assert [(i["key"], i["action"]) for i in r.json()["items"]] == [("DB_PASSWORD", "create"), ("API_KEY", "create")]
    assert r.json()["counts"]["invalid"] == 1 and keys() == {}
    r = imp(client, admin, {"content": text})
    assert r.status_code == 200 and r.json()["applied"] is True and r.json()["counts"]["create"] == 2 and r.json()["source"] == "upload"
    got = keys()
    assert set(got) == {"DB_PASSWORD", "API_KEY"} and crypto.decrypt_value(got["DB_PASSWORD"].encrypted_value) == V and V.encode() not in got["DB_PASSWORD"].encrypted_value
    assert V not in r.text                                                    # значения в ответе нет


def test_existing_secrets_are_skipped_unless_overwrite_and_same_values_do_not_add_versions(client, project, admin):
    imp(client, admin, {"content": "A=one\nB=two"})
    r = imp(client, admin, {"content": "A=one\nB=changed\nC=new"}).json()
    assert {i["key"]: i["action"] for i in r["items"]} == {"A": "same", "B": "exists", "C": "create"}
    assert crypto.decrypt_value(keys()["B"].encrypted_value) == "two"          # не перезаписано
    r = imp(client, admin, {"content": "A=one\nB=changed", "overwrite": True}).json()
    assert {i["key"]: i["action"] for i in r["items"]} == {"A": "same", "B": "update"}
    assert crypto.decrypt_value(keys()["B"].encrypted_value) == "changed" and keys()["B"].version == 2 and keys()["A"].version == 1


def test_scope_environment_vs_all_projects_and_unknown_environment(client, project, admin):
    assert imp(client, admin, {"content": "A=1"}, environment="prod").status_code == 200
    assert set(keys(scope="prod")) == {"A"} and keys(scope="*") == {}
    assert imp(client, admin, {"content": "A=1"}, environment="nope").status_code == 404


def test_keys_filter_applies_only_selected(client, project, admin):
    r = imp(client, admin, {"content": "A=1\nB=2\nC=3", "keys": ["A", "C"]}).json()
    assert {i["key"]: i["action"] for i in r["items"]} == {"A": "create", "B": "not_selected", "C": "create"} and set(keys()) == {"A", "C"}


def test_audit_has_names_and_counts_but_no_values_and_versions_record_the_reason(client, project, admin):
    imp(client, admin, {"content": f"TOKEN={V}\nOTHER=x"})
    db = SessionLocal()
    try:
        ev = [e for e in db.query(AuditEvent).all() if e.action == "secrets_imported"]
        assert len(ev) == 1 and ev[0].detail["created"] == 2 and sorted(ev[0].detail["keys"]) == ["OTHER", "TOKEN"] and ev[0].detail["source"] == "upload"
        assert V not in json.dumps([e.detail for e in db.query(AuditEvent).all()], default=str)
        imp(client, admin, {"content": "TOKEN=new", "overwrite": True})
        assert {v.reason for v in db.query(SecretVersion).all()} == {"import"}
    finally:
        db.close()
    assert client.get("/api/projects/demo/secrets", headers=admin).text.count(V) == 0


def test_permissions_and_argument_validation(client, project, make_token, admin):
    assert imp(client, make_token("developer"), {"content": "A=1"}).status_code == 403
    assert imp(client, make_token("viewer"), {"content": "A=1"}).status_code == 403
    assert imp(client, admin, {}).status_code == 422                                        # ни content, ни from_repo
    assert imp(client, admin, {"content": "A=1", "from_repo": True}).status_code == 422     # и то и другое
    assert imp(client, admin, {"content": "A=1"}, slug="nope").status_code == 404
    assert imp(client, admin, {"content": "x" * (dotenv.MAX_TEXT + 1)}).status_code == 422
    assert imp(client, admin, {"content": "# nothing here"}).json()["applied"] is False      # пустой файл — ничего не записано


# ---------- чтение из репозитория ----------

class FakeClient:
    calls = []
    text = f"FROM_REPO={V}\n"
    error = None

    def __init__(self, ref, token):
        FakeClient.calls.append({"repo": ref.full_name, "token": token})

    def read_file(self, path, ref):
        FakeClient.calls[-1].update(path=path, ref=ref)
        if FakeClient.error:
            raise FakeClient.error
        return FakeClient.text


@pytest.fixture
def fake_repo(monkeypatch):
    FakeClient.calls, FakeClient.text, FakeClient.error = [], f"FROM_REPO={V}\n", None
    monkeypatch.setitem(repoinspect.CLIENTS, "github", FakeClient)
    return FakeClient


def test_repo_import_reads_the_env_branch_and_never_returns_values(client, project, admin, fake_repo):
    r = imp(client, admin, {"from_repo": True, "path": "deploy/prod.env"}, environment="prod")
    assert r.status_code == 200 and r.json()["source"].startswith("repo:deploy/prod.env@") and V not in r.text
    assert fake_repo.calls[0]["path"] == "deploy/prod.env" and fake_repo.calls[0]["repo"] and set(keys(scope="prod")) == {"FROM_REPO"}
    assert crypto.decrypt_value(keys(scope="prod")["FROM_REPO"].encrypted_value) == V


def test_repo_import_token_order_request_then_project_then_none(client, project, admin, fake_repo):
    imp(client, admin, {"from_repo": True, "dry_run": True})
    assert fake_repo.calls[-1]["token"] is None
    db = SessionLocal(); p = db.query(Project).filter_by(slug="demo").one(); p.git_token_enc = crypto.encrypt_value("stored-token"); db.commit(); db.close()
    imp(client, admin, {"from_repo": True, "dry_run": True})
    assert fake_repo.calls[-1]["token"] == "stored-token"
    imp(client, admin, {"from_repo": True, "dry_run": True, "token": "given-token"})
    assert fake_repo.calls[-1]["token"] == "given-token"


@pytest.mark.parametrize("path", ["../etc/passwd", "/abs/.env", "a//b", "a/../b", "", "a b", "x" * 201, ".env;rm"])
def test_repo_import_rejects_unsafe_paths_before_any_request(client, project, admin, fake_repo, path):
    assert imp(client, admin, {"from_repo": True, "path": path}).status_code == 422 and fake_repo.calls == []


def test_repo_import_errors_are_explained(client, project, admin, fake_repo):
    fake_repo.text = None
    r = imp(client, admin, {"from_repo": True})
    assert r.status_code == 404 and ".env" in r.json()["detail"]
    fake_repo.text = "A=" + "x" * repoinspect.MAX_FILE
    assert imp(client, admin, {"from_repo": True}).status_code == 422
    fake_repo.text = "A=1"
    fake_repo.error = repoinspect.InspectError("Нет доступа: репозиторий приватный", 422)
    r = imp(client, admin, {"from_repo": True})
    assert r.status_code == 422 and "приватный" in r.json()["detail"] and keys() == {}
