#!/usr/bin/env bash
# Поднимает изолированный сервер, гоняет UI-тесты, гасит сервер. Скриншоты — в /tmp/ui-shots.
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p /tmp/ui-shots
# чистый старт: прежний экземпляр (если остался) убиваем и ждём освобождения порта
pkill -f "tests/ui_server.py" 2>/dev/null || true
for i in $(seq 1 20); do (echo > /dev/tcp/127.0.0.1/18099) 2>/dev/null || break; sleep 0.5; done
( cd ../control-plane && exec .venv/bin/python tests/ui_server.py ) > /tmp/ui-server.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null; wait $SRV 2>/dev/null' EXIT
for i in $(seq 1 40); do curl -sf http://127.0.0.1:18099/health >/dev/null && break; sleep 0.5; done
npx playwright test "$@"
