from kubernetes.client.exceptions import ApiException

from app.config import settings
from app.services import kpack


class FakeCustom:
    def __init__(self, existing_tag=None):
        self.calls = []
        self.exists = existing_tag is not None
        self.tag = existing_tag

    def get_namespaced_custom_object(self, group, version, ns, plural, name):
        self.calls.append("get")
        if not self.exists:
            raise ApiException(status=404)
        return {"metadata": {"resourceVersion": "7"},
                "spec": {"tag": self.tag, "builder": {"name": "default"}, "source": {"git": {"revision": getattr(self, "revision", "abc123")}, "subPath": ""}}}

    def create_namespaced_custom_object(self, group, version, ns, plural, body):
        self.calls.append("create")
        self.exists, self.tag = True, body["spec"]["tag"]

    def replace_namespaced_custom_object(self, group, version, ns, plural, name, body):
        self.calls.append("replace")
        self.replaced_with = body

    def delete_namespaced_custom_object(self, group, version, ns, plural, name):
        self.calls.append("delete")
        self.exists = False


def _run(monkeypatch, fake, prefix="ghcr.io/acme"):
    monkeypatch.setattr(kpack, "custom_objects_api", lambda: fake)
    monkeypatch.setattr(settings, "registry_prefix", prefix)
    kpack.trigger_build("demo", "https://x/y.git", "abc123", "", "apps")


def _run_ret(monkeypatch, fake, revision="abc123", prefix="ghcr.io/acme"):
    monkeypatch.setattr(kpack, "custom_objects_api", lambda: fake)
    monkeypatch.setattr(settings, "registry_prefix", prefix)
    return kpack.trigger_build("demo", "https://x/y.git", revision, "", "apps")


def test_creates_image_when_missing(monkeypatch):
    fake = FakeCustom()
    _run(monkeypatch, fake)
    assert fake.calls == ["get", "create"]


def test_replaces_when_tag_unchanged(monkeypatch):
    fake = FakeCustom(existing_tag="ghcr.io/acme/demo")
    fake.revision = "old000"          # прежняя ревизия отличается от запрошенной -> нужна новая сборка
    _run(monkeypatch, fake)
    assert fake.calls == ["get", "replace"]
    assert fake.replaced_with["metadata"]["resourceVersion"] == "7"


def test_recreates_image_when_registry_changes(monkeypatch):
    fake = FakeCustom(existing_tag="docker.io/acme/demo")   # kpack не даёт менять spec.tag
    _run(monkeypatch, fake)
    assert fake.calls[0] == "get" and "delete" in fake.calls and fake.calls[-1] == "create"
    assert "replace" not in fake.calls
    assert fake.tag == "ghcr.io/acme/demo"


def test_same_revision_is_a_noop_and_new_revision_rebuilds(monkeypatch):
    fake = FakeCustom(existing_tag="ghcr.io/acme/demo")
    assert _run_ret(monkeypatch, fake, revision="abc123") is False          # тот же коммит: без замены и без сборки
    assert "replace" not in fake.calls
    fake2 = FakeCustom(existing_tag="ghcr.io/acme/demo")
    assert _run_ret(monkeypatch, fake2, revision="newsha1") is True          # другой коммит: замена -> сборка
    assert "replace" in fake2.calls
    assert _run_ret(monkeypatch, FakeCustom(), revision="x") is True         # нет Image: создаём
