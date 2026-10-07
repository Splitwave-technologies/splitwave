#!/usr/bin/env bash
# Платформа на ОДНОМ сервере «под ключ»: k3s + реестр образов + PostgreSQL + платформа (Helm). Приложения клиентов запускаются на этом же сервере.
# Подходит для чистого VPS: Ubuntu 22.04/24.04, Debian 12 (нужны root или sudo, интернет, ≥ 4 ГБ ОЗУ, ≥ 20 ГБ диска).
#
#   sudo ./install-single-node.sh --chart ./splitwave --image ghcr.io/ORG/control-plane-pro:TAG \
#        [--registry-config ~/.docker/config.json] [--image-archive FILE] [--host panel.example.com] [--tls auto|custom|none]
#        [--email you@example.com] [--tls-cert FILE --tls-key FILE] [--license-key KEY] [--no-provisioner]
#
# --chart            каталог или .tgz Helm-чарта платформы (обязательно)
# --image            образ платформы repository:tag (обязательно)
# --registry-config  docker config.json с доступом на скачивание образа платформы (если реестр приватный)
# --image-archive    файл с образом платформы (docker/ctr save): образ загружается в k3s без доступа к реестру (приватный ghcr без учётных данных на сервере)
# --host             доменное имя панели (по умолчанию <ip-сервера>.sslip.io — работает без собственного DNS). A-запись домена должна указывать на этот сервер
# --tls              HTTPS: auto — сертификат Let's Encrypt выпускается и продлевается автоматически (нужны открытые порты 80 и 443 и домен, указывающий на сервер);
#                    custom — свой сертификат (--tls-cert/--tls-key, один на все домены: например wildcard); none — только HTTP.
#                    По умолчанию: auto, если указан --host, иначе none
# --email            почта для уведомлений Let's Encrypt о сроке сертификата (необязательно; без неё уведомлений не будет)
# --tls-cert/--tls-key  PEM-файлы своего сертификата (цепочка целиком) и ключа для --tls custom
# --license-key      лицензионный ключ платных функций (без него — Community)
# --no-provisioner   не давать платформе права создавать приложения в кластере (мастер будет выдавать YAML вместо создания)
# --admin-token-file куда записать токен администратора (по умолчанию /root/dsp-admin-token, права 0600)
#
# Что делает: ставит k3s и Helm, поднимает локальный реестр образов внутри кластера (платформа собирает образы клиентов kaniko и публикует
# сюда же), настраивает containerd на этот реестр, ставит платформу, ждёт готовности и печатает адрес. Скрипт можно запускать повторно.
# Что НЕ делает: резервные копии сверх встроенных, фаервол сервера (оставьте открытыми 22, 80, 443; порт 6443 наружу не нужен).
# Домены приложений: после установки привяжите домен к приложению в панели (проект → среды → «Домен»); HTTPS для них включается автоматически вместе с --tls.
set -euo pipefail

CHART=""; IMAGE=""; REG_CONFIG=""; HOST=""; LICENSE=""; PROVISIONER=true; TOKEN_FILE="/root/dsp-admin-token"
TLS=""; EMAIL=""; TLS_CERT=""; TLS_KEY=""; ARCHIVE=""
while [ $# -gt 0 ]; do case "$1" in
  --chart) CHART="$2"; shift 2;; --image) IMAGE="$2"; shift 2;; --registry-config) REG_CONFIG="$2"; shift 2;;
  --host) HOST="$2"; shift 2;; --license-key) LICENSE="$2"; shift 2;; --no-provisioner) PROVISIONER=false; shift;;
  --tls) TLS="$2"; shift 2;; --email) EMAIL="$2"; shift 2;; --tls-cert) TLS_CERT="$2"; shift 2;; --tls-key) TLS_KEY="$2"; shift 2;;
  --image-archive) ARCHIVE="$2"; shift 2;;
  --admin-token-file) TOKEN_FILE="$2"; shift 2;; -h|--help) sed -n '2,32p' "$0"; exit 0;;
  *) echo "неизвестный аргумент: $1 (см. --help)"; exit 2;; esac; done
[ -n "$CHART" ] && [ -n "$IMAGE" ] || { echo "нужны --chart и --image (см. --help)"; exit 2; }
case "$IMAGE" in *:*) ;; *) echo "--image: укажите repository:tag"; exit 2;; esac
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root: sudo $0 ..."; exit 1; }
HOST_GIVEN=false; [ -z "$HOST" ] || HOST_GIVEN=true
[ -n "$TLS" ] || { if $HOST_GIVEN; then TLS=auto; else TLS=none; fi; }
case "$TLS" in auto|custom|none) ;; *) echo "--tls: auto, custom или none"; exit 2;; esac
if [ "$TLS" = custom ]; then
  [ -f "$TLS_CERT" ] && [ -f "$TLS_KEY" ] || { echo "--tls custom требует существующие файлы --tls-cert и --tls-key (PEM)"; exit 2; }
  openssl x509 -in "$TLS_CERT" -noout >/dev/null 2>&1 || { echo "--tls-cert: это не PEM-сертификат"; exit 2; }
  [ "$(openssl x509 -in "$TLS_CERT" -noout -pubkey 2>/dev/null | openssl pkey -pubin -outform der 2>/dev/null | sha256sum)" = "$(openssl pkey -in "$TLS_KEY" -pubout -outform der 2>/dev/null | sha256sum)" ] \
    || { echo "--tls-key не подходит к --tls-cert (разные ключи)"; exit 2; }
fi
[ -z "$ARCHIVE" ] || [ -f "$ARCHIVE" ] || { echo "файл не найден: $ARCHIVE"; exit 2; }

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
SCRATCH=$(mktemp -d); trap 'rm -rf "$SCRATCH"' EXIT

say "Проверка сервера"
. /etc/os-release 2>/dev/null || true; echo "ОС: ${PRETTY_NAME:-неизвестно}"
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo); disk_gb=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
echo "ОЗУ: ${mem_mb} МБ, свободно на диске: ${disk_gb} ГБ, ядер: $(nproc)"
[ "$mem_mb" -ge 3500 ] || { echo "ОШИБКА: нужно не меньше 4 ГБ ОЗУ (k3s + база + сборки образов)"; exit 1; }
[ "$disk_gb" -ge 12 ] || echo "ПРЕДУПРЕЖДЕНИЕ: мало места (${disk_gb} ГБ): рекомендуется 20+ ГБ, сборки образов и реестр растут"
command -v curl >/dev/null || { echo "нужен curl"; exit 1; }
[ -e "$CHART" ] || { echo "чарт не найден: $CHART"; exit 1; }
[ -z "$REG_CONFIG" ] || [ -f "$REG_CONFIG" ] || { echo "файл не найден: $REG_CONFIG"; exit 1; }
IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')
[ -n "$HOST" ] || HOST="${IP:-localhost}.sslip.io"

say "k3s (Kubernetes)"
if ! command -v k3s >/dev/null; then
  curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--write-kubeconfig-mode 644" sh -
else echo "k3s уже установлен: $(k3s --version | head -1)"; fi
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
wait_node() { for _ in $(seq 1 90); do k3s kubectl get nodes 2>/dev/null | grep -q " Ready" && return 0; sleep 2; done; return 1; }
wait_node || { echo "ОШИБКА: узел k3s не стал Ready"; exit 1; }
KC="k3s kubectl"

if [ -n "$ARCHIVE" ]; then
  say "Образ платформы из файла"
  k3s ctr -n k8s.io images import "$ARCHIVE" | tail -1
fi

say "Helm"
if ! command -v helm >/dev/null; then curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash; else echo "helm: $(helm version --short)"; fi

say "Реестр образов внутри кластера"
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
# containerd не умеет разрешать имена сервисов кластера и ходит по https: даём зеркало на ClusterIP по http (kaniko публикует по имени, поды скачивают через зеркало)
mkdir -p /etc/rancher/k3s
cat > "$SCRATCH/registries.yaml" <<Y
mirrors:
  "$REG_NAME":
    endpoint:
      - "http://$REG_IP:5000"
Y
if ! cmp -s "$SCRATCH/registries.yaml" /etc/rancher/k3s/registries.yaml 2>/dev/null; then
  cp "$SCRATCH/registries.yaml" /etc/rancher/k3s/registries.yaml
  echo "Зеркало реестра настроено ($REG_NAME → $REG_IP:5000), перезапускаю k3s"
  systemctl restart k3s; sleep 5; wait_node || { echo "ОШИБКА: k3s не поднялся после перезапуска"; exit 1; }
  $KC -n registry rollout status deploy/registry --timeout=240s
fi

wait_traefik_args() {   # $1 — подстрока аргумента Traefik, которая должна появиться после применения настроек
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
    $HOST_GIVEN && ! getent hosts "$HOST" >/dev/null && echo "ПРЕДУПРЕЖДЕНИЕ: имя $HOST пока не резолвится: добавьте A-запись на этот сервер, иначе сертификат не выпустится (панель будет с самоподписанным)."
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

say "Платформа"
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

say "Токен администратора"
umask 077
$KC -n platform get secret dsp-splitwave-config -o jsonpath='{.data.ADMIN_BOOTSTRAP_TOKEN}' | base64 -d > "$TOKEN_FILE"; echo >> "$TOKEN_FILE"
KEY_FILE="${TOKEN_FILE}.encryption-key"
$KC -n platform get secret dsp-splitwave-config -o jsonpath='{.data.SECRET_ENCRYPTION_KEY}' | base64 -d > "$KEY_FILE"; echo >> "$KEY_FILE"

SCHEME=http; [ "$TLS" = none ] || SCHEME=https
NOTE=""; [ "$TLS" != none ] || NOTE="     (HTTPS не включён: используйте --tls auto или поставьте сертификат/прокси перед использованием в интернете)"
cat <<EOF

Готово.
  Адрес:               $SCHEME://$HOST/ui/$NOTE
  Токен администратора: $TOKEN_FILE   (вставьте его на странице входа)
  Ключ шифрования:     $KEY_FILE   ← СКОПИРУЙТЕ В НАДЁЖНОЕ МЕСТО: без него секреты проектов в базе не прочитать
  Реестр образов:      $REG_NAME (внутри кластера; данные на сервере в /var/lib/dsp-registry)
  Проверка:            k3s kubectl -n platform get pods
  Удаление платформы:  helm uninstall dsp -n platform   (секрет с ключами и данные базы остаются)
EOF
