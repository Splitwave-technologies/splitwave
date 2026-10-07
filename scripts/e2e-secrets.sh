#!/usr/bin/env bash
# Сквозной тест доставки секретов: приложение -> проект -> секрет -> sync -> значение в поде -> границы ролей.
# Запуск на машине с kubectl и файлом токена админа. Тестовые ресурсы (namespace e2e, проект e2e-app, токены) удаляются в конце.
set -uo pipefail
API=${API:-http://127.0.0.1:18080}
ADMIN=$(cat "${ADMIN_TOKEN_FILE:-$HOME/.control-plane-admin-token}")
NS=e2e; APP=e2e-app; PASS=0; FAIL=0
ok()   { echo "PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "FAIL  $1"; FAIL=$((FAIL+1)); }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (ожидалось '$3', получено '$2')"; fi; }
code() { curl -s -o /dev/null -w "%{http_code}" -m 30 "$@"; }
jget() { python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }
hdr()  { echo "Authorization: Bearer $1"; }

kubectl port-forward svc/control-plane 18080:80 >/tmp/pf-e2e.log 2>&1 & PF=$!
cleanup() {
  curl -s -o /dev/null -X DELETE -H "$(hdr "$ADMIN")" "$API/api/projects/$APP"
  for t in "${TOKEN_IDS[@]:-}"; do [ -n "$t" ] && curl -s -o /dev/null -X DELETE -H "$(hdr "$ADMIN")" "$API/api/tokens/$t"; done
  kubectl delete ns "$NS" --wait=false >/dev/null 2>&1
  kill $PF 2>/dev/null
}
TOKEN_IDS=(); trap cleanup EXIT
sleep 4

# 1. приложение в кластере (если прошлый namespace ещё удаляется — ждём, иначе ресурсы не создадутся)
for _ in $(seq 1 60); do kubectl get ns "$NS" >/dev/null 2>&1 || break; [ "$(kubectl get ns "$NS" -o jsonpath='{.status.phase}' 2>/dev/null)" = "Active" ] && break; sleep 5; done
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS" >/dev/null
sed "s/NAMESPACE/$NS/g" "$(dirname "$0")/../deploy/platform/namespace-access.yaml" | kubectl apply -f - >/dev/null
kubectl -n $NS apply -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: {name: $APP}
spec:
  replicas: 1
  selector: {matchLabels: {app: $APP}}
  template:
    metadata: {labels: {app: $APP}}
    spec:
      containers:
      - {name: app, image: "busybox:1.36", command: ["sh","-c","sleep 100000"]}
YAML
kubectl -n $NS rollout status deploy/$APP --timeout=180s >/dev/null && ok "тестовое приложение запущено" || bad "тестовое приложение не запустилось"

# 2. проект и роли
check "админ создаёт проект" "$(code -X POST -H "$(hdr "$ADMIN")" -H 'Content-Type: application/json' "$API/api/projects" -d "{\"slug\":\"$APP\",\"repo_full_name\":\"acme/$APP\",\"environments\":[{\"name\":\"prod\",\"namespace\":\"$NS\",\"deployment_name\":\"$APP\",\"container_name\":\"app\"}]}")" 201
mk() { R=$(curl -s -X POST -H "$(hdr "$ADMIN")" -H 'Content-Type: application/json' "$API/api/tokens" -d "{\"name\":\"$1\",\"role\":\"$2\"}"); echo "$R" | jget "['token']"; }
SFX=$(python3 -c 'import secrets;print(secrets.token_hex(3))')  # имена токенов уникальны: отозванные имена остаются занятыми
OPS_N="e2e-ops-$SFX"; DEV_N="e2e-dev-$SFX"
OPS=$(mk "$OPS_N" devops); DEV=$(mk "$DEV_N" developer)
for n in "$OPS_N" "$DEV_N"; do TOKEN_IDS+=("$(curl -s -H "$(hdr "$ADMIN")" "$API/api/tokens" | python3 -c "import sys,json; print(next(t['id'] for t in json.load(sys.stdin) if t['name']=='$n'))")"); done

# 3. секрет: devops пишет, developer не может ни писать, ни читать значение
VAL1="e2e-$(python3 -c 'import secrets;print(secrets.token_hex(8))')"
check "devops задаёт секрет" "$(code -X PUT -H "$(hdr "$OPS")" -H 'Content-Type: application/json' "$API/api/projects/$APP/secrets/E2E_SECRET" -d "{\"value\":\"$VAL1\"}")" 200
LIST=$(curl -s -H "$(hdr "$DEV")" "$API/api/projects/$APP/secrets")
echo "$LIST" | grep -q E2E_SECRET && ok "developer видит имя секрета" || bad "developer не видит имя секрета"
echo "$LIST" | grep -q "$VAL1" && bad "developer видит ЗНАЧЕНИЕ секрета" || ok "developer не видит значение в списке"
check "developer не может писать секрет"    "$(code -X PUT -H "$(hdr "$DEV")" -H 'Content-Type: application/json' "$API/api/projects/$APP/secrets/X" -d '{"value":"v"}')" 403
check "developer не может делать sync"       "$(code -X POST -H "$(hdr "$DEV")" "$API/api/projects/$APP/secrets/sync")" 403
check "значение не читается через API (GET)" "$(code -H "$(hdr "$ADMIN")" "$API/api/projects/$APP/secrets/E2E_SECRET")" 405

# 4. доставка в кластер
check "sync секретов" "$(code -X POST -H "$(hdr "$OPS")" "$API/api/projects/$APP/secrets/sync")" 200
GOT=$(kubectl -n $NS get secret $APP-prod-platform-env -o jsonpath='{.data.E2E_SECRET}' 2>/dev/null | base64 -d 2>/dev/null)
check "Kubernetes Secret содержит значение" "$GOT" "$VAL1"
check "Secret помечен managed-by" "$(kubectl -n $NS get secret $APP-prod-platform-env -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}' 2>/dev/null)" splitwave
kubectl -n $NS rollout status deploy/$APP --timeout=180s >/dev/null
check "значение доступно в поде (env)" "$(kubectl -n $NS exec deploy/$APP -- printenv E2E_SECRET 2>/dev/null)" "$VAL1"

# 5. смена значения перезапускает под
VAL2="e2e-$(python3 -c 'import secrets;print(secrets.token_hex(8))')"
curl -s -o /dev/null -X PUT -H "$(hdr "$OPS")" -H 'Content-Type: application/json' "$API/api/projects/$APP/secrets/E2E_SECRET" -d "{\"value\":\"$VAL2\"}"
curl -s -o /dev/null -X POST -H "$(hdr "$OPS")" "$API/api/projects/$APP/secrets/sync"
kubectl -n $NS rollout status deploy/$APP --timeout=180s >/dev/null
check "после смены значения под получает новое" "$(kubectl -n $NS exec deploy/$APP -- printenv E2E_SECRET 2>/dev/null)" "$VAL2"

# 6. аудит без значений
AUD=$(curl -s -H "$(hdr "$ADMIN")" "$API/api/audit?project=$APP&limit=100")
echo "$AUD" | grep -q -e "$VAL1" -e "$VAL2" && bad "значение попало в аудит" || ok "в аудите нет значений секретов"
echo "$AUD" | grep -q secret_set && echo "$AUD" | grep -q secrets_sync && ok "аудит содержит secret_set и secrets_sync" || bad "в аудите нет ожидаемых событий"

echo "ИТОГО: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
