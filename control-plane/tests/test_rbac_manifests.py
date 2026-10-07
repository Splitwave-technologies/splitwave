"""Права платформы в манифестах должны покрывать то, что она реально делает в namespace приложения.
Регрессия: сборка по Dockerfile под минимальными правами падала (нет jobs/status; в namespace-access.yaml не было ни заданий, ни ServiceAccount),
а e2e этого не видели, потому что шли под администратором кластера."""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]            # корень репозитория (в образе платной редакции каталога deploy нет — тест пропускается)
APP_NS_NEEDS = [                                       # (группа API, ресурс, глагол)
    ("kpack.io", "images", "get"), ("kpack.io", "images", "create"), ("kpack.io", "images", "patch"), ("kpack.io", "builds", "list"),
    ("apps", "deployments", "get"), ("apps", "deployments", "patch"),
    ("", "secrets", "get"), ("", "secrets", "create"), ("", "secrets", "patch"),
    ("", "pods", "list"), ("", "pods/log", "get"), ("", "pods", "get"),
    ("", "serviceaccounts", "get"), ("", "serviceaccounts", "patch"),
    ("batch", "jobs", "create"), ("batch", "jobs", "get"), ("batch", "jobs", "list"), ("batch", "jobs", "delete"), ("batch", "jobs/status", "get"),
]


def rules_of(path: Path, kind: str) -> list[dict]:
    docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
    return [r for d in docs if d.get("kind") == kind for r in d.get("rules", [])]


def allows(rules, group, resource, verb) -> bool:
    return any(group in r.get("apiGroups", []) and resource in r.get("resources", []) and verb in r.get("verbs", []) for r in rules)


@pytest.mark.parametrize("file,kind", [("deploy/platform/namespace-access.yaml", "ClusterRole"), ("deploy/kpack/control-plane-rbac.yaml", "Role")])
def test_manual_install_manifests_cover_what_the_platform_does(file, kind):
    path = ROOT / file
    if not path.exists():
        pytest.skip("манифесты развёртывания не входят в этот образ")
    rules = rules_of(path, kind)
    missing = [n for n in APP_NS_NEEDS if not allows(rules, *n)]
    # deployments create нужен только мастеру/превью и выдаётся отдельно; здесь проверяем остальное
    assert not missing, f"{file}: не хватает прав {missing}"


def test_helm_templates_grant_job_status_everywhere():
    path = ROOT / "deploy/helm/splitwave/templates/rbac.yaml"
    if not path.exists():
        pytest.skip("чарт не входит в этот образ")
    text = path.read_text()
    assert text.count('resources: ["jobs"]') == text.count('resources: ["jobs/status"]') == 2        # Role и ClusterRole
