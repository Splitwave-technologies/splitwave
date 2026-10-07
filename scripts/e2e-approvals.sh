#!/usr/bin/env bash
# Проверка согласований на живом кластере: откат в защищённой среде проходит только после решения другого человека.
# Создаёт временные ресурсы (namespace e2e-appr, проект appr-app, токены) и удаляет их в конце.
set -uo pipefail
API=${API:-http://127.0.0.1:18080}
ADMIN=$(cat "${ADMIN_TOKEN_FILE:-$HOME/.control-plane-admin-token}")
NS=e2e-appr; APP=appr-app; PASS=0; FAIL=0; TOKEN_IDS=()
ok(){ echo "PASS  $1"; PASS=$((PASS+1)); }; bad(){ echo "FAIL  $1"; FAIL=$((FAIL+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" || bad "$1 (ожидалось '$3', получено '$2')"; }
code(){ curl -s -o /dev/null -w "%{http_code}" -m 30 "$@"; }
jget(){ python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }
H(){ echo "Authorization: Bearer $1"; }
img(){ kubectl -n $NS get deploy $APP -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null; }
sql(){ kubectl -n platform-system exec postgres-0 -- psql -U platform -d platform -Atc "$1" </dev/null; }
kubectl port-forward svc/control-plane 18080:80 >/tmp/pf-appr.log 2>&1 </dev/null & PF=$!
cleanup(){ curl -s -o /dev/null -X DELETE -H "$(H "$ADMIN")" "$API/api/projects/$APP"
  for t in "${TOKEN_IDS[@]:-}"; do [ -n "$t" ] && curl -s -o /dev/null -X DELETE -H "$(H "$ADMIN")" "$API/api/tokens/$t"; done
  kubectl delete ns $NS --wait=false >/dev/null 2>&1; kill $PF 2>/dev/null; }
trap cleanup EXIT; sleep 4

kubectl get ns $NS >/dev/null 2>&1 || kubectl create ns $NS >/dev/null
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
kubectl -n $NS rollout status deploy/$APP --timeout=180s >/dev/null && ok "приложение запущено (busybox:1.36)" || bad "приложение не запустилось"

check "проект со средой, требующей согласования" "$(code -X POST -H "$(H "$ADMIN")" -H 'Content-Type: application/json' "$API/api/projects" -d "{\"slug\":\"$APP\",\"repo_full_name\":\"acme/$APP\",\"environments\":[{\"name\":\"prod\",\"namespace\":\"$NS\",\"deployment_name\":\"$APP\",\"container_name\":\"app\",\"require_approval\":true}]}")" 201
sql "insert into releases (id, environment_id, status, image_digest, git_revision, triggered_by, created_at, deployed_at) select gen_random_uuid(), e.id, 'deployed', 'busybox:1.35', 'old0000', 'e2e', now() - interval '20 minutes', now() - interval '20 minutes' from environments e join projects p on p.id=e.project_id where p.slug='$APP'" >/dev/null
sql "insert into releases (id, environment_id, status, image_digest, git_revision, triggered_by, created_at, deployed_at) select gen_random_uuid(), e.id, 'deployed', 'busybox:1.36', 'new1111', 'e2e', now() - interval '5 minutes', now() - interval '5 minutes' from environments e join projects p on p.id=e.project_id where p.slug='$APP'" >/dev/null

mk(){ R=$(curl -s -X POST -H "$(H "$ADMIN")" -H 'Content-Type: application/json' "$API/api/tokens" -d "{\"name\":\"$1\",\"role\":\"$2\"}"); echo "$R" | jget "['token']"; }
SFX=$(python3 -c 'import secrets;print(secrets.token_hex(3))')
OPS1=$(mk "e2e-ops1-$SFX" devops); OPS2=$(mk "e2e-ops2-$SFX" devops); DEV=$(mk "e2e-dev-$SFX" developer)
for n in "e2e-ops1-$SFX" "e2e-ops2-$SFX" "e2e-dev-$SFX"; do TOKEN_IDS+=("$(curl -s -H "$(H "$ADMIN")" "$API/api/tokens" | python3 -c "import sys,json; print(next(t['id'] for t in json.load(sys.stdin) if t['name']=='$n'))")"); done

R=$(curl -s -X POST -H "$(H "$OPS1")" "$API/api/projects/$APP/rollback?environment=prod")
check "откат создаёт заявку, а не выполняется" "$(echo "$R" | jget "['requires_approval']")" True
AID=$(echo "$R" | jget "['approval_id']")
check "образ в кластере не тронут до решения" "$(img)" "busybox:1.36"
check "в заявке зафиксирован образ отката" "$(curl -s -H "$(H "$ADMIN")" "$API/api/approvals/$AID" | jget "['params']['image']")" "busybox:1.35"
check "автор не может согласовать сам себе" "$(code -X POST -H "$(H "$OPS1")" -H 'Content-Type: application/json' "$API/api/approvals/$AID/approve" -d '{}')" 403
check "разработчик не может согласовать" "$(code -X POST -H "$(H "$DEV")" -H 'Content-Type: application/json' "$API/api/approvals/$AID/approve" -d '{}')" 403
check "обход согласования не-админом запрещён" "$(code -X POST -H "$(H "$OPS1")" "$API/api/projects/$APP/rollback?environment=prod&break_glass=true&reason=trying+to+bypass+approval")" 403
check "другой devops согласовывает" "$(code -X POST -H "$(H "$OPS2")" -H 'Content-Type: application/json' "$API/api/approvals/$AID/approve" -d '{"note":"e2e"}')" 200
sleep 2; kubectl -n $NS rollout status deploy/$APP --timeout=120s >/dev/null 2>&1
check "после согласования образ в кластере откатился" "$(img)" "busybox:1.35"
check "повторное решение невозможно" "$(code -X POST -H "$(H "$OPS2")" -H 'Content-Type: application/json' "$API/api/approvals/$AID/approve" -d '{}')" 409
AUD=$(curl -s -H "$(H "$ADMIN")" "$API/api/audit?project=$APP&limit=100")
for a in approval_requested approval_approved approval_executed rollback; do echo "$AUD" | grep -q "\"$a\"" && ok "аудит: $a" || bad "аудит: нет $a"; done
echo "ИТОГО: PASS=$PASS FAIL=$FAIL"; [ "$FAIL" -eq 0 ]
