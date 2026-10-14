import copy

import pytest
from kubernetes import client as k8s
from kubernetes.client.rest import ApiException

from app.services import dataservices as ds

NS = "apps"


def _env(name, value=None, secret=None):
    if secret:
        return k8s.V1EnvVar(name=name, value_from=k8s.V1EnvVarSource(secret_key_ref=k8s.V1SecretKeySelector(name=secret, key=name)))
    return k8s.V1EnvVar(name=name, value=value)


def make_sts(name="db", image="postgis/postgis:16-3.5", replicas=1, labels=None):
    labels = labels or {"app": name}
    return k8s.V1StatefulSet(
        metadata=k8s.V1ObjectMeta(name=name, labels=dict(labels), annotations={}),
        spec=k8s.V1StatefulSetSpec(
            replicas=replicas, service_name=name, selector=k8s.V1LabelSelector(match_labels=dict(labels)),
            template=k8s.V1PodTemplateSpec(metadata=k8s.V1ObjectMeta(labels=dict(labels)), spec=k8s.V1PodSpec(containers=[
                k8s.V1Container(name="postgres", image=image, env=[_env("POSTGRES_USER", "app"), _env("POSTGRES_PASSWORD", secret="dbsec")])])),
            volume_claim_templates=[k8s.V1PersistentVolumeClaim(
                metadata=k8s.V1ObjectMeta(name="data"),
                spec=k8s.V1PersistentVolumeClaimSpec(resources=k8s.V1VolumeResourceRequirements(requests={"storage": "5Gi"})))]),
        status=k8s.V1StatefulSetStatus(replicas=replicas, ready_replicas=replicas))


def make_svc(name="db", selector=None, port=5432, cluster_ip="10.0.0.1"):
    return k8s.V1Service(metadata=k8s.V1ObjectMeta(name=name),
                         spec=k8s.V1ServiceSpec(selector=selector or {"app": name}, cluster_ip=cluster_ip, ports=[k8s.V1ServicePort(port=port)]))


class FakeCluster:
    """Минимальный кластер: StatefulSet'ы, Deployment'ы, Service'ы, Job'ы, PVC, Secret'ы и журнал вызовов."""

    def __init__(self):
        self.sts, self.deploy, self.svc, self.jobs, self.pvc, self.secrets = {}, {}, {}, {}, {}, {}
        self.scale = {}

    @staticmethod
    def _get(store, name):
        if name not in store:
            raise ApiException(status=404)
        return store[name]

    # apps
    def list_namespaced_stateful_set(self, ns): return k8s.V1StatefulSetList(items=list(self.sts.values()))
    def list_namespaced_deployment(self, ns): return k8s.V1DeploymentList(items=list(self.deploy.values()))
    def read_namespaced_stateful_set(self, name, ns): return self._get(self.sts, name)
    def read_namespaced_deployment(self, name, ns): return self._get(self.deploy, name)

    def create_namespaced_stateful_set(self, ns, body):
        body.status = k8s.V1StatefulSetStatus(replicas=1, ready_replicas=0)
        self.sts[body.metadata.name] = body

    def delete_namespaced_stateful_set(self, name, ns): self._get(self.sts, name); del self.sts[name]

    def patch_namespaced_stateful_set(self, name, ns, patch):
        w = self._get(self.sts, name)
        meta = patch.get("metadata", {})
        w.metadata.annotations = {**(w.metadata.annotations or {}), **meta.get("annotations", {})}
        for c in patch.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
            for cc in w.spec.template.spec.containers:
                if cc.name == c["name"]:
                    cc.image = c["image"]

    patch_namespaced_deployment = patch_namespaced_stateful_set

    def patch_namespaced_stateful_set_scale(self, name, ns, patch):
        self._get(self.sts, name).spec.replicas = patch["spec"]["replicas"]

    def patch_namespaced_deployment_scale(self, name, ns, patch):
        self._get(self.deploy, name).spec.replicas = patch["spec"]["replicas"]

    # core
    def list_namespaced_service(self, ns): return k8s.V1ServiceList(items=list(self.svc.values()))
    def read_namespaced_service(self, name, ns): return copy.deepcopy(self._get(self.svc, name))
    def create_namespaced_service(self, ns, body): self.svc[body.metadata.name] = body
    def delete_namespaced_service(self, name, ns): self._get(self.svc, name); del self.svc[name]
    def replace_namespaced_service(self, name, ns, body): self._get(self.svc, name); self.svc[name] = body
    def create_namespaced_persistent_volume_claim(self, ns, body): self.pvc[body.metadata.name] = body
    def delete_namespaced_persistent_volume_claim(self, name, ns): self._get(self.pvc, name); del self.pvc[name]
    def create_namespaced_secret(self, ns, body): self.secrets[body.metadata.name] = body
    def delete_namespaced_secret(self, name, ns): self._get(self.secrets, name); del self.secrets[name]
    def list_namespaced_pod(self, ns, label_selector=None): return k8s.V1PodList(items=[])

    # batch
    def create_namespaced_job(self, ns, body):
        body.status = k8s.V1JobStatus()
        self.jobs[body.metadata.name] = body
    def read_namespaced_job(self, name, ns): return self._get(self.jobs, name)
    def delete_namespaced_job(self, name, ns, propagation_policy=None): self._get(self.jobs, name); del self.jobs[name]


@pytest.fixture
def cluster(monkeypatch):
    c = FakeCluster()
    for fn in ("core_v1_api", "apps_v1_api", "batch_v1_api"):
        monkeypatch.setattr(f"app.services.kubeclient.{fn}", lambda cluster=None, c=c: c)
    return c


def test_image_parsing_and_engine_detection():
    assert ds.parse_image("registry.local:5000/postgis/postgis:16-3.5@sha256:abc") == ("postgis/postgis", "16-3.5")
    assert ds.parse_image("postgres") == ("postgres", "latest")
    assert ds.detect_engine("bitnamilegacy/minio:2025.7.23-debian-12-r3") == "minio"
    assert ds.detect_engine("docker.io/library/mariadb:11.4") == "mariadb"
    assert ds.detect_engine("nginx:1") is None
    assert ds.major_of("postgres", "16-3.5") == "16" and ds.major_of("mysql", "8.0.36") == "8.0" and ds.major_of("postgres", "latest") is None


def test_discover_finds_data_services_and_skips_platform_apps(cluster):
    cluster.sts["db"] = make_sts()
    cluster.svc["db"] = make_svc()
    app = make_sts("api", image="registry/apps/api:1", labels={"app.kubernetes.io/managed-by": "splitwave"})
    cluster.deploy["web"] = k8s.V1Deployment(metadata=k8s.V1ObjectMeta(name="web", labels={"app.kubernetes.io/managed-by": "splitwave"}),
                                             spec=app.spec.template and k8s.V1DeploymentSpec(replicas=1, selector=app.spec.selector, template=app.spec.template),
                                             status=k8s.V1DeploymentStatus())
    found = ds.discover(NS)
    assert [s["name"] for s in found] == ["db"]
    assert found[0]["engine"] == "postgres" and found[0]["major"] == "16" and found[0]["service"] == "db"
    assert found[0]["volumes"] == [{"name": "data", "size": "5Gi"}]


def test_change_version_minor_ok_major_refused(cluster):
    cluster.sts["db"] = make_sts()
    cluster.svc["db"] = make_svc()
    out = ds.change_version(NS, "db", "16-3.6")
    assert out["to"] == "postgis/postgis:16-3.6" and cluster.sts["db"].spec.template.spec.containers[0].image == "postgis/postgis:16-3.6"
    assert out["backup_job"] and out["backup_job"]["volume"] in cluster.pvc and out["backup_job"]["job"] in cluster.jobs
    with pytest.raises(ds.DataServiceError, match="needs a migration"):
        ds.change_version(NS, "db", "17-3.5")
    with pytest.raises(ds.DataServiceError, match="tag must"):
        ds.change_version(NS, "db", "bad tag; rm -rf")


def test_change_version_s3_has_no_backup_and_any_tag(cluster):
    cluster.sts["minio"] = make_sts("minio", image="bitnamilegacy/minio:2025.7.23-debian-12-r3")
    out = ds.change_version(NS, "minio", "2025.9.1-debian-12-r0")
    assert out["backup_job"] is None and out["to"].endswith(":2025.9.1-debian-12-r0")


def test_backup_job_does_not_carry_password_values(cluster):
    cluster.sts["db"] = make_sts()
    cluster.svc["db"] = make_svc()
    out = ds.change_version(NS, "db", "16.4")
    job = cluster.jobs[out["backup_job"]["job"]]
    env = {e.name: e for e in job.spec.template.spec.containers[0].env}
    assert env["PGPASSWORD"].value is None and env["PGPASSWORD"].value_from.secret_key_ref.name == "dbsec"
    assert env["PGUSER"].value == "app"


def test_migration_lifecycle_start_copy_cutover_rollback_and_finalize(cluster):
    cluster.sts["db"] = make_sts()
    cluster.svc["db"] = make_svc()
    cluster.deploy["api"] = k8s.V1Deployment(
        metadata=k8s.V1ObjectMeta(name="api", labels={"app.kubernetes.io/managed-by": "splitwave"}),
        spec=k8s.V1DeploymentSpec(replicas=2, selector=k8s.V1LabelSelector(match_labels={"a": "b"}),
                                  template=k8s.V1PodTemplateSpec(spec=k8s.V1PodSpec(containers=[k8s.V1Container(name="api", image="r/api:1")]))))

    st = ds.start_migration(NS, "db", "17-3.5", stop_app=True)
    new = st["new_sts"]
    assert st["phase"] == "creating" and new in cluster.sts and new in cluster.svc
    assert cluster.sts[new].spec.template.spec.containers[0].image == "postgis/postgis:17-3.5"
    assert cluster.sts[new].spec.selector.match_labels[ds.LABEL_INSTANCE] == new
    assert cluster.svc["db"].spec.selector == {"app": "db"}                                   # живой Service не тронут
    pod_labels = cluster.sts[new].spec.template.metadata.labels
    assert pod_labels["app"] != "db" and pod_labels[ds.LABEL_INSTANCE] == new                 # новые поды не попадают под селектор живого Service

    assert ds.advance(NS, "db")["phase"] == "creating"                                        # новый экземпляр ещё не готов
    assert not cluster.jobs
    cluster.sts[new].status.ready_replicas = 1
    st = ds.advance(NS, "db")
    assert st["phase"] == "copying" and st["job"] in cluster.jobs and cluster.deploy["api"].spec.replicas == 0
    job = cluster.jobs[st["job"]]
    assert job.spec.template.spec.containers[0].image == "postgres:17"
    assert "pg_dumpall -h db" in job.spec.template.spec.containers[0].command[2] and f"-h {new}" in job.spec.template.spec.containers[0].command[2]

    cluster.jobs[st["job"]].status.succeeded = 1
    assert ds.advance(NS, "db")["phase"] == "copied"

    st = ds.cutover(NS, "db")
    assert st["phase"] == "switched" and cluster.svc["db"].spec.selector == {ds.LABEL_INSTANCE: new}
    assert cluster.sts["db"].spec.replicas == 0 and cluster.deploy["api"].spec.replicas == 2

    st = ds.rollback(NS, "db")
    assert st["phase"] == "rolled_back" and cluster.svc["db"].spec.selector == {"app": "db"}
    assert cluster.sts["db"].spec.replicas == 1 and new not in cluster.sts and new not in cluster.svc
    assert f"data-{new}-0" not in cluster.pvc


def test_finalize_only_after_switch(cluster):
    cluster.sts["db"] = make_sts()
    cluster.svc["db"] = make_svc()
    cluster.pvc["data-db-0"] = k8s.V1PersistentVolumeClaim()
    ds.start_migration(NS, "db", "17")
    with pytest.raises(ds.DataServiceError, match="only after a switch-over"):
        ds.finalize(NS, "db")
    cluster.sts[ds._new_names("db", "17")[0]].status.ready_replicas = 1
    ds.advance(NS, "db")
    cluster.jobs[ds._new_names("db", "17")[1]].status.succeeded = 1
    ds.advance(NS, "db")
    ds.cutover(NS, "db")
    ds.finalize(NS, "db")
    assert "db" not in cluster.sts and "data-db-0" not in cluster.pvc


def test_failed_copy_leaves_old_instance_and_restores_the_app(cluster):
    cluster.sts["db"] = make_sts()
    cluster.svc["db"] = make_svc()
    cluster.deploy["api"] = k8s.V1Deployment(
        metadata=k8s.V1ObjectMeta(name="api", labels={"app.kubernetes.io/managed-by": "splitwave"}),
        spec=k8s.V1DeploymentSpec(replicas=1, selector=k8s.V1LabelSelector(match_labels={"a": "b"}),
                                  template=k8s.V1PodTemplateSpec(spec=k8s.V1PodSpec(containers=[k8s.V1Container(name="api", image="r/api:1")]))))
    st = ds.start_migration(NS, "db", "17", stop_app=True)
    cluster.sts[st["new_sts"]].status.ready_replicas = 1
    st = ds.advance(NS, "db")
    cluster.jobs[st["job"]].status.failed = 1
    st = ds.advance(NS, "db")
    assert st["phase"] == "failed" and cluster.deploy["api"].spec.replicas == 1 and cluster.sts["db"].spec.replicas == 1
    assert cluster.svc["db"].spec.selector == {"app": "db"}


@pytest.mark.parametrize("target,msg", [("16", "must be newer"), ("15", "must be newer"), ("latest", "major version"), ("bad tag", "tag must")])
def test_migration_rejects_bad_targets(cluster, target, msg):
    cluster.sts["db"] = make_sts()
    cluster.svc["db"] = make_svc()
    with pytest.raises(ds.DataServiceError, match=msg):
        ds.start_migration(NS, "db", target)


def test_migration_needs_one_replica_and_a_service(cluster):
    cluster.sts["db"] = make_sts(replicas=2)
    cluster.svc["db"] = make_svc()
    with pytest.raises(ds.DataServiceError, match="one replica"):
        ds.start_migration(NS, "db", "17")
    cluster.sts["db"] = make_sts()
    del cluster.svc["db"]
    with pytest.raises(ds.DataServiceError, match="Service"):
        ds.start_migration(NS, "db", "17")


def test_second_migration_is_refused_while_one_runs(cluster):
    cluster.sts["db"] = make_sts()
    cluster.svc["db"] = make_svc()
    ds.start_migration(NS, "db", "17")
    with pytest.raises(ds.DataServiceError, match="already in progress"):
        ds.start_migration(NS, "db", "18")
    with pytest.raises(ds.DataServiceError, match="in progress"):
        ds.change_version(NS, "db", "16.4")


def test_mysql_migration_uses_mysqldump(cluster):
    cluster.sts["my"] = k8s.V1StatefulSet(
        metadata=k8s.V1ObjectMeta(name="my", labels={"app": "my"}, annotations={}),
        spec=k8s.V1StatefulSetSpec(replicas=1, service_name="my", selector=k8s.V1LabelSelector(match_labels={"app": "my"}),
                                   template=k8s.V1PodTemplateSpec(metadata=k8s.V1ObjectMeta(labels={"app": "my"}), spec=k8s.V1PodSpec(containers=[
                                       k8s.V1Container(name="mysql", image="mysql:8.0.36", env=[_env("MYSQL_ROOT_PASSWORD", secret="s")])])),
                                   volume_claim_templates=[]),
        status=k8s.V1StatefulSetStatus(replicas=1, ready_replicas=1))
    cluster.svc["my"] = make_svc("my", port=3306)
    st = ds.start_migration(NS, "my", "8.4")
    cluster.sts[st["new_sts"]].status.ready_replicas = 1
    st = ds.advance(NS, "my")
    cmd = cluster.jobs[st["job"]].spec.template.spec.containers[0].command[2]
    assert "mysqldump -h my" in cmd and f"mysql -h {st['new_svc']}" in cmd
    assert cluster.jobs[st["job"]].spec.template.spec.containers[0].image == "mysql:8.4"


def test_s3_copy_keeps_destination_keys_out_of_state_and_removes_the_secret(cluster):
    cluster.sts["minio"] = make_sts("minio", image="bitnamilegacy/minio:2025.7.23-debian-12-r3")
    cluster.sts["minio"].spec.template.spec.containers[0].env = [_env("MINIO_ROOT_USER", "root"), _env("MINIO_ROOT_PASSWORD", secret="m")]
    cluster.svc["minio"] = make_svc("minio", port=9000)
    st = ds.start_s3_copy(NS, "minio", {"endpoint": "http://seaweed:8333", "access_key": "AK", "secret_key": "SK", "provider": "SeaweedFS"})
    assert "SK" not in str(st) and "SK" not in str(cluster.sts["minio"].metadata.annotations)
    assert len(cluster.secrets) == 1
    env = {e.name: e for e in cluster.jobs[st["job"]].spec.template.spec.containers[0].env}
    assert env["RCLONE_CONFIG_SRC_ENDPOINT"].value == "http://minio:9000"
    assert env["RCLONE_CONFIG_DST_SECRET_ACCESS_KEY"].value is None
    cluster.jobs[st["job"]].status.succeeded = 1
    assert ds.advance(NS, "minio")["phase"] == "copied" and not cluster.secrets


def test_s3_copy_rejects_bad_destination(cluster):
    cluster.sts["minio"] = make_sts("minio", image="minio/minio:RELEASE.2025")
    cluster.svc["minio"] = make_svc("minio", port=9000)
    with pytest.raises(ds.DataServiceError, match="endpoint"):
        ds.start_s3_copy(NS, "minio", {"endpoint": "file:///etc", "access_key": "a", "secret_key": "b"})


# ---------- HTTP ----------

def test_api_lists_services_and_requires_manage_for_changes(client, admin, project, make_token, cluster):
    cluster.sts["db"] = make_sts()
    cluster.svc["db"] = make_svc()
    r = client.get("/api/projects/demo/environments/prod/data-services", headers=admin)
    assert r.status_code == 200 and r.json()["services"][0]["engine"] == "postgres"
    viewer = make_token("viewer")
    assert client.post("/api/projects/demo/environments/prod/data-services/db/version", headers=viewer, json={"tag": "16.4"}).status_code == 403
    r = client.post("/api/projects/demo/environments/prod/data-services/db/version", headers=admin, json={"tag": "17"})
    assert r.status_code == 422 and "migration" in r.json()["detail"]
    assert client.get("/api/projects/demo/environments/prod/data-services/nope/migration", headers=admin).status_code == 404


def test_api_migration_flow_and_finalize_needs_the_name(client, admin, project, cluster):
    cluster.sts["db"] = make_sts()
    cluster.svc["db"] = make_svc()
    base = "/api/projects/demo/environments/prod/data-services/db"
    assert client.post(f"{base}/migrate", headers=admin, json={"target_tag": "17"}).json()["phase"] == "creating"
    assert client.post(f"{base}/migration/finalize", headers=admin, json={"confirm": "x"}).status_code == 422
    assert client.post(f"{base}/migration/cutover", headers=admin).status_code == 409
    assert client.post(f"{base}/migration/rollback", headers=admin).json()["phase"] == "rolled_back"
    events = {e["action"] for e in client.get("/api/audit", headers=admin).json()} if client.get("/api/audit", headers=admin).status_code == 200 else set()
    assert not events or "data_migration_start" in events
