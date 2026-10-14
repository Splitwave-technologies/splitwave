# Data services: change versions and migrate databases and S3 storages

The project page has a **Data services** card. It finds the databases and S3 storages that run in the namespace of the environment and lets you change their version or move their data without leaving the panel.

Recognised: PostgreSQL (postgres, postgis, pgvector, timescaledb, Bitnami images), MySQL, MariaDB, Redis/Valkey, MinIO, SeaweedFS. Anything else in the namespace is ignored. Applications deployed by SplitWave are not listed.

## Turning it on

The platform needs extra rights in the namespace (StatefulSet, Service, volume claims, scale):

- Helm: `--set rbac.dataServices=true` (together with `rbac.deployNamespaces`).
- Manual install: `sed 's/NAMESPACE/my-app/g' deploy/platform/namespace-access-dataservices.yaml | kubectl apply -f -`.

Without these rights the card shows the error from the cluster and nothing is changed. Every action needs the `projects:manage` permission and is written to the audit log (service name, versions, destination address; never passwords or keys).

## Change version (same data format)

*Change version* replaces the image tag and waits for the rollout. For databases it only allows the same major version (16.3 → 16.5, 8.0.36 → 8.0.40); before the change it can start a dump into a separate volume (`<name>-dump-<time>`, kept until you delete it). A different major version is refused with a pointer to *Migrate*: the data directory of one major version cannot be opened by another.

For MinIO, SeaweedFS and Redis any tag is accepted.

## Migrate a database to a newer major version

PostgreSQL 16 → 17, MySQL 8.0 → 8.4, MariaDB 10.11 → 11.4 and so on. The old instance is never touched until you decide:

1. **Start.** A second instance of the target version starts next to the old one (its own StatefulSet, volume and Service). The old database keeps serving.
2. **Copy.** When the new instance is ready, a Job streams a dump from the old one into the new one (`pg_dumpall | psql`, `mysqldump | mysql`). Optional: stop the platform-deployed applications for the time of the copy to get consistent data; they are started again afterwards, also when the copy fails.
3. **Switch over.** One click changes the selector of the existing Service, so the application keeps the same address. The old instance is scaled to zero, its volume stays.
4. **Roll back** at any moment: before the switch-over it removes the new instance; after it, the Service returns to the old instance and it starts again.
5. **Delete the old instance** (after you have checked the application): irreversible, asks you to type the service name.

Requirements: the database runs as a StatefulSet with one replica; a regular (not headless) Service selects it; the password is set as a container variable (`POSTGRES_PASSWORD`, `MYSQL_ROOT_PASSWORD`, …, a plain value or a Secret reference). Passwords are copied into the Job as references and never pass through the platform's memory or responses. Use an image of the target version with the same extensions (for example `postgis/postgis:17-3.5` for a PostGIS database). Downgrades are not supported: restore from a backup instead.

MySQL and MariaDB: the databases and the user accounts with their grants are copied; the system schemas (`mysql`, `sys`, `performance_schema`) are not, because restoring them over a running server hangs and breaks the system tables of the newer version. Accounts that use the `mysql_native_password` plugin cannot be created on MySQL 8.4 (the plugin is off by default): the copy fails with a clear error and the old instance stays untouched; switch such accounts to `caching_sha2_password` first.

Errors of the copy other than "already exists" (the service roles and databases that a fresh instance already has) fail the Job; the log is shown in the card.

## Copy an S3 storage into another one

For MinIO or SeaweedFS the card offers *Copy to another S3*: enter the endpoint and keys of the destination (a new SeaweedFS, another MinIO, any S3). A Job goes through the source buckets one by one: `rclone copy` creates the bucket on the destination and copies the objects, then `rclone check` compares them (retried on temporary errors, which a freshly started SeaweedFS gives on its first writes); the source is not modified. The destination keys live in a temporary Secret that is removed when the Job ends. When the copy is verified, point the application at the new endpoint (`S3_ENDPOINT` and keys in the project secrets) and redeploy.
