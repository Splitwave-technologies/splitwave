#!/usr/bin/env bash
# Checks (and installs if needed) the optional cluster dependencies of the platform: kpack and a ClusterBuilder (buildpack builds).
#   scripts/install-prereqs.sh --check     check only
#   scripts/install-prereqs.sh             install kpack if it is missing (version: KPACK_VERSION or the latest)
set -euo pipefail
CHECK_ONLY=0; [ "${1:-}" = "--check" ] && CHECK_ONLY=1
command -v kubectl >/dev/null || { echo "kubectl is required"; exit 1; }
kubectl get ns >/dev/null 2>&1 || { echo "no access to the cluster (check your kubeconfig)"; exit 1; }
MISSING=0

if kubectl get crd images.kpack.io >/dev/null 2>&1; then
  echo "kpack: installed"
elif [ "$CHECK_ONLY" = 1 ]; then
  echo "kpack: NOT installed"; MISSING=1
else
  V=${KPACK_VERSION:-$(curl -fsS https://api.github.com/repos/buildpacks-community/kpack/releases/latest | sed -n 's/.*"tag_name": *"\(v[^"]*\)".*/\1/p' | head -1)}
  [ -n "$V" ] || { echo "could not determine the kpack version; set KPACK_VERSION=v0.x.y"; exit 1; }
  echo "Installing kpack $V"
  kubectl apply -f "https://github.com/buildpacks-community/kpack/releases/download/$V/release-${V#v}.yaml"
  kubectl -n kpack rollout status deploy/kpack-controller --timeout=180s
fi

if kubectl get clusterbuilder default >/dev/null 2>&1; then
  echo "ClusterBuilder 'default': present"
else
  echo "ClusterBuilder 'default': MISSING. Create it (you need your image registry and credentials) from the templates in deploy/kpack/:"
  echo "  lifecycle.yaml, stack.yaml, store.yaml, builder.yaml - replace the registry address and serviceAccountRef."
  MISSING=1
fi
exit $MISSING
