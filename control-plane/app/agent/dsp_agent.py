#!/usr/bin/env python3
"""SplitWave agent — запускает приложения в Docker на этом сервере по заданиям платформы.

Принципы:
  • Агент САМ соединяется с платформой (исходящий HTTPS), на сервере не нужно открывать входящие порты.
  • Выполняются только два вида заданий — deploy и remove — со строго проверяемыми полями; произвольных команд нет.
  • Все значения, попадающие в команду docker, проверяются здесь ещё раз (не доверяем даже платформе) и передаются списком
    аргументов без оболочки, поэтому внедрить команду нельзя.
  • Секреты приложения передаются через временный env-файл (права 0600), который удаляется сразу после запуска контейнера.
  • Неудачный выкат автоматически возвращает предыдущий контейнер.
Только стандартная библиотека Python 3.8+. Файл намеренно открыт и короткий: его можно прочитать перед установкой."""
import argparse
import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

VERSION = "1.0.0"
CONFIG = os.environ.get("DSP_AGENT_CONFIG", "/etc/dsp-agent/config.json")
POLL_SECONDS = 20
APP_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
VOL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
VOL_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]*$")
MEM_RE = re.compile(r"^[1-9][0-9]{1,5}[mMgG]$")
HEALTH_PATH_RE = re.compile(r"^/[A-Za-z0-9_./?=&%-]*$")
RESTARTS = ("no", "on-failure", "unless-stopped", "always")
LABEL = "dsp.managed"


class JobError(Exception):
    pass


# ───────────── проверка задания (защита в глубину) ─────────────
def check_deploy(p: dict) -> dict:
    app, image = p.get("app", ""), p.get("image", "")
    if not APP_RE.match(app):
        raise JobError("invalid app name")
    if not IMAGE_RE.match(image) or image.startswith("-"):
        raise JobError("invalid image reference")
    env = p.get("env") or {}
    if not isinstance(env, dict) or len(env) > 500:
        raise JobError("invalid env")
    for k, v in env.items():
        if not ENV_KEY_RE.match(k) or not isinstance(v, str) or "\n" in v or "\r" in v or "\x00" in v or len(v.encode()) > 65536:
            raise JobError(f"invalid env entry {k[:30]}")
    spec = p.get("runtime") or {}
    ports = spec.get("ports") or []
    if len(ports) > 20:
        raise JobError("too many ports")
    for x in ports:
        if not (isinstance(x.get("host"), int) and isinstance(x.get("container"), int) and 1 <= x["host"] <= 65535 and 1 <= x["container"] <= 65535):
            raise JobError("invalid port")
        if x.get("protocol", "tcp") not in ("tcp", "udp") or x.get("bind", "0.0.0.0") not in ("0.0.0.0", "127.0.0.1"):
            raise JobError("invalid port options")
    for v in spec.get("volumes") or []:
        if not VOL_NAME_RE.match(v.get("name", "")) or not VOL_PATH_RE.match(v.get("path", "")) or ".." in v["path"].split("/") or v["path"] == "/":
            raise JobError("invalid volume")
    if spec.get("restart", "unless-stopped") not in RESTARTS:
        raise JobError("invalid restart policy")
    if spec.get("memory") and not MEM_RE.match(spec["memory"]):
        raise JobError("invalid memory")
    if spec.get("cpus") is not None and not (isinstance(spec["cpus"], (int, float)) and 0.1 <= spec["cpus"] <= 64):
        raise JobError("invalid cpus")
    h = spec.get("health") or {}
    if h.get("mode", "running") not in ("http", "running") or not HEALTH_PATH_RE.match(h.get("path", "/")):
        raise JobError("invalid health check")
    if h.get("mode") == "http" and h.get("port") not in [x["host"] for x in ports]:
        raise JobError("health port must be a published port")
    return {"app": app, "image": image, "env": env, "runtime": spec, "registry": p.get("registry")}


# ───────────── docker ─────────────
def docker(*args, check=True, config_dir=None, timeout=600):
    env = dict(os.environ)
    if config_dir:
        env["DOCKER_CONFIG"] = config_dir
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, env=env)
    if check and r.returncode != 0:
        raise JobError(f"docker {args[0]} failed: {(r.stderr or r.stdout).strip()[-600:]}")
    return r


def container_exists(name: str) -> bool:
    return docker("inspect", "--type", "container", name, check=False).returncode == 0


def run_args(job: dict, env_file: str, name: str) -> list:
    spec = job["runtime"]
    a = ["run", "-d", "--name", name, "--label", f"{LABEL}=true", "--label", f"dsp.app={job['app']}", "--label", f"dsp.image={job['image']}",
         "--restart", spec.get("restart", "unless-stopped"), "--env-file", env_file]
    for x in spec.get("ports") or []:
        a += ["-p", f"{x.get('bind', '0.0.0.0')}:{x['host']}:{x['container']}/{x.get('protocol', 'tcp')}"]
    for v in spec.get("volumes") or []:
        a += ["-v", f"dsp-{job['app']}-{v['name']}:{v['path']}"]
    if spec.get("memory"):
        a += ["--memory", spec["memory"]]
    if spec.get("cpus"):
        a += ["--cpus", str(spec["cpus"])]
    return a + ["--", job["image"]]


def wait_healthy(name: str, spec: dict, log: list) -> None:
    h = spec.get("health") or {}
    deadline = time.time() + int(h.get("timeout_seconds", 60))
    if h.get("mode") == "http":
        url = f"http://127.0.0.1:{h['port']}{h.get('path', '/')}"
        while time.time() < deadline:
            if docker("inspect", "-f", "{{.State.Running}}", name, check=False).stdout.strip() != "true":
                raise JobError("container exited: " + docker("logs", "--tail", "20", name, check=False).stdout[-400:])
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    if 200 <= r.status < 400:
                        log.append(f"health ok: {url} -> {r.status}")
                        return
            except urllib.error.HTTPError as e:
                if 200 <= e.code < 400:
                    return
            except Exception:
                pass
            time.sleep(2)
        raise JobError(f"health check failed: {url} did not answer in {h.get('timeout_seconds', 60)}s")
    hold = min(int(h.get("timeout_seconds", 60)), 15)          # running: контейнер должен прожить несколько секунд без падения
    end = time.time() + hold
    while time.time() < end:
        if docker("inspect", "-f", "{{.State.Running}}", name, check=False).stdout.strip() != "true":
            raise JobError("container exited: " + docker("logs", "--tail", "20", name, check=False).stdout[-400:])
        time.sleep(1)
    log.append(f"container stayed running for {hold}s")


def do_deploy(raw: dict) -> dict:
    job, log = check_deploy(raw), []
    app = job["app"]
    cfg_dir = tempfile.mkdtemp(prefix="dsp-docker-")
    env_dir = tempfile.mkdtemp(prefix="dsp-env-")
    env_file = os.path.join(env_dir, "app.env")
    try:
        reg = job.get("registry")
        if reg:
            auth = base64.b64encode(f"{reg['username']}:{reg['password']}".encode()).decode()
            with open(os.path.join(cfg_dir, "config.json"), "w") as f:
                json.dump({"auths": {reg["server"]: {"auth": auth}}}, f)
            os.chmod(os.path.join(cfg_dir, "config.json"), 0o600)
        log.append("pulling " + job["image"])
        docker("pull", job["image"], config_dir=cfg_dir, timeout=1200)
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            for k, v in job["env"].items():
                f.write(f"{k}={v}\n")
        had_old = container_exists(app)
        if had_old:
            old = f"{app}-old"
            if container_exists(old):
                docker("rm", "-f", old)
            docker("rename", app, old)
            docker("stop", "-t", "20", old)
        try:
            cid = docker(*run_args(job, env_file, app)).stdout.strip()[:12]
            os.remove(env_file)                                   # секреты больше не лежат на диске
            wait_healthy(app, job["runtime"], log)
        except Exception as e:
            docker("logs", "--tail", "30", app, check=False)
            docker("rm", "-f", app, check=False)
            if had_old:
                docker("rename", f"{app}-old", app, check=False)
                docker("start", app, check=False)
                raise JobError(f"{e}; previous version restored")
            raise
        if had_old:
            docker("rm", "-f", f"{app}-old", check=False)
        log.append(f"deployed {app} ({cid})")
        return {"container": cid, "image": job["image"], "log": log}
    finally:
        shutil.rmtree(cfg_dir, ignore_errors=True)
        shutil.rmtree(env_dir, ignore_errors=True)


def do_remove(raw: dict) -> dict:
    app = raw.get("app", "")
    if not APP_RE.match(app):
        raise JobError("invalid app name")
    for n in (app, f"{app}-old"):
        docker("rm", "-f", n, check=False)
    return {"log": [f"removed {app}"]}


# ───────────── опрос платформы ─────────────
def inventory() -> dict:
    info = {"agent": VERSION}
    v = docker("version", "--format", "{{.Server.Version}}", check=False)
    info["docker"] = v.stdout.strip() if v.returncode == 0 else None
    try:
        info["host"] = {"cpus": os.cpu_count(), "load": os.getloadavg()[0], "mem_mb": int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1048576),
                        "disk_free_gb": round(shutil.disk_usage("/").free / 2 ** 30, 1), "os": _os_name()}
    except Exception:
        pass
    ps = docker("ps", "-a", "--filter", f"label={LABEL}=true", "--format",
                '{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Status}}\t{{.Label "dsp.image"}}', check=False)
    info["containers"] = []
    for line in ps.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            info["containers"].append({"name": parts[0], "image": parts[4] or parts[1], "state": parts[2], "status": parts[3]})
    return info


def _os_name() -> str:
    try:
        for line in open("/etc/os-release"):
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return sys.platform


class Platform:
    def __init__(self, cfg: dict):
        self.url = cfg["url"].rstrip("/")
        require_https(self.url)
        self.token = cfg["agent_token"]
        ctx = ssl.create_default_context(cafile=cfg.get("ca_file") or None)
        self.ctx = ctx

    def call(self, path: str, body: dict, timeout=40) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}", "User-Agent": f"dsp-agent/{VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout, context=self.ctx if self.url.startswith("https") else None) as r:
            return json.loads(r.read() or b"{}")


def require_https(url: str) -> None:
    """Токен агента не должен ходить по открытому каналу: http разрешён только для локального адреса или при DSP_AGENT_ALLOW_HTTP=1 (испытательные стенды)."""
    from urllib.parse import urlparse
    u = urlparse(url)
    if u.scheme == "https":
        return
    if u.scheme == "http" and (u.hostname in ("127.0.0.1", "localhost", "::1") or os.environ.get("DSP_AGENT_ALLOW_HTTP") == "1"):
        return
    raise SystemExit("адрес платформы должен начинаться с https:// (для испытаний: DSP_AGENT_ALLOW_HTTP=1)")


def enroll(url: str, token: str, name_hint: str, ca_file=None) -> str:
    require_https(url)
    ctx = ssl.create_default_context(cafile=ca_file) if url.startswith("https") else None
    req = urllib.request.Request(url.rstrip("/") + "/agent/v1/enroll", data=json.dumps({"token": token, "hostname": name_hint, "version": VERSION}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.loads(r.read())["agent_token"]


def run_job(job: dict) -> tuple:
    try:
        if job["kind"] == "deploy":
            return "succeeded", do_deploy(job["payload"])
        if job["kind"] == "remove":
            return "succeeded", do_remove(job["payload"])
        return "failed", {"error": f"unknown job kind {job['kind'][:30]}"}
    except JobError as e:
        return "failed", {"error": str(e)[:1500]}
    except Exception as e:  # noqa: BLE001
        return "failed", {"error": f"{type(e).__name__}: {e}"[:1500]}


def serve(cfg: dict) -> None:
    plat = Platform(cfg)
    backoff = 2
    while True:
        try:
            resp = plat.call("/agent/v1/poll", {"info": inventory()}, timeout=POLL_SECONDS + 25)
            backoff = 2
            job = resp.get("job")
            if job:
                status, result = run_job(job)
                plat.call(f"/agent/v1/jobs/{job['id']}/report", {"status": status, "result": result, "info": inventory()})
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                print("agent token rejected by the platform; stopping", file=sys.stderr)
                sys.exit(3)
            time.sleep(min(backoff, 60)); backoff *= 2
        except Exception as e:  # noqa: BLE001 — платформа недоступна: ждём и пробуем снова
            print(f"platform unreachable: {e}", file=sys.stderr)
            time.sleep(min(backoff, 60)); backoff *= 2


def main() -> None:
    ap = argparse.ArgumentParser(description="SplitWave agent")
    sub = ap.add_subparsers(dest="cmd")
    en = sub.add_parser("enroll", help="подключить этот сервер к платформе")
    en.add_argument("--url", required=True); en.add_argument("--token", required=True); en.add_argument("--ca-file")
    sub.add_parser("run", help="работать (обычно запускается службой systemd)")
    sub.add_parser("version")
    a = ap.parse_args()
    if a.cmd == "version":
        print(VERSION)
    elif a.cmd == "enroll":
        agent_token = enroll(a.url, a.token, os.uname().nodename, a.ca_file)
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"url": a.url, "agent_token": agent_token, "ca_file": a.ca_file}, f)
        print("enrolled; config written to", CONFIG)
    elif a.cmd == "run":
        serve(json.load(open(CONFIG)))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
