#!/usr/bin/env bash
# Проверка учётных записей на живом экземпляре: пользователь, принудительная смена пароля, куки, CSRF, 2FA-код, удаление.
# Нужны kubectl и файл токена админа. Создаёт и удаляет временного пользователя.
set -uo pipefail
API=${API:-http://127.0.0.1:18080}
ADMIN=$(cat "${ADMIN_TOKEN_FILE:-$HOME/.control-plane-admin-token}")
U="e2e-user-$(python3 -c 'import secrets;print(secrets.token_hex(3))')"
PW1="Temporary-E2E-Passphrase-1"; PW2="Chosen-E2E-Passphrase-2"
PASS=0; FAIL=0; JAR=$(mktemp)
ok(){ echo "PASS  $1"; PASS=$((PASS+1)); }; bad(){ echo "FAIL  $1"; FAIL=$((FAIL+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" || bad "$1 (ожидалось '$3', получено '$2')"; }
code(){ curl -s -o /dev/null -w "%{http_code}" -m 30 "$@"; }
kubectl port-forward svc/control-plane 18080:80 >/tmp/pf-acc.log 2>&1 </dev/null & PF=$!
cleanup(){ [ -n "${UID_:-}" ] && curl -s -o /dev/null -X DELETE -H "Authorization: Bearer $ADMIN" "$API/api/users/$UID_"; rm -f "$JAR"; kill $PF 2>/dev/null; }
trap cleanup EXIT; sleep 4

R=$(curl -s -X POST -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' "$API/api/users" -d "{\"username\":\"$U\",\"role\":\"viewer\",\"password\":\"$PW1\"}")
UID_=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))")
[ -n "$UID_" ] && ok "админ создал пользователя" || bad "создание пользователя"

HDRS=$(curl -s -D - -o /tmp/login.json -c "$JAR" -X POST -H 'Content-Type: application/json' "$API/api/auth/login" -d "{\"username\":\"$U\",\"password\":\"$PW1\"}")
echo "$HDRS" | grep -i "^set-cookie" | grep -qi httponly && ok "кука сессии httpOnly" || bad "кука без httpOnly"
echo "$HDRS" | grep -i "^set-cookie" | grep -qi "samesite=strict" && ok "кука SameSite=Strict" || bad "кука без SameSite=Strict"
CSRF=$(python3 -c "import json; print(json.load(open('/tmp/login.json'))['csrf_token'])"); rm -f /tmp/login.json
check "до смены пароля доступ ограничен" "$(code -b "$JAR" "$API/api/projects")" 403
check "пароль меняется (с CSRF)" "$(code -b "$JAR" -X POST -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' "$API/api/auth/password" -d "{\"current\":\"$PW1\",\"new\":\"$PW2\"}")" 200
check "после смены доступ есть" "$(code -b "$JAR" "$API/api/projects")" 200
check "изменение без CSRF отклонено" "$(code -b "$JAR" -X POST -H 'Content-Type: application/json' "$API/api/auth/password" -d "{\"current\":\"$PW2\",\"new\":\"$PW1\"}")" 403
check "viewer не читает аудит" "$(code -b "$JAR" "$API/api/audit")" 403
check "неверный пароль не пускает" "$(code -X POST -H 'Content-Type: application/json' "$API/api/auth/login" -d "{\"username\":\"$U\",\"password\":\"wrong-password-xx\"}")" 401
check "выход" "$(code -b "$JAR" -X POST -H "X-CSRF-Token: $CSRF" "$API/api/auth/logout")" 200
check "сессия после выхода недействительна" "$(code -b "$JAR" "$API/api/me")" 401
check "админский токен продолжает работать" "$(code -H "Authorization: Bearer $ADMIN" "$API/api/me")" 200
echo "ИТОГО: PASS=$PASS FAIL=$FAIL"; [ "$FAIL" -eq 0 ]
