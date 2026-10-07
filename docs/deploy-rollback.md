# Deploy and roll back

## Automatic deploy

Each environment follows one branch. A push to that branch calls the project's webhook and the platform builds and deploys. Turn this off per environment with *auto-deploy*. The webhook address, secret and events are on the project page (`GET /api/projects/{slug}/webhook`).

## Manual deploy

*Rebuild and roll out* on the project page builds the branch head and deploys it. Via API: `POST /api/projects/{slug}/redeploy?environment=prod`.

## When is a release "deployed"?

After changing the image the platform waits until all replicas have updated and are ready, and only then marks the release `deployed`. A release becomes `failed` if:

- the new pod is in `CrashLoopBackOff` (the message carries the exit code and the last 25 log lines, with secret values replaced by `***`);
- a secret or key is missing (`CreateContainerConfigError`) or the image name is wrong;
- the image cannot be pulled for more than 45 seconds (`ImagePullBackOff`, `ErrImagePull`);
- the wait runs out (300 seconds by default) or Kubernetes reports `ProgressDeadlineExceeded`.

The reason is written to the audit log as `rollout_failed` or `rollout_timeout`. The previous version keeps serving, because the Deployment updates as a rolling update. Settings (platform environment): `ROLLOUT_WAIT_ENABLED` (default `true`), `ROLLOUT_TIMEOUT_SECONDS` (300), `ROLLOUT_POLL_SECONDS` (3). For servers with an agent the platform waits for the agent's report instead.

## Roll back

*Roll back* on a release returns the environment to that image **without rebuilding**. API: `POST /api/projects/{slug}/rollback?environment=prod[&to=<release id>]`; without `to` it goes to the previous release. The rollback is itself a release and is audited.

## Approvals

If an environment has *require approval*, a deploy or rollback becomes a request. Another person with the right to decide approves or denies it in *Approvals*. The author cannot decide their own request (four-eyes principle). A request expires after 24 hours.

### Emergency bypass (break-glass)

When approval is required but you cannot wait, an authorised user can bypass it by giving a reason of at least 10 characters. The bypass is written to the audit log and can notify your team (event *Break-glass used*).

## Pull request previews (Lite and above)

For a pull request from the same repository the platform starts a temporary copy of the application, builds the PR's code, and deletes everything when the PR closes (or after a time limit). Secrets of the base environment are **not** copied: a preview only gets secrets marked "for all environments". Pull requests from forks are never built. Previews do not count towards environments per project. Turn previews on per project in the interface. By default a project has at most 5 previews at a time (`previews.maxPerProject`) and each is removed after 168 hours (`previews.ttlHours`) if the closing event is lost; the platform needs the right to create Deployments in the application's namespace (`rbac.previews=true`).

## Several environments

Create `staging` and `prod` for one project, each with its own branch and namespace. Secrets can be set for all environments or overridden per environment. Environments can live in other clusters or on servers with an agent on the Pro plan.
