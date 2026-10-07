# What SplitWave is

SplitWave is a self-hosted deployment platform. You connect a Git repository; after that every push is built, rolled out to your Kubernetes cluster or server, recorded in an audit log, and can be rolled back in one click. It runs on your infrastructure and sends no usage data anywhere.

## The two parts

| | Free core | Paid modules |
|---|---|---|
| Licence | Elastic License 2.0, free to use on your own servers | Commercial, by licence key |
| What you get | Repository wizard, builds, deploy, rollback, secrets, `.env` import, approvals, 2FA, audit log, notifications, external connection check | Team limits, PR previews, audit export, SSO, SIEM export, several clusters, servers with an agent |
| Limits | 1 project, 1 environment, 1 user | By plan (see [Plans and licences](plans-licensing.md)) |

When a paid licence expires, the paid modules switch off and the installation keeps running as the free edition. Nothing is deleted or locked.

## How a deployment flows

1. You push to a branch that an environment follows.
2. Your Git host calls the platform's webhook.
3. The platform builds an image from your Dockerfile, a Dockerfile it suggested, or Cloud Native Buildpacks, and pushes it to the registry.
4. The platform changes the image of the Deployment. Kubernetes rolls the new version out; the old one keeps serving until the new one is ready.
5. A release is marked `deployed` only when the application is actually up. If it crashes, the release is marked `failed` with the reason and the previous version stays in place.
6. Every step is written to the audit log: who, what, when, which image.

## Concepts

- **Project**: one application and its repository.
- **Environment**: where a project runs (for example `prod`, `staging`). Each has a branch, a namespace, and options such as auto-deploy and required approval.
- **Release**: one deployment of one image to one environment. You can roll back to any earlier release without rebuilding.
- **Secret**: an encrypted value (a password, a token) given to the application. Secrets are versioned and never shown again after saving.
- **Approval**: a second person must confirm a deploy or rollback in environments that require it.
- **Role**: `admin`, `devops`, `developer`, `viewer`. See [Team and security](team-security.md).

## Where to go next

- Install on a server: [Quick start](quickstart.md).
- Connect your first application: [Connecting a project](connect-project.md).
- Questions about plans: [Plans and licences](plans-licensing.md).
