# Troubleshooting and FAQ

## A release is `failed`

Open the release: the message says why. The previous version keeps serving.

| Message or reason | Likely cause | What to do |
|---|---|---|
| `CrashLoopBackOff` with log lines | the application crashes at start | read the lines; usually missing environment variables, no access to the database or a wrong port |
| `CreateContainerConfigError` | a secret or key the Deployment refers to is missing | add the secret in *Secrets*, then deploy again |
| `ImagePullBackOff`, `ErrImagePull` | wrong image name or no registry access | check the registry credentials of the cluster |
| `ProgressDeadlineExceeded` or timeout | the application did not become ready in time | check the readiness probe and the port; raise `ROLLOUT_TIMEOUT_SECONDS` if the start is slow |

## The build fails

Open *Builds, Logs*. Common causes: the Dockerfile is not in the expected place, a monorepo (the wizard warns about it: the build context is the repository, so check the Dockerfile paths), a private dependency without credentials, or buildpacks selected without kpack and a registry configured.

## The application starts and then crashes

Open *Logs* on the pod (for a crashed pod the previous run opens). Check that all variables from `.env.example` are set as secrets and use *Check connections* for external services.

## HTTPS does not work for a domain

- The A record must point at the server **before** the certificate is requested. Ports 80 and 443 must be open.
- With Cloudflare or another proxy switch the record to *DNS only* while the certificate is issued.
- Check from outside: `dig +short your.domain @1.1.1.1`. Your own resolver may cache a missing record for up to an hour.
- For your own certificate, see the checks in [Domains and certificates](domains-tls.md).

## "Limit reached" when creating a project, environment or user

Your plan's limit is reached (see *License and plans* for usage). Existing items keep working. Upgrade the plan or remove something unused.

## The licence key is not accepted

- The key is bound to an Installation ID: it works only on the installation it was issued for. Compare the ID in the console with the one on the key's order.
- Check that the Pro or Lite image is used, not the free core image, and that the key was passed as `secrets.licenseKey`.
- An expired key switches the paid modules off; renew with a new order.

## I lost the administrator token or the encryption key

The administrator token can be read from the Kubernetes Secret `dsp-devsecops-platform-config` (key `ADMIN_BOOTSTRAP_TOKEN`). The encryption key is in the same Secret (`SECRET_ENCRYPTION_KEY`); if the Secret is lost and you have no copy, stored secrets cannot be recovered, and you set them again.

## Can I see a secret's value?

No. Values are never returned. You can replace a secret or restore an older version.

## Does the platform send data to you?

No. The licence is checked offline and there is no telemetry. The only optional request is the daily update notice, which sends nothing about you; set `UPDATE_CHECK=false` to switch it off.

## Still stuck

Write to support@split-wave.com with the platform version, what you did, and the release message or log lines (remove secrets).
