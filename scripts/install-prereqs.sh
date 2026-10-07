#!/usr/bin/env bash
# Проверяет (и при необходимости ставит) зависимости платформы в кластере: kpack и ClusterBuilder.
#   scripts/install-prereqs.sh --check     только проверить
#   scripts/install-prereqs.sh             поставить kpack, если его нет (версия: KPACK_VERSION или последняя)
set -euo pipefail
CHECK_ONLY=0; [ "${1:-}" = "--check" ] && CHECK_ONLY=1
command -v kubectl >/dev/null || { echo "нужен kubectl"; exit 1; }
kubectl get ns >/dev/null 2>&1 || { echo "нет доступа к кластеру (проверьте kubeconfig)"; exit 1; }
MISSING=0

if kubectl get crd images.kpack.io >/dev/null 2>&1; then
  echo "kpack: установлен"
elif [ "$CHECK_ONLY" = 1 ]; then
  echo "kpack: НЕ установлен"; MISSING=1
else
  V=${KPACK_VERSION:-$(curl -fsS https://api.github.com/repos/buildpacks-community/kpack/releases/latest | sed -n 's/.*"tag_name": *"\(v[^"]*\)".*/\1/p' | head -1)}
  [ -n "$V" ] || { echo "не удалось определить версию kpack; задайте KPACK_VERSION=v0.x.y"; exit 1; }
  echo "Ставлю kpack $V"
  kubectl apply -f "https://github.com/buildpacks-community/kpack/releases/download/$V/release-${V#v}.yaml"
  kubectl -n kpack rollout status deploy/kpack-controller --timeout=180s
fi

if kubectl get clusterbuilder default >/dev/null 2>&1; then
  echo "ClusterBuilder 'default': есть"
else
  echo "ClusterBuilder 'default': НЕТ. Создайте его (нужен ваш реестр образов и учётные данные) по шаблонам в deploy/kpack/:"
  echo "  lifecycle.yaml, stack.yaml, store.yaml, builder.yaml — замените адрес реестра и serviceAccountRef."
  MISSING=1
fi
exit $MISSING
