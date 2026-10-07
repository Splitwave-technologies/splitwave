#!/usr/bin/env bash
# Собирает архив релиза для установки одним сервером: splitwave-<версия>.tar.gz и SHA256SUMS.
# Использование: scripts/make-release.sh 0.1.0 [каталог-результата]
set -euo pipefail
VER="${1:?укажите версию, например 0.1.0}"; VER="${VER#v}"
OUT="$(cd "${2:-dist}" 2>/dev/null && pwd || (mkdir -p "${2:-dist}" && cd "${2:-dist}" && pwd))"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="splitwave-$VER"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$NAME"
cd "$ROOT"
cp -r deploy "$STAGE/$NAME/deploy"
mkdir -p "$STAGE/$NAME/scripts" && cp scripts/install-prereqs.sh "$STAGE/$NAME/scripts/"
cp -r docs "$STAGE/$NAME/docs"
cp LICENSE LICENSE-COMMERCIAL.md README.md "$STAGE/$NAME/"
echo "$VER" > "$STAGE/$NAME/VERSION"
cat > "$STAGE/$NAME/install.sh" <<INSTALL
#!/usr/bin/env bash
# Установка SplitWave $VER на один сервер (Ubuntu 22.04/24.04 или Debian 12, root, от 4 ГБ ОЗУ).
# Пример: sudo ./install.sh --host panel.example.com --email you@example.com
# Остальные параметры: см. docs/install.md или ./deploy/quickstart/install-single-node.sh --help
set -euo pipefail
cd "\$(dirname "\$0")"
exec ./deploy/quickstart/install-single-node.sh --chart ./deploy/helm/splitwave --image "\${SPLITWAVE_IMAGE:-ghcr.io/splitwave-technologies/control-plane:$VER}" "\$@"
INSTALL
chmod +x "$STAGE/$NAME/install.sh" "$STAGE/$NAME/deploy/quickstart/"*.sh "$STAGE/$NAME/scripts/"*.sh
tar -C "$STAGE" --owner=0 --group=0 --numeric-owner -czf "$OUT/$NAME.tar.gz" "$NAME"
( cd "$OUT" && sha256sum "$NAME.tar.gz" > SHA256SUMS )
echo "готово: $OUT/$NAME.tar.gz"; cat "$OUT/SHA256SUMS"
