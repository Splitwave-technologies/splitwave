#!/usr/bin/env bash
# Живая проверка версий секретов: история, возврат версии, срок действия, значение реально уходит в кластер.
set -uo pipefail
API=${API:-http://127.0.0.1:18080}
ADMIN=$(cat "${ADMIN_TOKEN_FILE:-$HOME/.control-plane-admin-token}")
NS=e2e-ver; APP=ver-app; PASS=0; FAIL=0
ok(){ echo "PASS  $1"; PASS=$((PASS+1)); }; bad(){ echo "FAIL  $1"; FAIL=$((FAIL+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" || bad "$1 (ожидалось '$3', получено '$2')"; }
jget(){ python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }
A(){ curl -s -m 30 -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' "$@"; }
kubectl port-forward svc/control-plane 18080:80 >/tmp/pf-ver.log 2>&1 </dev/null & PF=$!
cleanup(){ A -o /dev/null -X DELETE "$API/api/projects/$APP"; kubectl delete ns $NS --wait=false >/dev/null 2>&1; kill $PF 2>/dev/null; }
trap cleanup EXIT; sleep 4
kubectl create ns $NS >/dev/null 2>&1
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
A -o /dev/null -X POST "$API/api/projects" -d "{\"slug\":\"$APP\",\"repo_full_name\":\"acme/$APP\",\"environments\":[{\"name\":\"prod\",\"namespace\":\"$NS\",\"deployment_name\":\"$APP\",\"container_name\":\"app\"}]}"
S="$API/api/projects/$APP/secrets/DB_PASS"
check "версия 1" "$(A -X PUT "$S" -d '{"value":"alpha"}' | jget "['version']")" 1
check "версия 2" "$(A -X PUT "$S" -d '{"value":"beta"}' | jget "['version']")" 2
check "история: две версии" "$(A "$S/versions" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))')" 2
check "значения нет в истории" "$(A "$S/versions" | grep -c -e alpha -e beta)" 0
check "возврат к v1 создаёт v3" "$(A -X POST "$S/restore" -d '{"version":1}' | jget "['version']")" 3
A -o /dev/null -X POST "$API/api/projects/$APP/secrets/sync?environment=prod"
kubectl -n $NS get secret $APP-prod-platform-env -o jsonpath='{.data.DB_PASS}' | base64 -d > /tmp/ver-val; check "в кластере значение версии v1" "$(cat /tmp/ver-val)" alpha; rm -f /tmp/ver-val
SOON=$(date -u -d '+3 days' +%Y-%m-%dT%H:%M:%SZ)
A -o /dev/null -X PUT "$S/policy" -d "{\"expires_at\":\"$SOON\"}"
check "статус: скоро истечёт" "$(A "$API/api/projects/$APP/secrets" | jget "[0]['status']")" expiring
check "в обзоре есть предупреждение" "$(A "$API/api/overview" | python3 -c "import sys,json; print(any(a['project']=='$APP' for a in json.load(sys.stdin)['secret_alerts']))")" True
check "прошлая дата отклоняется" "$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' "$S" -d '{"value":"x","expires_at":"2020-01-01T00:00:00Z"}')" 422
check "цепочка аудита цела" "$(A "$API/api/audit/verify" | jget "['ok']")" True
echo "ИТОГО: PASS=$PASS FAIL=$FAIL"; [ "$FAIL" -eq 0 ]
