"""Сервисы данных проекта (БД и S3-хранилища) в namespace среды: обнаружение, смена версии, миграция между версиями и движками.

Принципы:
- Состояние живёт в кластере (аннотация `splitwave.io/migration` на рабочей нагрузке), своей таблицы в БД платформы нет.
- Миграция «рядом»: новая версия поднимается отдельным экземпляром, данные копируются заданием (Job), старый экземпляр не трогается
  и продолжает работать. Переключение (cutover) — смена селектора у существующего Service; откат — возвращение селектора.
  Старый экземпляр после переключения остаётся остановленным (с томом) до явной финализации.
- Пароли платформа не читает и не пишет в ответы: переменные окружения контейнера копируются в Job как есть (значение или ссылка на Secret).
- Мажорная смена версии БД без миграции запрещена: том одной мажорной версии новая версия не откроет."""
import json
import re
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from app.services import kubeclient
from app.services.secrets import is_managed

ANNOTATION = "splitwave.io/migration"
LABEL_INSTANCE = "splitwave.io/instance"
LABEL_JOB = "splitwave.io/data-job"
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
RCLONE_IMAGE = "rclone/rclone:1.68.2"
# По одному бакету: список бакетов приёмника не нужен (SeaweedFS без настроенной аутентификации корневой список не отдаёт); бакет создаётся при копировании.
_RCLONE_SCRIPT = (
    "set -e; n=0; for b in $(rclone lsf SRC: --dirs-only | tr -d '/'); do n=$((n+1)); echo \"bucket $b\"; "
    "rclone copy \"SRC:$b\" \"DST:$b\" --create-empty-src-dirs --transfers 8 --retries 5 --low-level-retries 20 --retries-sleep 5s "
    "&& rclone check \"SRC:$b\" \"DST:$b\" --one-way; done; echo \"buckets verified: $n\"")


class DataServiceError(Exception):
    """Ошибка, понятная пользователю (код HTTP определяется полем status)."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# ---------- каталог движков ----------
# repos — имена образов (без реестра и тега); env_* — имена переменных окружения контейнера, откуда берутся учётные данные.
ENGINES = {
    "postgres": {
        "category": "database", "port": 5432, "label": "PostgreSQL",
        "repos": ("postgres", "postgis/postgis", "pgvector/pgvector", "timescale/timescaledb", "bitnami/postgresql",
                  "bitnamilegacy/postgresql", "cloudnative-pg/postgresql"),
        "env_user": ("POSTGRES_USER", "POSTGRESQL_USERNAME"), "env_password": ("POSTGRES_PASSWORD", "POSTGRESQL_PASSWORD"),
        "default_user": "postgres", "client_repo": "postgres",
    },
    "mysql": {
        "category": "database", "port": 3306, "label": "MySQL",
        "repos": ("mysql", "bitnami/mysql", "bitnamilegacy/mysql", "mysql/mysql-server"),
        "env_user": (), "env_password": ("MYSQL_ROOT_PASSWORD",), "default_user": "root", "client_repo": "mysql",
    },
    "mariadb": {
        "category": "database", "port": 3306, "label": "MariaDB",
        "repos": ("mariadb", "bitnami/mariadb", "bitnamilegacy/mariadb"),
        "env_user": (), "env_password": ("MARIADB_ROOT_PASSWORD", "MYSQL_ROOT_PASSWORD"), "default_user": "root", "client_repo": "mariadb",
    },
    "redis": {
        "category": "cache", "port": 6379, "label": "Redis",
        "repos": ("redis", "valkey/valkey", "bitnami/redis", "bitnamilegacy/redis"),
        "env_user": (), "env_password": ("REDIS_PASSWORD",), "default_user": None, "client_repo": None,
    },
    "minio": {
        "category": "s3", "port": 9000, "label": "MinIO",
        "repos": ("minio/minio", "bitnami/minio", "bitnamilegacy/minio", "quay.io/minio/minio"),
        "env_user": ("MINIO_ROOT_USER", "MINIO_ACCESS_KEY"), "env_password": ("MINIO_ROOT_PASSWORD", "MINIO_SECRET_KEY"),
        "default_user": None, "client_repo": None, "rclone_provider": "Minio",
    },
    "seaweedfs": {
        "category": "s3", "port": 8333, "label": "SeaweedFS",
        "repos": ("chrislusf/seaweedfs", "seaweedfs/seaweedfs"),
        "env_user": ("AWS_ACCESS_KEY_ID", "S3_ACCESS_KEY"), "env_password": ("AWS_SECRET_ACCESS_KEY", "S3_SECRET_KEY"),
        "default_user": None, "client_repo": None, "rclone_provider": "SeaweedFS",
    },
}
DB_ENGINES = ("postgres", "mysql", "mariadb")


def parse_image(image: str) -> tuple[str, str]:
    """'registry:5000/postgis/postgis:16-3.5@sha256:…' → ('postgis/postgis', '16-3.5')."""
    ref = image.split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    tag = "latest"
    if ":" in last:
        ref, tag = ref.rsplit(":", 1)
    parts = ref.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]                                        # отбрасываем адрес реестра
    return "/".join(parts), tag


def detect_engine(image: str) -> Optional[str]:
    repo, _ = parse_image(image)
    for engine, spec in ENGINES.items():
        for r in spec["repos"]:
            if repo == r or repo.endswith("/" + r):
                return engine
    return None


def major_of(engine: str, tag: str) -> Optional[str]:
    """Мажорная версия по тегу: postgres '16-3.5' → '16'; mysql '8.0.36' → '8.0'; mariadb '11.4.2' → '11.4'. Нет числа (latest) → None."""
    m = re.match(r"^(\d+)(?:\.(\d+))?", tag)
    if not m:
        return None
    if engine == "postgres":
        return m.group(1)
    if engine in ("mysql", "mariadb"):
        return f"{m.group(1)}.{m.group(2)}" if m.group(2) else m.group(1)
    return None


def _major_key(major: str):
    return tuple(int(x) for x in major.split("."))


# ---------- обнаружение ----------

def _container(spec: client.V1PodSpec) -> Optional[client.V1Container]:
    for c in spec.containers or []:
        if detect_engine(c.image or ""):
            return c
    return None


def _workloads(apps, ns: str) -> list:
    out = []
    for kind, call in (("StatefulSet", apps.list_namespaced_stateful_set), ("Deployment", apps.list_namespaced_deployment)):
        for w in call(ns).items:
            out.append((kind, w))
    return out


def _pvc_names(w) -> list[str]:
    return [t.metadata.name for t in (w.spec.volume_claim_templates or [])]


def read_state(w) -> Optional[dict]:
    raw = ((w.metadata.annotations or {}).get(ANNOTATION))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def discover(namespace: str, cluster: Optional[str] = None) -> list[dict]:
    """Сервисы данных в namespace: движок, версия, готовность, тома, активная миграция. Приложения платформы пропускаются."""
    apps, core = kubeclient.apps_v1_api(cluster), kubeclient.core_v1_api(cluster)
    out = []
    for kind, w in _workloads(apps, namespace):
        if is_managed(w.metadata.labels):
            continue
        c = _container(w.spec.template.spec)
        if not c:
            continue
        engine = detect_engine(c.image)
        repo, tag = parse_image(c.image)
        spec = ENGINES[engine]
        sizes = []
        for t in (w.spec.volume_claim_templates or []):
            req = ((t.spec.resources.requests or {}) if t.spec and t.spec.resources else {})
            sizes.append({"name": t.metadata.name, "size": req.get("storage")})
        svc = find_service(core, namespace, w)
        out.append({"name": w.metadata.name, "workload": kind, "engine": engine, "label": spec["label"], "category": spec["category"],
                    "image": c.image, "repo": repo, "tag": tag, "major": major_of(engine, tag),
                    "replicas": w.spec.replicas, "ready": w.status.ready_replicas or 0,
                    "service": svc.metadata.name if svc else None, "port": spec["port"], "volumes": sizes,
                    "migration": read_state(w)})
    return sorted(out, key=lambda s: s["name"])


def find_service(core, ns: str, w) -> Optional[client.V1Service]:
    """Service, чей селектор целиком входит в метки подов нагрузки (основной, не headless)."""
    labels = w.spec.template.metadata.labels or {}
    best = None
    for s in core.list_namespaced_service(ns).items:
        sel = s.spec.selector or {}
        if sel and all(labels.get(k) == v for k, v in sel.items()):
            if s.spec.cluster_ip == "None":
                best = best or s
                continue
            return s
    return best


# ---------- учётные данные (копируются в Job как есть, платформа значений не читает) ----------

def _find_env(c: client.V1Container, names) -> Optional[client.V1EnvVar]:
    for n in names:
        for e in c.env or []:
            if e.name == n and (e.value is not None or e.value_from is not None):
                return e
    return None


def _env_as(e: Optional[client.V1EnvVar], new_name: str) -> Optional[client.V1EnvVar]:
    if e is None:
        return None
    return client.V1EnvVar(name=new_name, value=e.value, value_from=deepcopy(e.value_from))


def credentials_env(engine: str, c: client.V1Container, prefix: str = "") -> list[client.V1EnvVar]:
    """Переменные пользователя и пароля для клиента движка (PGUSER/PGPASSWORD; MYSQL_USER/MYSQL_PWD; SRC_ACCESS/SRC_SECRET)."""
    spec = ENGINES[engine]
    user = _find_env(c, spec["env_user"])
    pwd = _find_env(c, spec["env_password"])
    if pwd is None and c.env_from:
        return []                      # учётные данные приходят целиком из Secret/ConfigMap (envFrom): Job получит те же envFrom, а имена переназначит env_prelude
    if pwd is None and engine not in ("redis",):
        raise DataServiceError("password variable of the container not found (expected one of: " + ", ".join(spec["env_password"]) + ")")
    if engine == "postgres":
        out = [_env_as(user, prefix + "PGUSER") or client.V1EnvVar(name=prefix + "PGUSER", value=spec["default_user"]),
               _env_as(pwd, prefix + "PGPASSWORD")]
    elif engine in ("mysql", "mariadb"):
        out = [client.V1EnvVar(name=prefix + "MYSQL_USER", value="root"), _env_as(pwd, prefix + "MYSQL_PWD")]
    else:
        out = [_env_as(user, prefix + "ACCESS"), _env_as(pwd, prefix + "SECRET")]
    return [e for e in out if e is not None]


def env_prelude(engine: str) -> str:
    """Если пароль пришёл через envFrom (POSTGRES_PASSWORD и т.п.), переназначаем в переменные клиента; уже заданные не трогаем."""
    if engine == "postgres":
        return ('export PGUSER="${PGUSER:-${POSTGRES_USER:-${POSTGRESQL_USERNAME:-postgres}}}"; '
                'export PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-${POSTGRESQL_PASSWORD:-}}}"; ')
    if engine in ("mysql", "mariadb"):
        return 'export MYSQL_PWD="${MYSQL_PWD:-${MYSQL_ROOT_PASSWORD:-${MARIADB_ROOT_PASSWORD:-}}}"; '
    if ENGINES.get(engine, {}).get("category") == "s3":
        return ('export RCLONE_CONFIG_SRC_ACCESS_KEY_ID="${RCLONE_CONFIG_SRC_ACCESS_KEY_ID:-${MINIO_ROOT_USER:-${MINIO_ACCESS_KEY:-${AWS_ACCESS_KEY_ID:-}}}}"; '
                'export RCLONE_CONFIG_SRC_SECRET_ACCESS_KEY="${RCLONE_CONFIG_SRC_SECRET_ACCESS_KEY:-${MINIO_ROOT_PASSWORD:-${MINIO_SECRET_KEY:-${AWS_SECRET_ACCESS_KEY:-}}}}"; ')
    return ""


# ---------- смена версии без смены формата данных ----------

def validate_tag(tag: str) -> str:
    if not isinstance(tag, str) or not TAG_RE.match(tag):
        raise DataServiceError("tag must look like 16.4, 17-alpine or 2025.7.23-debian-12-r3")
    return tag


def _get_workload(namespace: str, name: str, cluster: Optional[str]):
    apps = kubeclient.apps_v1_api(cluster)
    for kind, call in (("StatefulSet", apps.read_namespaced_stateful_set), ("Deployment", apps.read_namespaced_deployment)):
        try:
            return kind, call(name, namespace)
        except ApiException as e:
            if e.status != 404:
                raise
    raise DataServiceError(f"service {name} not found in namespace {namespace}", 404)


def change_version(namespace: str, name: str, tag: str, cluster: Optional[str] = None, backup: bool = True) -> dict:
    """Меняет тег образа в пределах той же мажорной версии (для БД) или любого тега (S3, Redis).
    Для БД перед заменой запускает задание-копию (дамп в отдельный том), если backup=True и движок это умеет."""
    validate_tag(tag)
    kind, w = _get_workload(namespace, name, cluster)
    if read_state(w) and read_state(w).get("phase") in ("creating", "copying", "copied"):
        raise DataServiceError("a migration is in progress for this service", 409)
    c = _container(w.spec.template.spec)
    if not c:
        raise DataServiceError("not a supported data service", 422)
    engine = detect_engine(c.image)
    repo, old_tag = parse_image(c.image)
    if engine in DB_ENGINES:
        old_major, new_major = major_of(engine, old_tag), major_of(engine, tag)
        if old_major is None or new_major is None:
            raise DataServiceError(f"cannot tell the major version from tag '{tag}'; use a numeric tag, e.g. {'16.4' if engine == 'postgres' else '8.0.36'}")
        if old_major != new_major:
            raise DataServiceError(f"major version change {old_major} → {new_major} needs a migration (the data volume of one major "
                                   "version cannot be opened by another): use «Migrate» instead of «Change version»")
    new_image = c.image.rsplit("@", 1)[0]
    new_image = (new_image.rsplit(":", 1)[0] if ":" in new_image.rsplit("/", 1)[-1] else new_image) + ":" + tag
    result = {"name": name, "engine": engine, "from": c.image, "to": new_image, "backup_job": None}
    if backup and engine in DB_ENGINES and kind == "StatefulSet":
        result["backup_job"] = start_backup(namespace, w, c, engine, cluster)
    patch = {"spec": {"template": {"spec": {"containers": [{"name": c.name, "image": new_image}]}}}}
    apps = kubeclient.apps_v1_api(cluster)
    if kind == "StatefulSet":
        apps.patch_namespaced_stateful_set(name, namespace, patch)
    else:
        apps.patch_namespaced_deployment(name, namespace, patch)
    return result


def start_backup(namespace: str, w, c: client.V1Container, engine: str, cluster: Optional[str]) -> dict:
    """Дамп всей БД в новый том `<name>-dump-<время>` (хранится, пока его не удалят). Возвращает имя задания и тома."""
    core = kubeclient.core_v1_api(cluster)
    svc = find_service(core, namespace, w)
    if not svc:
        raise DataServiceError("no Service found for this database: cannot reach it to make a dump", 409)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    pvc_name, job_name = f"{w.metadata.name}-dump-{ts}", f"{w.metadata.name}-backup-{ts}"
    size = "5Gi"
    for t in (w.spec.volume_claim_templates or []):
        size = ((t.spec.resources.requests or {}).get("storage")) or size
        break
    core.create_namespaced_persistent_volume_claim(namespace, client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name=pvc_name, labels={LABEL_JOB: "backup", LABEL_INSTANCE: w.metadata.name}),
        spec=client.V1PersistentVolumeClaimSpec(access_modes=["ReadWriteOnce"], resources=client.V1VolumeResourceRequirements(requests={"storage": size}))))
    cmd = dump_command(engine, svc.metadata.name, ENGINES[engine]["port"]) + " > /dump/dump.sql"
    job = _job(job_name, namespace, client_image(engine, c), ["sh", "-c", "set -e; " + env_prelude(engine) + cmd], credentials_env(engine, c),
               {LABEL_JOB: "backup", LABEL_INSTANCE: w.metadata.name}, volumes=[("dump", pvc_name, "/dump")], env_from=c.env_from)
    kubeclient.batch_v1_api(cluster).create_namespaced_job(namespace, job)
    return {"job": job_name, "volume": pvc_name}


def client_image(engine: str, c: client.V1Container, tag: Optional[str] = None) -> str:
    """Образ клиента нужной версии: pg_dump/psql — из образа `postgres:<мажорная>`, mysql/mariadb — из того же репозитория."""
    _, t = parse_image(c.image)
    spec = ENGINES[engine]
    if tag is not None:
        t = tag
    if engine == "postgres":
        return f"postgres:{major_of(engine, t) or 'latest'}"
    return f"{spec['client_repo']}:{t}"


def dump_command(engine: str, host: str, port: int) -> str:
    if engine == "postgres":
        return f"pg_dumpall -h {host} -p {port}"
    return f"mysqldump -h {host} -P {port} --all-databases --single-transaction --routines --events --triggers"


def wait_script(engine: str, *hosts: str) -> str:
    """Ждёт, пока серверы принимают соединения: готовность пода без проверки готовности не означает, что БД уже запустилась."""
    port = ENGINES[engine]["port"]
    ping = (lambda h: f"pg_isready -q -h {h} -p {port}") if engine == "postgres" else (lambda h: f"mysqladmin --connect-timeout=2 -h {h} -P {port} ping >/dev/null 2>&1")
    return "".join(f"n=0; until {ping(h)}; do n=$((n+1)); [ $n -gt 150 ] && echo 'server {h} is not accepting connections' && exit 1; sleep 2; done; "
                   for h in hosts)


_MYSQL_SYSTEM_USERS = "'root','mysql.sys','mysql.session','mysql.infoschema','mariadb.sys','mysql','debian-sys-maint'"


def mysql_copy_script(src: str, dst: str, port: int) -> str:
    """MySQL/MariaDB: базы пользователя (без служебных схем mysql, sys, performance_schema: их восстановление поверх живого сервера
    зависает на блокировке метаданных и ломает служебные таблицы новой версии) и учётные записи с правами (SHOW CREATE USER / SHOW GRANTS)."""
    return (wait_script("mysql", src, dst) + f"""
DBS=$(mysql -h {src} -P {port} -N -B -e "SHOW DATABASES" | grep -Ev '^(mysql|information_schema|performance_schema|sys)$' | tr '\\n' ' ')
: > /tmp/users.sql
mysql -h {src} -P {port} -N -B -e "SELECT user, host FROM mysql.user WHERE user NOT IN ({_MYSQL_SYSTEM_USERS}) AND user <> ''" | while IFS="$(printf '\\t')" read -r u h; do
  mysql -h {src} -P {port} -N -B -e "SHOW CREATE USER '$u'@'$h'" | sed 's/$/;/' >> /tmp/users.sql
  mysql -h {src} -P {port} -N -B -e "SHOW GRANTS FOR '$u'@'$h'" | sed 's/$/;/' >> /tmp/users.sql
done
{{ if [ -n "$DBS" ]; then mysqldump -h {src} -P {port} --databases $DBS --single-transaction --routines --events --triggers; fi; cat /tmp/users.sql; }} 2>/tmp/dump.err | mysql -h {dst} -P {port} >/dev/null 2>/tmp/restore.err
cat /tmp/dump.err /tmp/restore.err
if grep -ih 'error\\|fatal\\|could not' /tmp/dump.err /tmp/restore.err | grep -iv 'already exists'; then echo COPY-FAILED; exit 1; fi
""")


def copy_script(engine: str, src_host: str, dst_host: str) -> str:
    """Дамп → восстановление потоком. Ошибки «already exists» (служебные роли и базы нового экземпляра) допустимы, любые другие — сбой."""
    port = ENGINES[engine]["port"]
    if engine in ("mysql", "mariadb"):
        return mysql_copy_script(src_host, dst_host, port)
    return (wait_script(engine, src_host, dst_host) +
            f"{{ {dump_command(engine, src_host, port)} 2>/tmp/dump.err; }} | {restore_command(engine, dst_host, port)} >/dev/null 2>/tmp/restore.err; "
            "cat /tmp/dump.err /tmp/restore.err; "
            "if grep -ih 'error\\|fatal\\|could not' /tmp/dump.err /tmp/restore.err | grep -iv 'already exists'; then echo COPY-FAILED; exit 1; fi")


def restore_command(engine: str, host: str, port: int) -> str:
    if engine == "postgres":
        return f"psql -h {host} -p {port} -q postgres"
    return f"mysql -h {host} -P {port}"


def _job(name: str, namespace: str, image: str, command: list[str], env: list, labels: dict, volumes=None, env_from=None) -> client.V1Job:
    vols, mounts = [], []
    for vname, pvc, path in volumes or []:
        vols.append(client.V1Volume(name=vname, persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=pvc)))
        mounts.append(client.V1VolumeMount(name=vname, mount_path=path))
    container = client.V1Container(
        name="work", image=image, command=command, env=env, env_from=deepcopy(env_from) if env_from else None, volume_mounts=mounts or None,
        security_context=client.V1SecurityContext(allow_privilege_escalation=False, capabilities=client.V1Capabilities(drop=["ALL"])),
        resources=client.V1ResourceRequirements(requests={"cpu": "50m", "memory": "64Mi"}, limits={"memory": "1Gi"}))
    return client.V1Job(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=labels),
        spec=client.V1JobSpec(backoff_limit=0, ttl_seconds_after_finished=7 * 86400,
                              template=client.V1PodTemplateSpec(
                                  metadata=client.V1ObjectMeta(labels=labels),
                                  spec=client.V1PodSpec(restart_policy="Never", containers=[container], volumes=vols or None))))


# ---------- миграция БД рядом ----------

def _new_names(name: str, target_tag: str) -> tuple[str, str]:
    suffix = re.sub(r"[^a-z0-9]+", "-", target_tag.lower()).strip("-")[:20] or "next"
    base = name[:30]
    return f"{base}-{suffix}", f"{base}-{suffix}-copy"


def _save_state(apps, kind: str, namespace: str, name: str, state: dict) -> None:
    patch = {"metadata": {"annotations": {ANNOTATION: json.dumps(state, separators=(",", ":"))}}}
    (apps.patch_namespaced_stateful_set if kind == "StatefulSet" else apps.patch_namespaced_deployment)(name, namespace, patch)


def start_migration(namespace: str, name: str, target_tag: str, cluster: Optional[str] = None, stop_app: bool = False) -> dict:
    """Шаг 1: поднимает новый экземпляр БД (отдельный StatefulSet+том+Service). Дальше ход ведёт advance()."""
    validate_tag(target_tag)
    kind, w = _get_workload(namespace, name, cluster)
    if kind != "StatefulSet":
        raise DataServiceError("migration is supported for databases running as a StatefulSet", 422)
    st = read_state(w)
    if st and st.get("phase") in ("creating", "copying", "copied"):
        raise DataServiceError("a migration is already in progress for this service", 409)
    c = _container(w.spec.template.spec)
    engine = detect_engine(c.image) if c else None
    if engine not in DB_ENGINES:
        raise DataServiceError("migration of this engine is not supported yet; supported: " + ", ".join(DB_ENGINES), 422)
    if (w.spec.replicas or 0) != 1:
        raise DataServiceError("the database must run as one replica (scale it to 1) before migration", 409)
    repo, old_tag = parse_image(c.image)
    old_major, new_major = major_of(engine, old_tag), major_of(engine, target_tag)
    if old_major is None or new_major is None:
        raise DataServiceError("cannot tell the major version from the tag; use a numeric tag, e.g. 17 or 8.4")
    if _major_key(new_major) <= _major_key(old_major):
        raise DataServiceError(f"target version {new_major} must be newer than the current {old_major} (downgrade is not supported: "
                               "use a backup restore)")
    core, apps = kubeclient.core_v1_api(cluster), kubeclient.apps_v1_api(cluster)
    svc = find_service(core, namespace, w)
    if not svc or svc.spec.cluster_ip == "None":
        raise DataServiceError("no regular (non-headless) Service selects this database: nothing to switch over", 409)
    credentials_env(engine, c)                                  # заранее: есть ли пароль в окружении
    new_name, job_name = _new_names(name, target_tag)
    if kind and _exists(apps.read_namespaced_stateful_set, new_name, namespace):
        raise DataServiceError(f"{new_name} already exists: finish or roll back the previous migration first", 409)
    new_sts = _clone_statefulset(w, c, new_name, target_tag, svc.spec.selector)
    apps.create_namespaced_stateful_set(namespace, new_sts)
    next_svc = client.V1Service(
        metadata=client.V1ObjectMeta(name=new_name, labels={LABEL_INSTANCE: new_name}),
        spec=client.V1ServiceSpec(selector={LABEL_INSTANCE: new_name}, ports=deepcopy(svc.spec.ports)))
    core.create_namespaced_service(namespace, next_svc)
    state = {"id": uuid.uuid4().hex[:8], "kind": "db", "engine": engine, "phase": "creating", "from_tag": old_tag, "to_tag": target_tag,
             "new_sts": new_name, "new_svc": new_name, "job": job_name, "service": svc.metadata.name,
             "old_selector": dict(svc.spec.selector or {}), "stop_app": bool(stop_app), "scaled": {},
             "started": datetime.now(timezone.utc).isoformat(timespec="seconds"), "message": "starting the new instance"}
    _save_state(apps, kind, namespace, name, state)
    return state


def _exists(read, name: str, namespace: str) -> bool:
    try:
        read(name, namespace)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


def _clone_statefulset(w, c: client.V1Container, new_name: str, target_tag: str, service_selector: Optional[dict] = None) -> client.V1StatefulSet:
    new = client.V1StatefulSet(api_version="apps/v1", kind="StatefulSet")
    spec = deepcopy(w.spec)
    spec.replicas = 1
    # метки, по которым старый Service находит поды, у нового экземпляра меняем: иначе живой Service начнёт отдавать трафик и в новый, ещё пустой экземпляр
    renamed = {k: f"{v}-{new_name}"[:63].rstrip("-.") for k, v in (service_selector or {}).items()}
    selector_labels = {k: renamed.get(k, v) for k, v in (spec.selector.match_labels or {}).items()}
    spec.selector.match_labels = {**selector_labels, LABEL_INSTANCE: new_name}
    spec.template.metadata.labels = {**{k: renamed.get(k, v) for k, v in (spec.template.metadata.labels or {}).items()}, LABEL_INSTANCE: new_name}
    for cc in spec.template.spec.containers:
        if cc.name == c.name:
            base = cc.image.split("@", 1)[0]
            cc.image = (base.rsplit(":", 1)[0] if ":" in base.rsplit("/", 1)[-1] else base) + ":" + target_tag
    spec.service_name = w.spec.service_name
    spec.volume_claim_templates = deepcopy(w.spec.volume_claim_templates)
    new.metadata = client.V1ObjectMeta(name=new_name, labels={**{k: v for k, v in (w.metadata.labels or {}).items()
                                                                  if not k.startswith("splitwave.io/")}, LABEL_INSTANCE: new_name})
    new.spec = spec
    return new


def _app_deployments(apps, ns: str) -> list:
    return [d for d in apps.list_namespaced_deployment(ns).items if is_managed(d.metadata.labels)]


def _scale_apps(apps, ns: str, state: dict, down: bool) -> None:
    if down:
        for d in _app_deployments(apps, ns):
            if (d.spec.replicas or 0) > 0:
                state["scaled"][d.metadata.name] = d.spec.replicas
                apps.patch_namespaced_deployment_scale(d.metadata.name, ns, {"spec": {"replicas": 0}})
    else:
        for dname, n in list(state["scaled"].items()):
            try:
                apps.patch_namespaced_deployment_scale(dname, ns, {"spec": {"replicas": n}})
            except ApiException as e:
                if e.status != 404:
                    raise
        state["scaled"] = {}


def advance(namespace: str, name: str, cluster: Optional[str] = None) -> Optional[dict]:
    """Продвигает автомат миграции: ждёт готовности нового экземпляра, запускает копирование, следит за заданием."""
    kind, w = _get_workload(namespace, name, cluster)
    st = read_state(w)
    if not st:
        return None
    apps, core, batch = kubeclient.apps_v1_api(cluster), kubeclient.core_v1_api(cluster), kubeclient.batch_v1_api(cluster)
    phase = st["phase"]
    if st.get("kind") == "s3":
        return _advance_s3(namespace, name, kind, st, apps, core, batch)
    if phase == "creating":
        new = apps.read_namespaced_stateful_set(st["new_sts"], namespace)
        if (new.status.ready_replicas or 0) < 1:
            st["message"] = "waiting for the new instance to become ready"
        else:
            if st["stop_app"]:
                _scale_apps(apps, namespace, st, down=True)
            c = _container(w.spec.template.spec)
            engine = st["engine"]
            cmd = copy_script(engine, st["service"], st["new_svc"])
            env = credentials_env(engine, c)
            job = _job(st["job"], namespace, client_image(engine, c, st["to_tag"]), ["sh", "-c", env_prelude(engine) + cmd], env,
                       {LABEL_JOB: "copy", LABEL_INSTANCE: name}, env_from=c.env_from)
            batch.create_namespaced_job(namespace, job)
            st.update(phase="copying", message="copying data to the new instance")
    elif phase == "copying":
        j = batch.read_namespaced_job(st["job"], namespace)
        if (j.status.succeeded or 0) >= 1:
            st.update(phase="copied", message="data copied; ready to switch over")
        elif (j.status.failed or 0) >= 1:
            st.update(phase="failed", message="copy job failed: see the job log; the old instance is untouched")
            if st["scaled"]:
                _scale_apps(apps, namespace, st, down=False)
    _save_state(apps, kind, namespace, name, st)
    return st


def job_log(namespace: str, job: str, cluster: Optional[str] = None, tail: int = 80) -> str:
    core = kubeclient.core_v1_api(cluster)
    pods = core.list_namespaced_pod(namespace, label_selector=f"job-name={job}").items
    if not pods:
        return ""
    try:
        return core.read_namespaced_pod_log(pods[0].metadata.name, namespace, tail_lines=tail)
    except ApiException:
        return ""


def cutover(namespace: str, name: str, cluster: Optional[str] = None) -> dict:
    """Переключает существующий Service на новый экземпляр, останавливает старый (том остаётся для отката)."""
    kind, w = _get_workload(namespace, name, cluster)
    st = read_state(w)
    if not st or st.get("kind") != "db" or st["phase"] != "copied":
        raise DataServiceError("nothing to switch over: the data copy has not finished", 409)
    apps, core = kubeclient.apps_v1_api(cluster), kubeclient.core_v1_api(cluster)
    _replace_selector(core, namespace, st["service"], {LABEL_INSTANCE: st["new_sts"]})
    apps.patch_namespaced_stateful_set_scale(name, namespace, {"spec": {"replicas": 0}})
    _scale_apps(apps, namespace, st, down=False)
    st.update(phase="switched", message="switched to the new instance; the old one is stopped and kept for rollback")
    _save_state(apps, kind, namespace, name, st)
    return st


def _replace_selector(core, ns: str, svc_name: str, selector: dict) -> None:
    """Полная замена селектора (merge-патч дописывает ключи, а нужно именно заменить)."""
    s = core.read_namespaced_service(svc_name, ns)
    s.spec.selector = selector
    core.replace_namespaced_service(svc_name, ns, s)


def rollback(namespace: str, name: str, cluster: Optional[str] = None) -> dict:
    """Откат: до переключения — убирает новый экземпляр и копирующее задание; после — возвращает Service на старый и запускает его."""
    kind, w = _get_workload(namespace, name, cluster)
    st = read_state(w)
    if not st or st.get("kind") != "db" or st["phase"] in ("rolled_back", "finalized"):
        raise DataServiceError("nothing to roll back", 409)
    apps, core, batch = kubeclient.apps_v1_api(cluster), kubeclient.core_v1_api(cluster), kubeclient.batch_v1_api(cluster)
    if st["phase"] == "switched":
        apps.patch_namespaced_stateful_set_scale(name, namespace, {"spec": {"replicas": 1}})
        _replace_selector(core, namespace, st["service"], st["old_selector"])
    _delete_quiet(batch.delete_namespaced_job, st["job"], namespace, propagation_policy="Background")
    _delete_quiet(core.delete_namespaced_service, st["new_svc"], namespace)
    _delete_quiet(apps.delete_namespaced_stateful_set, st["new_sts"], namespace)
    for t in _pvc_names(w):
        _delete_quiet(core.delete_namespaced_persistent_volume_claim, f"{t}-{st['new_sts']}-0", namespace)
    _scale_apps(apps, namespace, st, down=False)
    st.update(phase="rolled_back", message="rolled back; the old instance serves traffic")
    _save_state(apps, kind, namespace, name, st)
    return st


def finalize(namespace: str, name: str, cluster: Optional[str] = None) -> dict:
    """Необратимо удаляет старый экземпляр и его том (после успешного переключения и проверки приложения)."""
    kind, w = _get_workload(namespace, name, cluster)
    st = read_state(w)
    if not st or st.get("kind") != "db" or st["phase"] != "switched":
        raise DataServiceError("finalize is possible only after a switch-over", 409)
    apps, core = kubeclient.apps_v1_api(cluster), kubeclient.core_v1_api(cluster)
    pvcs = [f"{t}-{name}-0" for t in _pvc_names(w)]
    _delete_quiet(apps.delete_namespaced_stateful_set, name, namespace)
    _delete_quiet(core.delete_namespaced_service, st["new_svc"], namespace)      # служебный Service копирования: основной Service уже смотрит на новый экземпляр
    for p in pvcs:
        _delete_quiet(core.delete_namespaced_persistent_volume_claim, p, namespace)
    return {**st, "phase": "finalized", "message": "the old instance and its volume were removed"}


def _delete_quiet(call, name: str, namespace: str, **kw) -> None:
    try:
        call(name, namespace, **kw)
    except ApiException as e:
        if e.status != 404:
            raise


# ---------- копирование S3 между хранилищами (rclone) ----------

def start_s3_copy(namespace: str, name: str, dest: dict, cluster: Optional[str] = None) -> dict:
    """Копирует все бакеты из S3-хранилища проекта в другое S3 (rclone copy + check, источник не изменяется).
    Ключи приёмника кладутся во временный Secret задания, а не в аннотации и не в журнал."""
    kind, w = _get_workload(namespace, name, cluster)
    c = _container(w.spec.template.spec)
    engine = detect_engine(c.image) if c else None
    if ENGINES.get(engine, {}).get("category") != "s3":
        raise DataServiceError("source is not an S3 storage", 422)
    st = read_state(w)
    if st and st.get("phase") in ("creating", "copying"):
        raise DataServiceError("a copy is already in progress for this service", 409)
    endpoint = str(dest.get("endpoint") or "")
    if not re.match(r"^https?://[A-Za-z0-9.-]+(:\d+)?(/.*)?$", endpoint):
        raise DataServiceError("destination endpoint must look like http://host:port or https://host")
    for key in ("access_key", "secret_key"):
        if not isinstance(dest.get(key), str) or not dest[key] or len(dest[key]) > 256:
            raise DataServiceError(f"destination {key} is required")
    core, apps, batch = kubeclient.core_v1_api(cluster), kubeclient.apps_v1_api(cluster), kubeclient.batch_v1_api(cluster)
    svc = find_service(core, namespace, w)
    if not svc:
        raise DataServiceError("no Service found for this storage", 409)
    src = credentials_env(engine, c)
    if not src and c.env_from:
        src = [None, None]                       # ключи источника придут через envFrom, переназначит env_prelude
    elif len(src) < 2:
        raise DataServiceError("access keys of the source are not set as container variables", 409)
    secret_name = f"{name}-s3copy-{uuid.uuid4().hex[:6]}"
    core.create_namespaced_secret(namespace, client.V1Secret(
        metadata=client.V1ObjectMeta(name=secret_name, labels={LABEL_JOB: "s3copy"}),
        string_data={"access": dest["access_key"], "secret": dest["secret_key"]}))
    provider = dest.get("provider") or "Other"
    env = [e for e in (_env_as(src[0], "RCLONE_CONFIG_SRC_ACCESS_KEY_ID"), _env_as(src[1], "RCLONE_CONFIG_SRC_SECRET_ACCESS_KEY")) if e] + [
           client.V1EnvVar(name="RCLONE_CONFIG_SRC_TYPE", value="s3"),
           client.V1EnvVar(name="RCLONE_CONFIG_SRC_PROVIDER", value=ENGINES[engine]["rclone_provider"]),
           client.V1EnvVar(name="RCLONE_CONFIG_SRC_ENDPOINT", value=f"http://{svc.metadata.name}:{ENGINES[engine]['port']}"),
           client.V1EnvVar(name="RCLONE_CONFIG_DST_TYPE", value="s3"),
           client.V1EnvVar(name="RCLONE_CONFIG_DST_PROVIDER", value=str(provider)[:30]),
           client.V1EnvVar(name="RCLONE_CONFIG_DST_ENDPOINT", value=endpoint),
           client.V1EnvVar(name="RCLONE_CONFIG_DST_ACCESS_KEY_ID", value_from=client.V1EnvVarSource(
               secret_key_ref=client.V1SecretKeySelector(name=secret_name, key="access"))),
           client.V1EnvVar(name="RCLONE_CONFIG_DST_SECRET_ACCESS_KEY", value_from=client.V1EnvVarSource(
               secret_key_ref=client.V1SecretKeySelector(name=secret_name, key="secret")))]
    job_name = f"{name}-s3copy-{uuid.uuid4().hex[:6]}"
    batch.create_namespaced_job(namespace, _job(
        job_name, namespace, RCLONE_IMAGE,
        ["sh", "-c", env_prelude(engine) + _RCLONE_SCRIPT],
        env, {LABEL_JOB: "s3copy", LABEL_INSTANCE: name}, env_from=c.env_from))
    state = {"id": uuid.uuid4().hex[:8], "kind": "s3", "engine": engine, "phase": "copying", "job": job_name, "secret": secret_name,
             "dest_endpoint": endpoint, "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "message": "copying buckets (rclone copy, then rclone check)"}
    _save_state(apps, kind, namespace, name, state)
    return state


def _advance_s3(namespace: str, name: str, kind: str, st: dict, apps, core, batch) -> dict:
    if st["phase"] == "copying":
        j = batch.read_namespaced_job(st["job"], namespace)
        if (j.status.succeeded or 0) >= 1:
            st.update(phase="copied", message="buckets copied and verified; point the application at the new endpoint "
                                              "(S3_ENDPOINT and keys in the project secrets) and redeploy")
        elif (j.status.failed or 0) >= 1:
            st.update(phase="failed", message="copy failed: see the job log; the source is untouched")
        if st["phase"] != "copying":
            _delete_quiet(core.delete_namespaced_secret, st["secret"], namespace)   # ключи приёмника больше не нужны
        _save_state(apps, kind, namespace, name, st)
    return st
