# Installation

Two ways: a one-command install on a single server, or the Helm chart in an existing Kubernetes cluster.

## One server, one command

Requirements: Ubuntu 22.04/24.04 or Debian 12, at least 4 GB RAM, 20 GB disk, internet, root.

```bash
sudo ./deploy/quickstart/install-single-node.sh \
  --chart ./deploy/helm/devsecops-platform --image <registry>/control-plane:<version> \
  [--registry-config ~/.docker/config.json]   # image in a private registry
  [--image-archive image.tar]                 # or pass the image as a file: no registry credentials on the server
  [--host panel.example.com]                  # panel domain; default <ip>.sslip.io
  [--tls auto|custom|none] [--email you@example.com]
  [--tls-cert fullchain.pem --tls-key key.pem]  # for --tls custom
  [--license-key <key>] [--no-provisioner]
```

The script prints the address, the path of the administrator token (`/root/dsp-admin-token`) and the path of the **secrets encryption key**. Keep a copy of the key outside the server.

### HTTPS

- `--tls auto` (default when `--host` is set): Let's Encrypt certificates are issued and renewed by the built-in Traefik using HTTP-01. Ports **80 and 443** must be reachable and the domain's A record must point at the server.
- `--tls custom --tls-cert … --tls-key …`: your own certificate (one for all domains, for example a wildcard).
- `--tls none`: HTTP only.

You do not need certbot. The chosen mode also applies to the domains you bind to applications.

### Other notes

- Builds from a Dockerfile work immediately. Builds **without a Dockerfile** (buildpacks) need kpack and a real registry with credentials.
- `--no-provisioner`: the platform gets no right to create applications; the wizard prints YAML for you to apply manually (see [Connecting a project](connect-project.md)).
- The script is idempotent. To remove: `helm uninstall dsp -n platform` (data and keys stay).

## Existing Kubernetes cluster

Requirements: Kubernetes (k3s or any conformant cluster), `kubectl`, `helm` 3 or 4, and a registry where kpack can publish images.

```bash
# 1. Prerequisites: kpack and ClusterBuilder
scripts/install-prereqs.sh

# 2. The platform (keys, passwords and the admin token are generated)
helm install dsp deploy/helm/devsecops-platform -n platform --create-namespace \
  --set image.repository=<your-image> --set image.tag=<version> \
  --set config.registryPrefix=<registry/organisation> \
  --set 'rbac.deployNamespaces={apps}' --wait

# 3. Administrator token and a check
kubectl -n platform get secret dsp-devsecops-platform-config \
  -o jsonpath='{.data.ADMIN_BOOTSTRAP_TOKEN}' | base64 -d; echo
helm test dsp -n platform
```

Registry: use a private one (for example `ghcr.io`). The credentials are a Secret of type `kubernetes.io/dockerconfigjson` attached to the kpack ServiceAccount (publishing) and the platform's ServiceAccount (pulling). For `ghcr.io` use a classic token with only `read:packages` and `write:packages`, never `repo`.

Good to know:
- The Secret `dsp-devsecops-platform-config` is **not** deleted on `helm uninstall`; it holds the encryption key. Keep a separate copy of `SECRET_ENCRYPTION_KEY`.
- External database: `--set postgresql.enabled=false --set postgresql.external.url=postgresql+psycopg2://...`
- Access from outside: set `ingress.enabled` and `ingress.host`. With network policies, allow the ingress controller's namespace via `networkPolicy.allowFromNamespaces`.
- Run **one replica** (`replicaCount: 1`): builds run inside the process.
- Paid features: `--set secrets.licenseKey=<key>` and the Pro image.
- Upgrade: `helm upgrade dsp deploy/helm/devsecops-platform -n platform --reuse-values ...`. The token and keys do not change.

## Back up

Back up two things: the PostgreSQL database and the **encryption key** (`SECRET_ENCRYPTION_KEY`). The database without the key cannot be decrypted; the key without the database is useless.

## Single sign-on and other settings

Helm values `sso.*`, `siem.*`, `previews.*`, `onboarding.*` and `provisioner.*` configure the optional parts. See [Team and security](team-security.md) and [Notifications and SIEM](notifications-siem.md).
