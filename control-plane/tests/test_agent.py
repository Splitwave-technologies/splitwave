"""Агент сервера: строгая проверка заданий, безопасная сборка команды docker, откат при неудаче, файлы агента."""
import hashlib
import os
import stat
from types import SimpleNamespace as NS

import pytest

from app.agent import dsp_agent as ag

GOOD = {"app": "shop", "image": "registry.example.com/team/shop@sha256:" + "a" * 64, "env": {"DB_PASS": "s3cret", "MODE": "prod"},
        "runtime": {"ports": [{"host": 8080, "container": 8000}], "volumes": [{"name": "data", "path": "/data"}], "restart": "unless-stopped",
                    "memory": "512m", "cpus": 1, "health": {"mode": "http", "port": 8080, "path": "/health", "timeout_seconds": 5}},
        "registry": {"server": "registry.example.com", "username": "u", "password": "p"}}


def job(**over):
    j = {k: (dict(v) if isinstance(v, dict) else v) for k, v in GOOD.items()}
    j.update(over)
    return j


@pytest.mark.parametrize("patch", [
    {"app": "Shop"}, {"app": "a;rm -rf /"}, {"app": ""}, {"image": "-v /:/host alpine"}, {"image": "img with space"}, {"image": "img;reboot"}, {"image": "$(id)"},
    {"env": {"1BAD": "x"}}, {"env": {"OK": "line1\nline2"}}, {"env": {"OK": "x\x00y"}}, {"env": {"A B": "x"}},
    {"runtime": {"ports": [{"host": 0, "container": 1}]}}, {"runtime": {"ports": [{"host": 80, "container": 80, "bind": "10.0.0.1"}]}},
    {"runtime": {"volumes": [{"name": "d", "path": "/a/../etc"}]}}, {"runtime": {"volumes": [{"name": "d", "path": "/"}]}}, {"runtime": {"volumes": [{"name": "../x", "path": "/d"}]}},
    {"runtime": {"restart": "yolo"}}, {"runtime": {"memory": "1g; touch /x"}}, {"runtime": {"cpus": 1000}},
    {"runtime": {"health": {"mode": "http", "port": 9, "path": "/"}, "ports": [{"host": 80, "container": 80}]}}, {"runtime": {"health": {"path": "no-slash"}}},
])
def test_unsafe_jobs_are_rejected_by_the_agent_itself(patch):
    with pytest.raises(ag.JobError):
        ag.check_deploy(job(**patch))


def test_valid_job_passes_and_docker_args_have_no_injection_surface():
    j = ag.check_deploy(job())
    a = ag.run_args(j, "/tmp/x.env", "shop")
    assert a[:4] == ["run", "-d", "--name", "shop"] and a[-2:] == ["--", j["image"]]             # "--": образ не может стать флагом
    assert "-p" in a and "0.0.0.0:8080:8000/tcp" in a and "dsp-shop-data:/data" in a and "--memory" in a and "--env-file" in a
    assert all(isinstance(x, str) for x in a) and not any(";" in x or "$(" in x for x in a if x != j["image"])
    assert "s3cret" not in " ".join(a)                                                              # секреты не в аргументах (их видно в ps)


class FakeDocker:
    """Имитация docker: состояние контейнеров и журнал команд."""

    def __init__(self, existing=(), fail_run_health=False):
        self.c = {n: "running" for n in existing}
        self.log, self.seen_env = [], {}
        self.fail_health = fail_run_health
        self.cfg_seen = {}

    def __call__(self, *args, check=True, config_dir=None, timeout=600):
        self.log.append(args)
        cmd = args[0]
        ok = NS(returncode=0, stdout="", stderr="")
        if cmd == "pull":
            if config_dir:
                p = os.path.join(config_dir, "config.json")
                self.cfg_seen = {"exists": os.path.exists(p), "mode": stat.S_IMODE(os.stat(p).st_mode) if os.path.exists(p) else None}
        elif cmd == "inspect":
            name = args[-1]
            return NS(returncode=0 if name in self.c else 1, stdout="true" if self.c.get(name) == "running" else "false", stderr="")
        elif cmd == "rename":
            self.c[args[2]] = self.c.pop(args[1])
        elif cmd == "stop":
            self.c[args[-1]] = "exited"
        elif cmd == "start":
            self.c[args[-1]] = "running"
        elif cmd == "rm":
            self.c.pop(args[-1], None)
        elif cmd == "run":
            ef = args[args.index("--env-file") + 1]
            self.seen_env = {"mode": stat.S_IMODE(os.stat(ef).st_mode), "text": open(ef).read()}
            name = args[args.index("--name") + 1]
            self.c[name] = "running"
            ok.stdout = "abc123def4567890"
        return ok


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        d = FakeDocker(**kw)
        monkeypatch.setattr(ag, "docker", d)
        monkeypatch.setattr(ag.time, "sleep", lambda s: None)
        return d
    return install


def test_first_deploy_uses_private_env_file_and_removes_secrets(fake, monkeypatch):
    d = fake()
    monkeypatch.setattr(ag, "wait_healthy", lambda *a: None)
    res = ag.do_deploy(job())
    assert res["container"] == "abc123def456" and d.c == {"shop": "running"}
    assert d.seen_env["mode"] == 0o600 and "DB_PASS=s3cret" in d.seen_env["text"]
    assert d.cfg_seen == {"exists": True, "mode": 0o600}                                            # доступ к реестру — временный и закрытый
    assert ("pull", GOOD["image"]) == d.log[0][:2]


def test_redeploy_replaces_the_old_container(fake, monkeypatch):
    d = fake(existing=["shop"])
    monkeypatch.setattr(ag, "wait_healthy", lambda *a: None)
    ag.do_deploy(job())
    names = [a[0] for a in d.log]
    assert names.index("rename") < names.index("stop") < names.index("run")
    assert "shop-old" not in d.c and d.c["shop"] == "running"


def test_failed_health_check_restores_the_previous_version(fake, monkeypatch):
    d = fake(existing=["shop"])

    def bad(*a):
        raise ag.JobError("health check failed")
    monkeypatch.setattr(ag, "wait_healthy", bad)
    with pytest.raises(ag.JobError, match="previous version restored"):
        ag.do_deploy(job())
    assert d.c == {"shop": "running"}                                                               # старый контейнер снова работает
    assert ("rename", "shop-old", "shop") in d.log and ("start", "shop") in d.log


def test_failed_first_deploy_leaves_nothing_running(fake, monkeypatch):
    d = fake()
    monkeypatch.setattr(ag, "wait_healthy", lambda *a: (_ for _ in ()).throw(ag.JobError("container exited")))
    with pytest.raises(ag.JobError):
        ag.do_deploy(job())
    assert d.c == {}


def test_remove_job_and_unknown_kinds(fake):
    d = fake(existing=["shop", "shop-old"])
    assert ag.run_job({"kind": "remove", "payload": {"app": "shop"}})[0] == "succeeded" and d.c == {}
    assert ag.run_job({"kind": "format-disk", "payload": {}})[0] == "failed"
    status, res = ag.run_job({"kind": "deploy", "payload": job(image="-x")})
    assert status == "failed" and "invalid image" in res["error"]


def test_inventory_reports_managed_containers(monkeypatch):
    def fake_docker(*args, check=True, **kw):
        if args[0] == "version":
            return NS(returncode=0, stdout="27.1.1\n", stderr="")
        return NS(returncode=0, stdout="shop\tsha256:x\trunning\tUp 2 minutes\treg/shop@sha256:abc\n", stderr="")
    monkeypatch.setattr(ag, "docker", fake_docker)
    inv = ag.inventory()
    assert inv["docker"] == "27.1.1" and inv["containers"] == [{"name": "shop", "image": "reg/shop@sha256:abc", "state": "running", "status": "Up 2 minutes"}]


def test_agent_files_are_served_with_matching_checksum(client):
    src = client.get("/agent/dsp_agent.py")
    assert src.status_code == 200 and "def do_deploy" in src.text
    assert client.get("/agent/dsp_agent.py.sha256").text == hashlib.sha256(src.text.encode()).hexdigest()
    sh = client.get("/agent/install.sh", headers={"Host": "platform.example.com"})
    assert "URL=\"http://platform.example.com\"" in sh.text and "dsp_agent.py.sha256" in sh.text
    sh2 = client.get("/agent/install.sh", headers={"Host": "p.example.com", "X-Forwarded-Proto": "https"})
    assert "http://p.example.com" in sh2.text                                                        # без доверенного прокси заголовку не верим


def test_install_script_is_valid_shell_and_contains_no_secrets(client):
    import subprocess, tempfile
    text = client.get("/agent/install.sh").text
    with tempfile.NamedTemporaryFile("w", suffix=".sh") as f:
        f.write(text); f.flush()
        assert subprocess.run(["sh", "-n", f.name]).returncode == 0
    assert "token" in text.lower() and "s3cret" not in text


def test_agent_refuses_a_plain_http_platform_address(monkeypatch):
    import pytest
    from app.agent import dsp_agent
    monkeypatch.delenv("DSP_AGENT_ALLOW_HTTP", raising=False)
    dsp_agent.require_https("https://panel.example.com")
    dsp_agent.require_https("http://127.0.0.1:8080")
    with pytest.raises(SystemExit):
        dsp_agent.require_https("http://panel.example.com")
    monkeypatch.setenv("DSP_AGENT_ALLOW_HTTP", "1")
    dsp_agent.require_https("http://lab.internal:8080")
