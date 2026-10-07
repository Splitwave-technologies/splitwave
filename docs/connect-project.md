# Connecting a project

*Projects, Connect repository.* Paste a link; the platform works out the rest.

## 1. Repository

Any of these works: `https://github.com/owner/repo`, `owner/repo`, `git@github.com:owner/repo.git`, a GitLab group path, Bitbucket Cloud, Gitea or Forgejo (`https://git.example.com/owner/repo`).

For a private repository or a self-hosted server, open *Private repository* and enter a read-only access token (and the provider if it cannot be guessed from the host name). The token is stored encrypted, used for builds only and never shown again.

## 2. Settings

The platform reads the repository through the provider's API (read-only, nothing is cloned) and proposes:

- language and framework;
- how to build: your Dockerfile (in the root or in `docker/`, `deploy/`, `backend/`, `api/`…) or buildpacks;
- the application port (`EXPOSE` or `PORT`), the project name, the default branch and its latest commit.

Warnings appear for things that usually go wrong: a monorepo, several Dockerfiles, no `start` script, Python without a `Procfile`, a frontend without a server, `docker-compose`, a private repository without a token. Variable names from `.env.example` are listed so you remember to set their values under *Secrets* (values are never read).

Then choose where to deploy: a Kubernetes cluster (optionally with a domain, which creates an Ingress) or a server with an agent.

## 3. Done

The project and environment are created, the application is prepared, the first build and deploy start at the latest commit, and the webhook to add to your repository is shown (address, secret, events).

## What is created in the cluster

For every application: a **Namespace** (Pod Security level `baseline`), a **RoleBinding** that lets the platform change the image there, a **Deployment** (a placeholder image until the first deploy, `PORT` set, a TCP readiness probe, no service-account token, all capabilities dropped except `NET_BIND_SERVICE`, no privilege escalation), a **Service**, and an **Ingress** if you entered a domain. Existing objects are never overwritten or deleted.

## Two modes: how much to trust the platform

- **Minimal permissions (default).** The platform can only change the image of an existing Deployment. The wizard shows a ready YAML; an administrator applies it once (`kubectl apply -f app.yaml`), then presses *I applied it, deploy*.
- **Provisioner.** The platform creates applications itself. Enable it with `provisioner.enabled=true` (it applies `deploy/platform/provisioner-rbac.yaml`). It grants the right to create namespaces, role bindings (only the deployer role), Deployments, Services and Ingresses **cluster-wide**. The right to create Deployments anywhere allows running any image there: combine it with Pod Security Admission.

The wizard checks the permissions and tells you which mode is active.

## No Dockerfile?

If the stack is recognised (Node.js, Python, Go, Java with Maven or Gradle, PHP with Composer, Ruby, .NET, Rust, static sites), the wizard prepares a Dockerfile and shows it. You can edit the text, or change the parameters (port, start command, runtime version) and press *Regenerate from parameters*.

- The Dockerfile is kept by the platform, not written to your repository, and is used for every build including auto-deploy. You can view, edit or switch back to the repository's Dockerfile on the project page (*Source*).
- Templates are multi-stage, run as a non-root user and read the port from `PORT`. Frontends without a server (React, Vue, Svelte, Angular) are built and served by nginx on port 8080 with a fallback to `index.html`.
- Guesses (for example the Python start command or the Go main package) are marked: check them.
- Do not put secrets into the Dockerfile: its text is visible in the build job description.
- A Dockerfile in the repository always wins unless you choose otherwise.
- The wizard looks for the application in the repository root and in the first-level folders. Check the chosen folder, especially in monorepos.
- The generated Dockerfile is a starting point. Applications that need a database, a secret key (for example Rails needs `SECRET_KEY_BASE`) or a `.env` file must get them in *Secrets* before the first deploy; otherwise the pod starts and stops, and the release is marked failed with the last log lines.
- With a very large number of requests to GitHub the public API may refuse inspection (rate limit). Set `SCM_GITHUB_TOKEN` (a read-only token) in the platform settings.

## Limits

GitHub Enterprise Server and Bitbucket Server are not supported yet (github.com and bitbucket.org only). `docker-compose` is not deployed: one service per project.

## Security notes

- The platform calls the provider's API for you, so every outgoing address is checked: https only, no credentials in the URL, public addresses only, no redirects, bounded response size, 20 checks per minute per user. Self-hosted Git servers in private networks need `SCM_ALLOW_PRIVATE_HOSTS=true`.
- The access token goes only to the provider's API and to the build job; it is not logged or returned.
- Names are validated before anything is created; reserved namespaces (`kube-*`, the platform's and the build namespace) are refused.
