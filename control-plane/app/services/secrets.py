"""Хранение секретов проекта и доставка их в кластер при деплое.

Правило: значение секрета выходит из системы ТОЛЬКО в Kubernetes Secret при деплое.
Ни один API-эндпоинт, лог или запись аудита значения не содержит."""
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from kubernetes.client.exceptions import ApiException
from sqlalchemy.orm import Session

from kubernetes import client

from app.config import settings
from app.db.models import Environment, Project, Secret, SecretVersion
from app.services import crypto, kubeclient

logger = logging.getLogger(__name__)

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_VALUE_BYTES = 64 * 1024
HASH_ANNOTATION = "platform.split-wave.com/secrets-hash"


def validate_key(key: str) -> None:
    if not KEY_RE.match(key):
        raise ValueError("key must be a valid env var name (letters, digits, underscore; not starting with a digit)")


def validate_value(value: str) -> None:
    if len(value.encode()) > MAX_VALUE_BYTES:
        raise ValueError(f"value is larger than {MAX_VALUE_BYTES} bytes")


ALL = "*"  # область "все среды проекта"
MANAGED_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY = "splitwave"
LEGACY_MANAGED_BY = ("devsecops-platform",)             # прежнее имя проекта: ресурсы, созданные старыми версиями, считаются своими


def is_managed(labels) -> bool:
    """Создан ли ресурс этой платформой (в том числе версией до переименования)."""
    return (labels or {}).get(MANAGED_LABEL) in (MANAGED_BY, *LEGACY_MANAGED_BY)


UNSET = object()  # «поле не передавали» — отличается от None («сбросить»)


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _snapshot(db: Session, project: Project, row: Secret, actor: str, reason: str) -> None:
    db.add(SecretVersion(project_id=project.id, scope=row.scope, key=row.key, version=row.version,
                         encrypted_value=row.encrypted_value, created_by=actor, reason=reason,
                         created_at=datetime.now(timezone.utc)))
    db.flush()
    keep = max(settings.secret_versions_keep, 1)
    stale = (db.query(SecretVersion).filter_by(project_id=project.id, scope=row.scope, key=row.key)
             .order_by(SecretVersion.version.desc()).offset(keep).all())
    for v in stale:
        db.delete(v)


def set_secret(db: Session, project: Project, key: str, value: str, scope: str = ALL, actor: str = "system",
               expires_at=UNSET, rotation_days=UNSET, reason: str = "set", _encrypted: Optional[bytes] = None) -> bool:
    """Создаёт или обновляет секрет в области scope ("*" или имя среды). Каждое значение сохраняется как версия.
    Возвращает True, если создан новый."""
    validate_key(key)
    validate_value(value)
    if expires_at is not UNSET and expires_at is not None and _utc(expires_at) <= datetime.now(timezone.utc):
        raise ValueError("expires_at must be in the future")
    if rotation_days is not UNSET and rotation_days is not None and not 1 <= rotation_days <= 3650:
        raise ValueError("rotation_days must be between 1 and 3650")
    existing = db.query(Secret).filter_by(project_id=project.id, scope=scope, key=key).first()
    encrypted = _encrypted or crypto.encrypt_value(value)
    created = existing is None
    if created:
        existing = Secret(project_id=project.id, scope=scope, key=key, encrypted_value=encrypted, version=1)
        db.add(existing)
    else:
        existing.encrypted_value = encrypted
        existing.version = (existing.version or 1) + 1
    existing.updated_by = actor
    existing.updated_at = datetime.now(timezone.utc)
    if expires_at is not UNSET:
        existing.expires_at = _utc(expires_at)
    if rotation_days is not UNSET:
        existing.rotation_days = rotation_days
    db.flush()
    _snapshot(db, project, existing, actor, reason)
    db.commit()
    return created


def set_policy(db: Session, project: Project, key: str, scope: str, expires_at, rotation_days) -> Optional[Secret]:
    row = db.query(Secret).filter_by(project_id=project.id, scope=scope, key=key).first()
    if not row:
        return None
    if expires_at is not None and _utc(expires_at) <= datetime.now(timezone.utc):
        raise ValueError("expires_at must be in the future")
    if rotation_days is not None and not 1 <= rotation_days <= 3650:
        raise ValueError("rotation_days must be between 1 and 3650")
    row.expires_at = _utc(expires_at)
    row.rotation_days = rotation_days
    db.commit()
    return row


def list_versions(db: Session, project: Project, key: str, scope: str) -> Optional[list[dict]]:
    current = db.query(Secret).filter_by(project_id=project.id, scope=scope, key=key).first()
    if not current:
        return None
    rows = (db.query(SecretVersion).filter_by(project_id=project.id, scope=scope, key=key)
            .order_by(SecretVersion.version.desc()).all())
    return [{"version": v.version, "created_at": _utc(v.created_at).isoformat(), "created_by": v.created_by,
             "reason": v.reason, "current": v.version == current.version} for v in rows]


def restore_version(db: Session, project: Project, key: str, scope: str, version: int, actor: str) -> Optional[int]:
    """Возвращает значение прошлой версии как НОВУЮ версию (история не переписывается). None — версии нет."""
    old = db.query(SecretVersion).filter_by(project_id=project.id, scope=scope, key=key, version=version).first()
    if not old:
        return None
    value = crypto.decrypt_value(old.encrypted_value)
    set_secret(db, project, key, value, scope, actor=actor, reason="restore", _encrypted=crypto.rotate_value(old.encrypted_value))
    return db.query(Secret).filter_by(project_id=project.id, scope=scope, key=key).one().version


def secret_status(row: Secret, now: Optional[datetime] = None) -> str:
    """ok | expired | expiring | rotation_due (в порядке тяжести)."""
    now = now or datetime.now(timezone.utc)
    exp = _utc(row.expires_at)
    if exp and exp <= now:
        return "expired"
    if exp and exp <= now + timedelta(days=settings.secret_expiry_warn_days):
        return "expiring"
    upd = _utc(row.updated_at)
    if row.rotation_days and upd and upd + timedelta(days=row.rotation_days) <= now:
        return "rotation_due"
    return "ok"


def delete_secret(db: Session, project: Project, key: str, scope: str = ALL) -> bool:
    existing = db.query(Secret).filter_by(project_id=project.id, scope=scope, key=key).first()
    if not existing:
        return False
    db.query(SecretVersion).filter_by(project_id=project.id, scope=scope, key=key).delete()
    db.delete(existing)
    db.commit()
    return True


def _effective(rows: list[Secret], environment: Optional[str]) -> dict[str, Secret]:
    """Действующий набор для среды: общие значения, поверх них значения самой среды."""
    result: dict[str, Secret] = {}
    for r in rows:
        if r.scope == ALL:
            result[r.key] = r
    if environment:
        for r in rows:
            if r.scope == environment:
                result[r.key] = r
    return result


def list_secrets(db: Session, project: Project, environment: Optional[str] = None) -> list[dict]:
    """Без environment — все записи с их областями; с environment — действующий набор этой среды."""
    rows = db.query(Secret).filter_by(project_id=project.id).order_by(Secret.key, Secret.scope).all()
    chosen = list(rows) if environment is None else sorted(_effective(rows, environment).values(), key=lambda r: r.key)
    now = datetime.now(timezone.utc)
    return [{"key": r.key, "scope": r.scope, "inherited": r.scope == ALL, "key_version": r.key_version,
             "version": r.version, "updated_by": r.updated_by, "status": secret_status(r, now),
             "expires_at": _utc(r.expires_at).isoformat() if r.expires_at else None, "rotation_days": r.rotation_days,
             "updated_at": r.updated_at.isoformat() if r.updated_at else None} for r in chosen]


def decrypt_all(db: Session, project: Project, environment: Optional[str] = None) -> dict[str, str]:
    rows = db.query(Secret).filter_by(project_id=project.id).all()
    return {k: crypto.decrypt_value(r.encrypted_value) for k, r in _effective(rows, environment).items()}


def drop_environment_secrets(db: Session, project: Project, environment: str) -> int:
    db.query(SecretVersion).filter_by(project_id=project.id, scope=environment).delete()
    n = db.query(Secret).filter_by(project_id=project.id, scope=environment).delete()
    db.commit()
    return n


def secret_name(project: Project, environment: Environment) -> str:
    # Имя намеренно отличается от вручную созданных "<slug>-secrets": платформа владеет только своим объектом.
    # Среда входит в имя: несколько сред одного проекта могут жить в одном namespace.
    return f"{project.slug}-{environment.name}-platform-env"


def _digest(data: dict[str, str]) -> str:
    h = hashlib.sha256()
    for k in sorted(data):
        h.update(k.encode() + b"\0" + data[k].encode() + b"\0")
    return h.hexdigest()


def sync_to_cluster(db: Session, project: Project, environment: Environment) -> int:
    """Создаёт/обновляет Kubernetes Secret проекта и подключает его к Deployment через envFrom.
    Аннотация с хешем в шаблоне пода перезапускает поды, когда значения изменились.
    Возвращает число синхронизированных ключей."""
    data = decrypt_all(db, project, environment.name)
    if not data:
        return 0

    name = secret_name(project, environment)
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(name=name, namespace=environment.namespace,
                                     labels={MANAGED_LABEL: MANAGED_BY}),
        type="Opaque",
        string_data=data,
    )
    cluster = environment.cluster
    core = kubeclient.core_v1_api(cluster) if cluster else kubeclient.core_v1_api()
    try:
        core.create_namespaced_secret(environment.namespace, body)
    except ApiException as e:
        if e.status != 409:
            raise
        existing = core.read_namespaced_secret(name, environment.namespace)
        labels = (existing.metadata.labels or {}) if existing.metadata else {}
        if not is_managed(labels):
            # Секрет с таким именем создан не платформой — не перезаписываем чужие данные.
            raise RuntimeError(f"secret {environment.namespace}/{name} exists and is not managed by the platform")
        core.replace_namespaced_secret(name, environment.namespace, body)

    apps = kubeclient.apps_v1_api(cluster) if cluster else kubeclient.apps_v1_api()
    deployment = kubeclient.serialize(apps.read_namespaced_deployment(environment.deployment_name, environment.namespace))
    containers = deployment["spec"]["template"]["spec"]["containers"]
    container = next((c for c in containers if c["name"] == environment.container_name), None)
    if container is None:
        raise RuntimeError(f"container {environment.container_name} not found in deployment {environment.deployment_name}")
    env_from = list(container.get("envFrom") or [])
    if not any((ef.get("secretRef") or {}).get("name") == name for ef in env_from):
        env_from.append({"secretRef": {"name": name}})

    apps.patch_namespaced_deployment(environment.deployment_name, environment.namespace, {
        "spec": {"template": {
            "metadata": {"annotations": {HASH_ANNOTATION: _digest(data)}},
            "spec": {"containers": [{"name": environment.container_name, "envFrom": env_from}]},
        }}
    })
    logger.info("Secrets of %s synced to %s/%s (%d keys)", project.slug, environment.namespace, name, len(data))
    return len(data)
