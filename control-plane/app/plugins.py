"""Подключение закрытого модуля platform_ee (платные функции), если он установлен и лицензия действительна."""
import importlib
import logging

from app.licensing import current_license

logger = logging.getLogger(__name__)

# Способы входа, которые подключают платные модули (SSO): [{"id", "name", "url"}]. Показываются на странице входа.
LOGIN_PROVIDERS: list[dict] = []


# Поставщик клиентов удалённых кластеров (платный модуль multi_cluster): функция (имя) -> kubernetes.client.ApiClient.
CLUSTER_PROVIDER = None


def set_cluster_provider(fn) -> None:
    global CLUSTER_PROVIDER
    CLUSTER_PROVIDER = fn


def cluster_names() -> list[str]:
    return list(CLUSTER_PROVIDER.names()) if CLUSTER_PROVIDER and hasattr(CLUSTER_PROVIDER, "names") else []


# Обработчик событий PR/MR всех провайдеров (платная функция preview_envs): функция (событие scm.parse_pull_request, background_tasks) -> dict.
PULL_REQUEST_HANDLER = None


def set_pull_request_handler(fn) -> None:
    global PULL_REQUEST_HANDLER
    PULL_REQUEST_HANDLER = fn


# Цель выката «сервер с агентом» (платная функция servers). Объект с методами:
#   names() -> list[str]; deploy(db, project, env, image) — ждёт отчёта агента; sync_secrets(db, project, env) -> int;
#   runtime(env) -> dict; remove(db, project, env)
SERVER_TARGET = None


def set_server_target(obj) -> None:
    global SERVER_TARGET
    SERVER_TARGET = obj


# Управление командой (платная функция teams): несколько учётных записей, роли, участники проектов. Объект с методами:
#   list_users(principal); create_user(body, principal); update_user(user_id, body, principal); delete_user(user_id, principal);
#   list_members(slug, principal); set_member(slug, username, role, principal); remove_member(slug, username, principal);
#   resolve_session(user) -> (роль, {project_id: роль участника})
# Без него действует одна встроенная учётная запись администратора.
TEAMS = None


def set_teams(obj) -> None:
    global TEAMS
    TEAMS = obj


# Несколько сред на проект (платная функция environments). Объект с методами:
#   create(db, project, body, principal) -> Environment (проверяет предел редакции);
#   check_new_project(count) — можно ли создать проект сразу с несколькими средами;
#   route_branch(db, project, branch) -> list[Environment] — какие среды собирать и выкатывать при push в ветку.
# Без него у проекта одна среда: вторую создать нельзя, при push выкатывается только она.
ENVIRONMENTS = None


def set_environments(obj) -> None:
    global ENVIRONMENTS
    ENVIRONMENTS = obj


# Несколько проектов (платная функция projects). Объект с методами:
#   check_new(db) — можно ли создать ещё один проект (предел редакции);
#   routed(db, project) -> bool — выкатывается ли проект автоматически по push.
# Без него платформа ведёт один проект: второй создать нельзя, а лишние проекты (если они остались от лицензии) по push не выкатываются.
PROJECTS = None


def set_projects(obj) -> None:
    global PROJECTS
    PROJECTS = obj


def register_login_provider(provider_id: str, name: str, url: str) -> None:
    LOGIN_PROVIDERS[:] = [p for p in LOGIN_PROVIDERS if p["id"] != provider_id] + [{"id": provider_id, "name": name, "url": url}]


def load_enterprise(app) -> bool:
    try:
        module = importlib.import_module("platform_ee")
    except ModuleNotFoundError:
        return False
    lic = current_license()
    if not lic.valid:
        logger.warning("platform_ee is installed but the license is %s — paid features are disabled", lic.status)
        return False
    if module.register(app, lic) is False:         # платный модуль сам проверил лицензию (встроенным ключом) и отказался
        logger.warning("platform_ee refused to start: its own license check failed — paid features are disabled")
        return False
    logger.info("platform_ee loaded (tier=%s, customer=%s)", lic.tier, lic.customer)
    return True
