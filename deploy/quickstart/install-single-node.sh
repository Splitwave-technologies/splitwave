#!/usr/bin/env bash
# SplitWave on a SINGLE server, turnkey: k3s + image registry + PostgreSQL + the platform (Helm). Your applications run on the same server.
# Suited to a clean VPS: Ubuntu 22.04/24.04, Debian 12 (needs root or sudo, internet, at least 4 GB RAM and 20 GB disk).
#
#   sudo ./install-single-node.sh --chart ./splitwave --image ghcr.io/ORG/control-plane-pro:TAG \
#        [--registry-config ~/.docker/config.json] [--image-archive FILE] [--host panel.example.com] [--tls auto|custom|none]
#        [--email you@example.com] [--tls-cert FILE --tls-key FILE] [--license-key KEY] [--no-provisioner]
#
# --chart            directory or .tgz of the platform Helm chart (required)
# --image            platform image repository:tag (required)
# --registry-config  docker config.json that can pull the platform image (if the registry is private)
# --image-archive    file with the platform image (docker/ctr save): the image is loaded into k3s without registry access (private ghcr with no credentials on the server)
# --host             domain name of the panel (default <server-ip>.sslip.io, which works without your own DNS). The A record of the domain must point at this server
# --tls              HTTPS: auto - a Let's Encrypt certificate is issued and renewed automatically (needs open ports 80 and 443 and a domain pointing at the server);
#                    custom - your own certificate (--tls-cert/--tls-key, one for all domains, for example a wildcard); none - HTTP only.
#                    Default: auto if --host is given, otherwise none
# --email            email for Let's Encrypt certificate-expiry notices (optional; without it you get no notices)
# --tls-cert/--tls-key  PEM files of your own certificate (full chain) and key for --tls custom
# --license-key      licence key for the paid modules (without it you run the free Community core)
# --no-provisioner   do not let the platform create applications in the cluster (the wizard will give you YAML instead of creating them)
# --admin-token-file where to write the administrator token (default /root/dsp-admin-token, mode 0600)
#
# What it does: installs k3s and Helm, starts a local image registry inside the cluster (the platform builds your images with kaniko and publishes them
# there), points containerd at that registry, installs the platform, waits until it is ready and prints the address. Safe to run again.
# What it does NOT do: backups beyond the built-in ones, or the server firewall (keep 22, 80 and 443 open; port 6443 does not need to be public).
# Application domains: after the install, bind a domain to an application in the panel (project, environments, Domain); HTTPS for them is enabled automatically together with --tls.
set -euo pipefail

CHART=""; IMAGE=""; REG_CONFIG=""; HOST=""; LICENSE=""; PROVISIONER=true; TOKEN_FILE="/root/dsp-admin-token"
TLS=""; EMAIL=""; TLS_CERT=""; TLS_KEY=""; ARCHIVE=""
while [ $# -gt 0 ]; do case "$1" in
  --chart) CHART="$2"; shift 2;; --image) IMAGE="$2"; shift 2;; --registry-config) REG_CONFIG="$2"; shift 2;;
  --host) HOST="$2"; shift 2;; --license-key) LICENSE="$2"; shift 2;; --no-provisioner) PROVISIONER=false; shift;;
  --tls) TLS="$2"; shift 2;; --email) EMAIL="$2"; shift 2;; --tls-cert) TLS_CERT="$2"; shift 2;; --tls-key) TLS_KEY="$2"; shift 2;;
  --image-archive) ARCHIVE="$2"; shift 2;;
  --admin-token-file) TOKEN_FILE="$2"; shift 2;; -h|--help) sed -n '2,32p' "$0"; exit 0;;
  *) echo "unknown argument: $1 (see --help)"; exit 2;; esac; done
[ -n "$CHART" ] && [ -n "$IMAGE" ] || { echo "--chart and --image are required (see --help)"; exit 2; }
case "$IMAGE" in *:*) ;; *) echo "--image: give repository:tag"; exit 2;; esac
[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo $0 ..."; exit 1; }
HOST_GIVEN=false; [ -z "$HOST" ] || HOST_GIVEN=true
[ -n "$TLS" ] || { if $HOST_GIVEN; then TLS=auto; else TLS=none; fi; }
case "$TLS" in auto|custom|none) ;; *) echo "--tls: auto, custom or none"; exit 2;; esac
if [ "$TLS" = custom ]; then
  [ -f "$TLS_CERT" ] && [ -f "$TLS_KEY" ] || { echo "--tls custom needs existing --tls-cert and --tls-key files (PEM)"; exit 2; }
  openssl x509 -in "$TLS_CERT" -noout >/dev/null 2>&1 || { echo "--tls-cert: this is not a PEM certificate"; exit 2; }
  [ "$(openssl x509 -in "$TLS_CERT" -noout -pubkey 2>/dev/null | openssl pkey -pubin -outform der 2>/dev/null | sha256sum)" = "$(openssl pkey -in "$TLS_KEY" -pubout -outform der 2>/dev/null | sha256sum)" ] \
    || { echo "--tls-key does not match --tls-cert (different keys)"; exit 2; }
fi
[ -z "$ARCHIVE" ] || [ -f "$ARCHIVE" ] || { echo "file not found: $ARCHIVE"; exit 2; }

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
SCRATCH=$(mktemp -d); trap 'rm -rf "$SCRATCH"' EXIT

say "Checking the server"
. /etc/os-release 2>/dev/null || true; echo "OS: ${PRETTY_NAME:-unknown}"
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo); disk_gb=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
echo "RAM: ${mem_mb} MB, free disk: ${disk_gb} GB, CPU cores: $(nproc)"
[ "$mem_mb" -ge 3500 ] || { echo "ERROR: at least 4 GB of RAM is needed (k3s + database + image builds)"; exit 1; }
[ "$disk_gb" -ge 12 ] || echo "WARNING: little disk space (${disk_gb} GB): 20+ GB is recommended, image builds and the registry grow"
command -v curl >/dev/null || { echo "curl is required"; exit 1; }
[ -e "$CHART" ] || { echo "chart not found: $CHART"; exit 1; }
[ -z "$REG_CONFIG" ] || [ -f "$REG_CONFIG" ] || { echo "file not found: $REG_CONFIG"; exit 1; }
IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')
[ -n "$HOST" ] || HOST="${IP:-localhost}.sslip.io"

say "k3s (Kubernetes)"
if ! command -v k3s >/dev/null; then
  curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--write-kubeconfig-mode 644" sh -
else echo "k3s is already installed: $(k3s --version | head -1)"; fi
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
wait_node() { for _ in $(seq 1 90); do k3s kubectl get nodes 2>/dev/null | grep -q " Ready" && return 0; sleep 2; done; return 1; }
wait_node || { echo "ERROR: the k3s node did not become Ready"; exit 1; }
KC="k3s kubectl"

if [ -n "$ARCHIVE" ]; then
  say "Platform image from the file"
  k3s ctr -n k8s.io images import "$ARCHIVE" | tail -1
fi

say "Helm"
if ! command -v helm >/dev/null; then curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash; else echo "helm: $(helm version --short)"; fi

say "Image registry inside the cluster"
$KC apply -f - <<'Y'
apiVersion: v1
kind: Namespace
metadata: {name: registry}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: registry, namespace: registry}
spec:
  replicas: 1
  selector: {matchLabels: {app: registry}}
  template:
    metadata: {labels: {app: registry}}
    spec:
      containers:
        - name: registry
          image: registry:2
          ports: [{containerPort: 5000}]
          env: [{name: REGISTRY_STORAGE_DELETE_ENABLED, value: "true"}]
          volumeMounts: [{name: data, mountPath: /var/lib/registry}]
          readinessProbe: {httpGet: {path: /v2/, port: 5000}, periodSeconds: 5}
      volumes: [{name: data, hostPath: {path: /var/lib/dsp-registry, type: DirectoryOrCreate}}]
---
apiVersion: v1
kind: Service
metadata: {name: registry, namespace: registry}
spec: {selector: {app: registry}, ports: [{port: 5000, targetPort: 5000}]}
Y
$KC -n registry rollout status deploy/registry --timeout=240s
REG_IP=$($KC -n registry get svc registry -o jsonpath='{.spec.clusterIP}')
REG_NAME="registry.registry.svc.cluster.local:5000"
# containerd cannot resolve cluster service names and speaks https: give it a mirror on the ClusterIP over http (kaniko pushes by name, pods pull through the mirror)
mkdir -p /etc/rancher/k3s
cat > "$SCRATCH/registries.yaml" <<Y
mirrors:
  "$REG_NAME":
    endpoint:
      - "http://$REG_IP:5000"
Y
if ! cmp -s "$SCRATCH/registries.yaml" /etc/rancher/k3s/registries.yaml 2>/dev/null; then
  cp "$SCRATCH/registries.yaml" /etc/rancher/k3s/registries.yaml
  echo "Registry mirror configured ($REG_NAME -> $REG_IP:5000), restarting k3s"
  systemctl restart k3s; sleep 5; wait_node || { echo "ERROR: k3s did not come back after the restart"; exit 1; }
  $KC -n registry rollout status deploy/registry --timeout=240s
fi

wait_traefik_args() {   # $1 - substring of a Traefik argument that must appear after the settings are applied
  for _ in $(seq 1 60); do
    $KC -n kube-system get deploy traefik -o jsonpath='{range .spec.template.spec.containers[0].args[*]}{@}{"\n"}{end}' 2>/dev/null | grep -q -- "$1" && break
    sleep 4
  done
  $KC -n kube-system rollout status deploy/traefik --timeout=240s >/dev/null
}
TLS_ANN='{}'; TLS_BLOCK='[]'
if [ "$TLS" != none ]; then
  say "HTTPS ($TLS)"
  ACME_EMAIL=""; [ -z "$EMAIL" ] || ACME_EMAIL="          email: \"$EMAIL\""
  if [ "$TLS" = auto ]; then
    $KC apply -f - <<Y
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata: {name: traefik, namespace: kube-system}
spec:
  valuesContent: |-
    persistence:
      enabled: true
      size: 128Mi
    certificatesResolvers:
      le:
        acme:
          httpChallenge:
            entryPoint: web
          storage: /data/acme.json
$ACME_EMAIL
    ports:
      web:
        http:
          redirections:
            entryPoint:
              to: websecure
              scheme: https
              permanent: true
Y
    wait_traefik_args "certificatesresolvers.le.acme.httpChallenge"
    TLS_ANN='{"traefik.ingress.kubernetes.io/router.entrypoints":"websecure","traefik.ingress.kubernetes.io/router.tls":"true","traefik.ingress.kubernetes.io/router.tls.certresolver":"le"}'
    $HOST_GIVEN && ! getent hosts "$HOST" >/dev/null && echo "WARNING: the name $HOST does not resolve yet: add an A record pointing at this server, otherwise the certificate will not be issued (the panel will use a self-signed one)."
  else
    $KC -n kube-system create secret tls dsp-default-cert --cert="$TLS_CERT" --key="$TLS_KEY" --dry-run=client -o yaml | $KC apply -f -
    for _ in $(seq 1 60); do $KC get crd tlsstores.traefik.io >/dev/null 2>&1 && break; sleep 3; done
    $KC apply -f - <<Y
apiVersion: traefik.io/v1alpha1
kind: TLSStore
metadata: {name: default, namespace: kube-system}
spec: {defaultCertificate: {secretName: dsp-default-cert}}
---
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata: {name: traefik, namespace: kube-system}
spec:
  valuesContent: |-
    ports:
      web:
        http:
          redirections:
            entryPoint:
              to: websecure
              scheme: https
              permanent: true
Y
    wait_traefik_args "entryPoints.web.http.redirections"
    TLS_ANN='{"traefik.ingress.kubernetes.io/router.entrypoints":"websecure","traefik.ingress.kubernetes.io/router.tls":"true"}'
  fi
  TLS_BLOCK="[{\"hosts\":[\"$HOST\"]}]"
fi

say "Platform"
$KC create namespace platform --dry-run=client -o yaml | $KC apply -f -
SETS=(--set "image.repository=${IMAGE%:*}" --set "image.tag=${IMAGE##*:}"
      --set "config.registryPrefix=$REG_NAME/apps" --set "config.kaniko.insecureRegistries=$REG_NAME"
      --set "ingress.enabled=true" --set "ingress.host=$HOST" --set "ingress.className=traefik" --set "auth.trustedProxies=10.42.0.0/16"
      --set "provisioner.enabled=$PROVISIONER")
if [ -n "$REG_CONFIG" ]; then
  $KC -n platform create secret generic regcred --type=kubernetes.io/dockerconfigjson --from-file=.dockerconfigjson="$REG_CONFIG" --dry-run=client -o yaml | $KC apply -f -
  SETS+=(--set "imagePullSecrets[0].name=regcred")
fi
[ -z "$LICENSE" ] || SETS+=(--set "secrets.licenseKey=$LICENSE")
if [ "$TLS" != none ]; then
  SETS+=(--set-json "ingress.annotations=$TLS_ANN" --set-json "ingress.tls=$TLS_BLOCK" --set "onboarding.tls=$TLS" --set "onboarding.certResolver=le")
fi
helm upgrade --install dsp "$CHART" -n platform "${SETS[@]}" --wait --timeout 600s

say "Administrator token"
umask 077
$KC -n platform get secret dsp-splitwave-config -o jsonpath='{.data.ADMIN_BOOTSTRAP_TOKEN}' | base64 -d > "$TOKEN_FILE"; echo >> "$TOKEN_FILE"
KEY_FILE="${TOKEN_FILE}.encryption-key"
$KC -n platform get secret dsp-splitwave-config -o jsonpath='{.data.SECRET_ENCRYPTION_KEY}' | base64 -d > "$KEY_FILE"; echo >> "$KEY_FILE"

SCHEME=http; [ "$TLS" = none ] || SCHEME=https
NOTE=""; [ "$TLS" != none ] || NOTE="     (HTTPS is not enabled: use --tls auto, or put a certificate or proxy in front before exposing this to the internet)"
cat <<EOF

Done.
  Address:              $SCHEME://$HOST/ui/$NOTE
  Administrator token:  $TOKEN_FILE   (paste it on the sign-in page)
  Encryption key:       $KEY_FILE   <- COPY IT TO A SAFE PLACE: without it the project secrets in the database cannot be read
  Image registry:       $REG_NAME (inside the cluster; data on the server in /var/lib/dsp-registry)
  Check:                k3s kubectl -n platform get pods
  Uninstall:            helm uninstall dsp -n platform   (the secret with the keys and the database data are kept)
EOF
