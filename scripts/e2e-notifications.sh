#!/usr/bin/env bash
# Живая проверка уведомлений: защита от SSRF, шифрование адреса, реальная попытка доставки на публичный хост
# (example.com отвечает ошибкой на POST — это и нужно: проверяем, что ошибка записана, а действие не сломано).
set -uo pipefail
API=${API:-http://127.0.0.1:18080}
ADMIN=$(cat "${ADMIN_TOKEN_FILE:-$HOME/.control-plane-admin-token}")
PASS=0; FAIL=0; CID=""
ok(){ echo "PASS  $1"; PASS=$((PASS+1)); }; bad(){ echo "FAIL  $1"; FAIL=$((FAIL+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" || bad "$1 (ожидалось '$3', получено '$2')"; }
jget(){ python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }
A(){ curl -s -m 40 -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' "$@"; }
code(){ curl -s -o /dev/null -w '%{http_code}' -m 40 -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' "$@"; }
kubectl port-forward svc/control-plane 18080:80 >/tmp/pf-notif.log 2>&1 </dev/null & PF=$!
cleanup(){ [ -n "$CID" ] && A -o /dev/null -X DELETE "$API/api/notifications/channels/$CID"; kill $PF 2>/dev/null; }
trap cleanup EXIT; sleep 4
N="e2e-hook-$RANDOM"
check "loopback запрещён" "$(code -X POST "$API/api/notifications/channels" -d "{\"name\":\"$N-a\",\"kind\":\"webhook\",\"url\":\"https://127.0.0.1/x\"}")" 422
check "metadata-адрес запрещён" "$(code -X POST "$API/api/notifications/channels" -d "{\"name\":\"$N-b\",\"kind\":\"webhook\",\"url\":\"https://169.254.169.254/x\"}")" 422
check "http запрещён" "$(code -X POST "$API/api/notifications/channels" -d "{\"name\":\"$N-c\",\"kind\":\"webhook\",\"url\":\"http://example.com/x\"}")" 422
R=$(A -X POST "$API/api/notifications/channels" -d "{\"name\":\"$N\",\"kind\":\"webhook\",\"url\":\"https://example.com/dsp-hook\",\"events\":[\"project_create\"]}")
CID=$(echo "$R" | jget "['id']")
check "канал создан, ключ подписи выдан" "$(echo "$R" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["signing_secret"])>20)')" True
check "список не содержит адреса и ключа" "$(A "$API/api/notifications/channels" | grep -c -e dsp-hook -e signing_secret)" 0
check "в базе адрес зашифрован" "$(kubectl -n platform-system exec postgres-0 -- psql -U platform -d platform -Atc "select count(*) from notification_channels where convert_from(config_encrypted,'UTF8') like '%dsp-hook%'" </dev/null 2>/dev/null)" 0
T=$(A -X POST "$API/api/notifications/channels/$CID/test")
check "тест: доставка не удалась, но ошибка отдана" "$(echo "$T" | jget "['ok']")" False
check "статус канала записан" "$(A "$API/api/notifications/channels" | python3 -c "import sys,json; print([c for c in json.load(sys.stdin) if c['id']=='$CID'][0]['last_status'])")" error
check "проверки сроков и аудита выполняются" "$(A -X POST "$API/api/notifications/check" | jget "['chain_ok']")" True
check "разработчику каналы недоступны" "$(curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer invalid-token' "$API/api/notifications/channels")" 401
echo "ИТОГО: PASS=$PASS FAIL=$FAIL"; [ "$FAIL" -eq 0 ]
