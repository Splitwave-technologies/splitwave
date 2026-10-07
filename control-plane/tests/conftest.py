import os
import tempfile

from cryptography.fernet import Fernet

# Настройки задаются ДО импорта приложения (Settings читается при импорте); .env проекта не участвует.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["SECRET_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["ADMIN_BOOTSTRAP_TOKEN"] = "bootstrap-token-for-tests-0123456789"
os.environ["GITHUB_WEBHOOK_SECRET"] = "whsec"
os.environ["CHECKS_INTERVAL_MINUTES"] = "0"
os.environ["ROLLOUT_WAIT_ENABLED"] = "false"      # общие тесты не ходят в кластер; ожидание готовности проверяется в test_rollout.py
os.environ["SPLITWAVE_TEST_MODE"] = "1"
os.environ.setdefault("UPDATE_CHECK", "false")
os.environ["TIER_LIMITS_ENFORCE"] = "false"      # общие тесты создают много проектов; лимиты проверяются отдельно (test_limits.py)
os.environ["SCRYPT_LOG2_N"] = "10"   # быстрые тесты: реальная стоимость задаётся в настройках

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from kubernetes import client as k8s  # noqa: E402
from kubernetes.client.exceptions import ApiException  # noqa: E402

from app import auth  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.db.base import Base, SessionLocal, engine  # noqa: E402
from app.db.models import Environment, Project  # noqa: E402
from app.main import app  # noqa: E402



@event.listens_for(engine, "connect")
def _sqlite_foreign_keys(dbapi_connection, _):
    # как в Postgres: нарушение внешнего ключа = ошибка (по умолчанию SQLite их не проверяет)
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


ADMIN = {"Authorization": "Bearer bootstrap-token-for-tests-0123456789"}


@pytest.fixture
def sent(monkeypatch):
    """Перехватывает исходящие уведомления: список (url, тело, заголовки); ответ 200."""
    from app.services import notifications
    out = []
    monkeypatch.setattr(notifications, "_post", lambda url, body, headers: (out.append((url, body, headers)), 200)[1])
    monkeypatch.setattr(notifications, "_resolve", lambda host: ["93.184.216.34"])
    return out


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    from app.services import notifications
    monkeypatch.setattr(notifications, "SYNC", True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    auth.bootstrap_admin(db)
    db.close()
    from app.services import instance
    instance.reset_cache()             # идентификатор установки живёт в базе, а база пересоздаётся для каждого теста
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def admin():
    return ADMIN


@pytest.fixture
def make_token(client):
    def _make(role, name=None):
        r = client.post("/api/tokens", json={"name": name or f"{role}-tester", "role": role}, headers=ADMIN)
        assert r.status_code == 201, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}
    return _make


@pytest.fixture
def project():
    db = SessionLocal()
    p = Project(slug="demo", repo_full_name="acme/demo", sub_path="", builder="default", registry_prefix="reg/acme")
    db.add(p)
    db.flush()
    db.add(Environment(project_id=p.id, name="prod", namespace="apps", deployment_name="demo",
                       container_name="demo", branch="main", auto_deploy=True))
    db.commit()
    db.close()
    return "demo"


class FakeCore:
    def __init__(self):
        self.secrets = {}
        self.pods = []
        self.logs = {}
        self.missing_pod = False

    def create_namespaced_secret(self, namespace, body):
        key = (namespace, body.metadata.name)
        if key in self.secrets:
            raise ApiException(status=409)
        self.secrets[key] = body

    def list_namespaced_pod(self, namespace, label_selector=None):
        return k8s.V1PodList(items=self.pods)

    def read_namespaced_pod(self, name, namespace):
        if self.missing_pod:
            raise ApiException(status=404)
        return k8s.V1Pod(metadata=k8s.V1ObjectMeta(name=name), spec=k8s.V1PodSpec(
            init_containers=[k8s.V1Container(name="prepare"), k8s.V1Container(name="build")],
            containers=[k8s.V1Container(name="completion")]))

    def read_namespaced_pod_log(self, name, namespace, container=None, tail_lines=None):
        return self.logs.get(container, f"log of {container}")

    def read_namespaced_secret(self, name, namespace):
        return self.secrets[(namespace, name)]

    def replace_namespaced_secret(self, name, namespace, body):
        self.secrets[(namespace, name)] = body


class FakeApps:
    def __init__(self):
        self.patches = []

    def read_namespaced_deployment(self, name, namespace):
        return k8s.V1Deployment(spec=k8s.V1DeploymentSpec(
            replicas=2,
            selector=k8s.V1LabelSelector(match_labels={"app": name}),
            template=k8s.V1PodTemplateSpec(spec=k8s.V1PodSpec(
                containers=[k8s.V1Container(name=name, image="old")]))),
            status=k8s.V1DeploymentStatus(replicas=2, ready_replicas=1, updated_replicas=2, available_replicas=1,
                                          conditions=[k8s.V1DeploymentCondition(type="Available", status="False", reason="MinimumReplicasUnavailable")]))

    def patch_namespaced_deployment(self, name, namespace, patch):
        self.patches.append((name, namespace, patch))


@pytest.fixture
def fake_k8s(monkeypatch):
    core, apps = FakeCore(), FakeApps()
    monkeypatch.setattr("app.services.kubeclient.core_v1_api", lambda cluster=None: core)
    monkeypatch.setattr("app.services.kubeclient.apps_v1_api", lambda cluster=None: apps)
    monkeypatch.setattr("app.services.deploy.apps_v1_api", lambda cluster=None: apps)
    return core, apps


class _TestEnvironments:
    """Замена платного модуля environments в тестах ядра (настоящий лежит в закрытом репозитории и проверяется там)."""
    def check_new_project(self, count):
        pass

    def create(self, db, project, body, principal):
        from fastapi import HTTPException
        from app.routers.projects import _target_fields
        from app.services import project_repo
        if project_repo.get_environment(db, project, body.name):
            raise HTTPException(status_code=409, detail=f"environment {body.name} already exists")
        env = Environment(project_id=project.id, name=body.name, namespace=body.namespace, deployment_name=body.deployment_name,
                          container_name=body.container_name, branch=body.branch, auto_deploy=body.auto_deploy,
                          require_approval=body.require_approval, **_target_fields(body))
        db.add(env)
        db.commit()
        from app.services import audit
        audit.log_event(db, principal.name, "env_create", project.id, env.id, {"name": body.name, "namespace": body.namespace})
        return env

    def route_branch(self, db, project, branch):
        return db.query(Environment).filter_by(project_id=project.id, branch=branch).order_by(Environment.name).all()


@pytest.fixture(autouse=True)
def multi_environments(monkeypatch):
    from app import plugins
    monkeypatch.setattr(plugins, "ENVIRONMENTS", _TestEnvironments())


@pytest.fixture
def single_environment(monkeypatch):
    """Как без платного модуля: у проекта одна среда."""
    from app import plugins
    monkeypatch.setattr(plugins, "ENVIRONMENTS", None)


class _TestProjects:
    """Замена платного модуля projects в тестах ядра (настоящий лежит в закрытом репозитории и проверяется там)."""
    def check_new(self, db):
        pass

    def routed(self, db, project):
        return True


@pytest.fixture(autouse=True)
def multi_projects(monkeypatch):
    from app import plugins
    monkeypatch.setattr(plugins, "PROJECTS", _TestProjects())


@pytest.fixture
def single_project(monkeypatch):
    """Как без платного модуля: один проект."""
    from app import plugins
    monkeypatch.setattr(plugins, "PROJECTS", None)
