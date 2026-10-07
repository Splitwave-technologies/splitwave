# Security model

What the platform does to protect your systems, and what remains your job.

## What the platform does

- **Secrets are encrypted** in the database with a key that you keep. A value leaves the system only into a Kubernetes Secret at deploy time and is masked in logs.
- **Tamper-evident audit log**: a hash chain over all events; verification finds a changed or deleted record.
- **Least privilege by default**: in the default mode the platform can only change the image of an existing Deployment. The broader provisioner mode is optional.
- **Hardened workloads**: application namespaces use Pod Security level `baseline`; pods get no service-account token, drop all capabilities except `NET_BIND_SERVICE` and cannot escalate privileges.
- **Authentication**: passwords, optional TOTP with recovery codes, enforced 2FA, session limits, lockout after failed attempts, API tokens stored as hashes, OIDC single sign-on with PKCE (state, nonce, signature, issuer and audience checks).
- **Outbound requests are guarded** (repository wizard, connection check, notifications, SIEM): public addresses only, no redirects, bounded sizes and call rates.
- **Previews are isolated**: pull requests from forks are never built; base secrets are not copied into previews.
- **Servers with an agent** connect out to the platform over HTTPS: no inbound port on the server. Tokens are stored as hashes and the content of a job is erased when it finishes.
- **No telemetry, offline licence check, optional update notice (a plain GET of a public file once a day; `UPDATE_CHECK=false` disables it).**

## What stays with you

- Keep a copy of the encryption key and the database backup.
- Restrict who is `admin` and `devops`; use 2FA or SSO.
- Review the provisioner mode: the right to create Deployments cluster-wide allows running any image; combine it with Pod Security Admission.
- Keep the platform and Kubernetes up to date.
- Do not store secrets in the repository or the Dockerfile.

## Reporting a vulnerability

Write to support@split-wave.com. Please give details privately and allow time for a fix before publishing.
