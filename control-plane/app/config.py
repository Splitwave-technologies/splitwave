import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "splitwave-control-plane"
    github_webhook_secret: str = ""
    default_namespace: str = "default"
    kaniko_image: str = "gcr.io/kaniko-project/executor:v1.23.2"
    kaniko_git_image: str = "alpine/git:2.45.2"
    kaniko_registry_secret: str = "ghcr-creds"       # Secret dockerconfigjson с доступом на запись в реестр образов
    kaniko_insecure_registries: str = ""              # реестры без TLS (через запятую), например registry.registry.svc:5000
    kaniko_cpu: str = "1"
    kaniko_memory: str = "2Gi"
    kaniko_memory_limit: str = "4Gi"
    build_namespace: str = ""   # где собираются образы сред удалённых кластеров (по умолчанию default_namespace)
    default_builder: str = "default"
    registry_prefix: str = "ghcr.io/splitwave-technologies"
    build_timeout_seconds: int = 900
    connection_check_allow_loopback: bool = False  # проверка подключений: loopback запрещён (защита от SSRF); включается только в тестах
    connection_check_timeout_seconds: int = 5
    connection_check_per_minute: int = 30          # лимит проверок в минуту на пользователя
    rollout_wait_enabled: bool = True            # после выката ждать, пока приложение реально поднимется (иначе релиз «deployed» бывает при падающем приложении)
    rollout_timeout_seconds: int = 300           # сколько ждать готовности всех реплик
    rollout_poll_seconds: float = 3
    build_poll_interval_seconds: int = 10

    database_url: str = "postgresql+psycopg2://platform:platform@localhost:5432/platform"
    # Ключи шифрования секретов (Fernet). Несколько ключей через запятую: первый шифрует,
    # все расшифровывают — так делается ротация без простоя.
    secret_encryption_key: str = ""
    # Токен первого администратора (создаётся при старте, если в БД ещё нет ни одного токена API).
    admin_bootstrap_token: str = ""

    # Лицензия платных функций (см. app/licensing.py). Пусто = редакция Community.
    license_key: str = ""
    license_file: str = ""          # альтернатива: путь к файлу с ключом
    license_public_key: str = ""    # переопределяет встроенный открытый ключ (тесты, свой издатель)

    # Учётные записи и сессии
    require_2fa: bool = False          # True: пользователь без 2FA может только настроить её (после входа)
    session_ttl_hours: int = 12        # абсолютный срок сессии
    session_idle_minutes: int = 60     # сессия истекает после стольких минут без активности
    public_url: str = ""               # внешний адрес платформы (для команды установки агента); пусто — по запросу
    trusted_proxies: str = ""         # сети CIDR через запятую: от них принимаются X-Forwarded-For/-Proto (например 10.42.0.0/16)
    cookie_secure: bool | None = None  # None: Secure ставится, если запрос пришёл по https
    scrypt_log2_n: int = 15            # стоимость хеширования паролей (тесты понижают)
    login_max_failures: int = 5        # неудачных входов на имя за окно, после чего временная блокировка
    login_window_minutes: int = 15

    secret_versions_keep: int = 20     # сколько прошлых значений секрета хранить
    secret_expiry_warn_days: int = 14  # за сколько дней до срока секрет считается «скоро истекает»

    # Уведомления
    notify_allow_private_targets: bool = False   # True: разрешить адреса во внутренних сетях (по умолчанию защита от SSRF)
    notify_allow_http: bool = False              # True: разрешить http:// (по умолчанию только https)
    notify_timeout_seconds: int = 8
    scm_allow_private_hosts: bool = False        # мастер подключения: True — разрешить Git-серверы во внутренних сетях (самохостинг GitLab/Gitea)
    scm_allow_http: bool = False                 # True — разрешить http:// (только для испытательных стендов)
    scm_timeout_seconds: int = 8
    update_check: bool = True                    # раз в сутки читать публичный файл последнего выпуска; UPDATE_CHECK=false выключает (ничего об установке не отправляется)
    update_url: str = "https://split-wave.com/releases.json"
    tier_limits_enforce: bool = True             # лимиты проектов/сред по редакции лицензии (выключается только в тестах и стендах)
    # Токены чтения для публичных хостов (github.com / gitlab.com): без них у GitHub лимит 60 запросов/час на адрес платформы.
    # Используются ТОЛЬКО для разбора репозитория на этих хостах, никогда не отправляются на другие серверы и не сохраняются в проектах.
    scm_github_token: str = ""
    scm_gitlab_token: str = ""
    # Мастер подключения: что создаётся в кластере для нового приложения
    platform_namespace: str = ""                 # где работает платформа (пусто: из ServiceAccount или default_namespace)
    platform_service_account: str = "control-plane-sa"
    deployer_cluster_role: str = "control-plane-deployer"   # ClusterRole, которую мастер выдаёт платформе в namespace приложения (Helm: <релиз>-deployer)
    placeholder_image: str = "registry.k8s.io/pause:3.10"   # образ-заглушка до первого выката
    app_pull_secret: str = ""                    # Secret доступа к реестру в namespace платформы: копируется в namespace приложения
    ingress_class: str = ""                      # пусто — класс по умолчанию кластера
    ingress_tls: str = "none"                    # HTTPS для Ingress приложений: none | auto (Let's Encrypt через резолвер Traefik) | custom (свой сертификат: TLSStore Traefik или секрет)
    ingress_cert_resolver: str = "le"            # имя резолвера ACME в Traefik (для ingress_tls=auto)
    ingress_tls_secret: str = ""                 # секрет kubernetes.io/tls в namespace приложения (для контроллеров без общего хранилища сертификатов)
    ingress_annotations: str = ""                # JSON-объект дополнительных аннотаций Ingress (например для nginx/cert-manager)
    notify_lang: str = "ru"                      # ru | en — язык текста уведомлений
    checks_interval_minutes: int = 60            # периодические проверки (срок секретов, цепочка аудита); 0 — выключено

    approval_ttl_hours: int = 24       # заявка на согласование действует столько часов

    class Config:
        env_file = ".env"


def apply_license_guard(s: "Settings", env=None) -> "Settings":
    """Лимиты редакций — часть функциональности лицензионного ключа; по лицензии (Elastic License 2.0) их нельзя отключать или обходить.
    Выключить их можно только в тестовых стендах, и только вместе с SPLITWAVE_TEST_MODE=1."""
    if not s.tier_limits_enforce and (os.environ if env is None else env).get("SPLITWAVE_TEST_MODE") != "1":
        s.tier_limits_enforce = True
    return s


settings = apply_license_guard(Settings())

