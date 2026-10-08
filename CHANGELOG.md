# Changelog

## 0.1.1

- The installer, the Helm notes and the prerequisites script now speak English.
- The project page no longer shows "kubernetes 404: Not Found" under Builds on installations without kpack; builds made with kaniko are listed as before.

## 0.1.0 (first public release)

First release of the free core (source-available, Elastic License 2.0).

- **Connect a repository and deploy.** Paste a link (GitHub, GitLab, Bitbucket, Gitea, Forgejo): the platform detects the stack, prepares or uses a Dockerfile, builds the image in your cluster and rolls it out. Failed rollouts do no harm: the old version keeps serving.
- **Stacks with a ready Dockerfile template:** Node.js, Python, Go, Java (Maven, Gradle), PHP (Composer), Ruby, .NET, Rust and static sites. Any repository with its own Dockerfile works.
- **Rollback in one click** to any earlier release, without rebuilding.
- **Secrets** encrypted at rest with a key you keep, with versions, expiry and `.env` import.
- **Approvals, audit log with a hash chain, API tokens with roles, two-factor authentication, notifications.** The core has one built-in administrator account; several accounts, human roles and project members are the paid Teams module, and several environments per project (with deploys by branch) are the paid Environments module; the core runs one project, and several projects are the paid Projects module.
- **Deploy targets:** Kubernetes (k3s tested; other distributions via the Helm chart) and plain servers with an agent.
- **Interface** in English, Russian and Kazakh, light and dark themes.
- **Paid modules** (Lite, Pro, Max) enabled by an offline-verified licence key; the platform never contacts a licence server and sends no telemetry. An optional update notice reads one public file a day (no key, address or installation id is sent); `UPDATE_CHECK=false` turns it off.

Known limits: GitHub Enterprise Server and Bitbucket Server are not supported yet; one service per project (no docker-compose); installer tested on Ubuntu 22.04/24.04 and Debian 12.
