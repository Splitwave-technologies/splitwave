import uuid
from datetime import datetime

from sqlalchemy import true as sa_true
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, LargeBinary, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("provider", "repo_full_name", name="uq_project_provider_repo"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    repo_full_name: Mapped[str] = mapped_column(String, nullable=False)
    # Где лежит код: github | gitlab | bitbucket | gitea. git_url — полный адрес клонирования (обязателен для самохостинга и Gitea).
    provider: Mapped[str] = mapped_column(String, nullable=False, default="github", server_default="github")
    git_url: Mapped[str] = mapped_column(String, nullable=True)
    git_username: Mapped[str] = mapped_column(String, nullable=True)
    git_token_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=True)   # токен доступа к приватному репозиторию (зашифрован)
    sub_path: Mapped[str] = mapped_column(String, default="")
    # Как собирать: buildpacks (kpack, без Dockerfile) или dockerfile (kaniko по Dockerfile из репозитория).
    build_method: Mapped[str] = mapped_column(String, nullable=False, default="buildpacks", server_default="buildpacks")
    dockerfile_path: Mapped[str] = mapped_column(String, nullable=False, default="Dockerfile", server_default="Dockerfile")
    # Dockerfile, хранимый платформой (предложен мастером, правится пользователем): если задан, сборка использует его вместо файла из репозитория.
    dockerfile_content: Mapped[str] = mapped_column(Text, nullable=True)
    builder: Mapped[str] = mapped_column(String, default="default")
    registry_prefix: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def clone_url(self) -> str:
        from app.services import scm
        return scm.default_clone_url(self.provider, self.repo_full_name, self.git_url)

    environments: Mapped[list["Environment"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    secrets: Mapped[list["Secret"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    members: Mapped[list["ProjectMember"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    approvals: Mapped[list["Approval"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Environment(Base):
    __tablename__ = "environments"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_environment_project_name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    namespace: Mapped[str] = mapped_column(String, nullable=False)
    deployment_name: Mapped[str] = mapped_column(String, nullable=False)
    container_name: Mapped[str] = mapped_column(String, nullable=False)
    branch: Mapped[str] = mapped_column(String, default="main")
    auto_deploy: Mapped[bool] = mapped_column(Boolean, default=True)
    # True: выкат и откат этой среды выполняются только после согласования другим человеком
    require_approval: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Имя зарегистрированного удалённого кластера (платная функция multi_cluster); None — кластер, в котором работает платформа.
    # Сборка всегда идёт в домашнем кластере (namespace сборки), выкат, секреты и состояние — в кластере среды.
    cluster: Mapped[str] = mapped_column(String, nullable=True)
    # Номер pull request для временной среды (платная функция preview_envs); у обычных сред None.
    preview_of: Mapped[str] = mapped_column(String, nullable=True)
    # Имя подключённого сервера (VPS, сервер в ЦОД, сервер партнёра; платная функция servers): приложение запускается в Docker на нём
    # через агента. Тогда deployment_name — имя контейнера, cluster не задаётся. runtime — параметры контейнера (порты, тома, проверка).
    server: Mapped[str] = mapped_column(String, nullable=True)
    runtime: Mapped[dict] = mapped_column(JSON, nullable=True)
    # Порт приложения (мастер подключения: Service/Ingress и повторная выдача манифеста); у старых сред None.
    app_port: Mapped[int] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def image_name(self) -> str:
        """Имя объекта kpack Image: у обычных сред — slug проекта, у временных отдельное, чтобы сборки PR не мешали основным."""
        return f"{self.project.slug}-{self.name}" if self.preview_of else self.project.slug

    @property
    def build_namespace(self) -> str:
        from app.config import settings
        return settings.build_namespace or settings.default_namespace if (self.cluster or self.server) else self.namespace

    project: Mapped["Project"] = relationship(back_populates="environments")
    releases: Mapped[list["Release"]] = relationship(
        back_populates="environment", cascade="all, delete-orphan"
    )
    approvals: Mapped[list["Approval"]] = relationship(back_populates="environment", cascade="all, delete-orphan")


class Release(Base):
    __tablename__ = "releases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("environments.id"), nullable=False
    )
    image_digest: Mapped[str] = mapped_column(String, nullable=True)
    git_revision: Mapped[str] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    triggered_by: Mapped[str] = mapped_column(String, default="api")
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    environment: Mapped["Environment"] = relationship(back_populates="releases")


class Secret(Base):
    __tablename__ = "secrets"
    __table_args__ = (UniqueConstraint("project_id", "scope", "key", name="uq_secret_project_scope_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    # "*" — значение для всех сред проекта; иначе имя среды (значение среды перекрывает общее)
    scope: Mapped[str] = mapped_column(String, nullable=False, default="*", server_default="*")
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")  # версия значения
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    rotation_days: Mapped[int] = mapped_column(Integer, nullable=True)  # напоминать о смене раз в столько дней
    updated_by: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    project: Mapped["Project"] = relationship(back_populates="secrets")


class SecretVersion(Base):
    """История значений секрета (зашифрованных). Хранится по (проект, область, имя) и не зависит от строки Secret."""

    __tablename__ = "secret_versions"
    __table_args__ = (UniqueConstraint("project_id", "scope", "key", "version", name="uq_secret_version"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_by: Mapped[str] = mapped_column(String, nullable=True)
    reason: Mapped[str] = mapped_column(String, nullable=False, default="set")  # set | restore
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("environments.id"), nullable=True
    )
    actor: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Цепочка целостности: seq — сквозной номер, hash = HMAC(prev_hash + содержимое). См. services/audit.py.
    seq: Mapped[int] = mapped_column(BigInteger, nullable=True, unique=True)
    prev_hash: Mapped[str] = mapped_column(String, nullable=True)
    hash: Mapped[str] = mapped_column(String, nullable=True)
    # Пояснения, добавляемые позже (например, slug удалённого проекта); в хеш НЕ входят.
    context: Mapped[dict] = mapped_column(JSON, nullable=True)


class ApiToken(Base):
    """Токен доступа к API. Сам токен не хранится — только SHA-256 хеш (токены длинные и случайные)."""

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)  # admin | devops | developer | viewer
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    token_prefix: Mapped[str] = mapped_column(String, nullable=False)  # первые символы — чтобы узнать токен в списке
    # если задан — токен действует только в этом проекте (для CI одного проекта)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped["Project"] = relationship(foreign_keys=[project_id])

    @property
    def project_slug(self):
        return self.project.slug if self.project else None


class User(Base):
    """Локальная учётная запись. role — глобальная роль (admin|devops|developer|viewer|none);
    none = доступ только к проектам, где пользователь состоит участником."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, default="none")
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=True)       # зашифрован ключом секретов
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_last_counter: Mapped[int] = mapped_column(nullable=True)                # защита от повторного использования кода
    recovery_codes: Mapped[list] = mapped_column(JSON, nullable=True)            # SHA-256 хеши неиспользованных кодов
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # local — вход по паролю платформы; иначе имя внешнего провайдера (oidc), а external_id — его идентификатор пользователя
    auth_source: Mapped[str] = mapped_column(String, nullable=False, default="local", server_default="local")
    external_id: Mapped[str] = mapped_column(String, nullable=True)

    sessions: Mapped[list["UserSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    memberships: Mapped[list["ProjectMember"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    csrf_token: Mapped[str] = mapped_column(String, nullable=False)
    restricted: Mapped[str] = mapped_column(String, nullable=True)   # "totp_setup" | "password_change": пока не выполнено — доступ только к /api/auth
    ip: Mapped[str] = mapped_column(String, nullable=True)
    user_agent: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="sessions")


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("user_id", "project_id", name="uq_member_user_project"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)        # devops | developer | viewer
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="memberships")
    project: Mapped["Project"] = relationship(back_populates="members")


class LoginAttempt(Base):
    """Попытки входа — для временной блокировки перебора паролей."""

    __tablename__ = "login_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String, nullable=False, index=True)   # "user:<имя>" или "ip:<адрес>"
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class Approval(Base):
    """Заявка на выкат/откат в среде, требующей согласования. Решает другой человек с правом отката."""

    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    environment_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("environments.id"), nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)                 # redeploy | rollback
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)    # что именно будет сделано (снимок на момент заявки)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")  # pending|approved|denied|cancelled|expired|executed|failed
    requested_by: Mapped[str] = mapped_column(String, nullable=False)
    requester_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=True)   # id пользователя/токена; None — вебхук
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by: Mapped[str] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(String, nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=True)

    project: Mapped["Project"] = relationship(back_populates="approvals")
    environment: Mapped["Environment"] = relationship(back_populates="approvals")


class NotificationChannel(Base):
    """Канал уведомлений: webhook, Slack или Telegram. Адрес и токены хранятся зашифрованно."""

    __tablename__ = "notification_channels"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # webhook | slack | telegram
    config_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)  # JSON: url / token / chat_id / secret
    target_hint: Mapped[str] = mapped_column(String, nullable=False, default="")   # безопасная подпись (хост), без секретов
    events: Mapped[list] = mapped_column(JSON, nullable=False)                     # список действий аудита или ["*"]
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    created_by: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str] = mapped_column(String, nullable=True)  # ok | error
    last_error: Mapped[str] = mapped_column(String, nullable=True)


class PluginState(Base):
    """Небольшое хранилище ключ → JSON для платных модулей (настройки интеграций, курсоры), чтобы им не нужны свои миграции."""

    __tablename__ = "plugin_state"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Server(Base):
    """Сервер, подключённый через агента (исходящее соединение агента с платформой; входящих портов на сервере не нужно)."""

    __tablename__ = "servers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    enroll_hash: Mapped[str] = mapped_column(String, nullable=True, index=True)      # SHA-256 одноразового токена подключения
    enroll_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_hash: Mapped[str] = mapped_column(String, nullable=True, index=True)       # SHA-256 постоянного токена агента
    registry_auth_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=True)     # учётные данные реестра образов (зашифрованы)
    info: Mapped[dict] = mapped_column(JSON, nullable=True)                          # что сообщил агент: ОС, Docker, ресурсы, контейнеры
    agent_version: Mapped[str] = mapped_column(String, nullable=True)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    jobs: Mapped[list["ServerJob"]] = relationship(back_populates="server", cascade="all, delete-orphan")


class ServerJob(Base):
    """Задание агенту: выкат контейнера, удаление. Параметры (с секретами) хранятся зашифрованно."""

    __tablename__ = "server_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    server_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("servers.id"), nullable=False, index=True)
    environment_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)                        # deploy | remove
    payload_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")   # pending | running | succeeded | failed | expired
    result: Mapped[dict] = mapped_column(JSON, nullable=True)
    requested_by: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    server: Mapped["Server"] = relationship(back_populates="jobs")
