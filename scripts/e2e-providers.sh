#!/usr/bin/env bash
# Живая проверка вебхуков GitLab / Bitbucket / Gitea: подпись, разбор события, защита. Сборки не запускаются
# (ветка не привязана ни к одной среде — событие принимается и пропускается).
set -uo pipefail
API=${API:-http://127.0.0.1:18080}
ADMIN=$(cat "${ADMIN_TOKEN_FILE:-$HOME/.control-plane-admin-token}")
PASS=0; FAIL=0; PROJECTS=()
ok(){ echo "PASS  $1"; PASS=$((PASS+1)); }; bad(){ echo "FAIL  $1"; FAIL=$((FAIL+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" || bad "$1 (ожидалось '$3', получено '$2')"; }
jget(){ python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }
A(){ curl -s -m 30 -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' "$@"; }
kubectl port-forward svc/control-plane 18080:80 >/tmp/pf-prov.log 2>&1 </dev/null & PF=$!
cleanup(){ for p in "${PROJECTS[@]:-}"; do [ -n "$p" ] && A -o /dev/null -X DELETE "$API/api/projects/$p"; done; kill $PF 2>/dev/null; }
trap cleanup EXIT; sleep 4
mk(){ PROJECTS+=("$1"); A -o /dev/null -w '%{http_code}' -X POST "$API/api/projects" -d "{\"slug\":\"$1\",\"repo_full_name\":\"$2\",\"provider\":\"$3\"$4,\"environments\":[{\"name\":\"prod\",\"namespace\":\"e2e-prov\",\"deployment_name\":\"x\",\"container_name\":\"x\",\"branch\":\"release-only\"}]}"; }
hook(){ curl -s -m 30 -X POST "$API/webhook/$1/$2" -H 'Content-Type: application/json' "${@:4}" -d "$3"; }
mac(){ printf '%s' "$2" | openssl dgst -sha256 -hmac "$1" | sed 's/^.* //'; }

check "GitLab-проект создан" "$(mk e2e-gl acme/team/app gitlab ',"git_url":"https://gitlab.example.com/acme/team/app.git"')" 201
check "Bitbucket-проект создан" "$(mk e2e-bb acme/app bitbucket '')" 201
check "Gitea-проект создан" "$(mk e2e-gt acme/app gitea ',"git_url":"https://git.example.com/acme/app.git"')" 201

GS=$(A "$API/api/projects/e2e-gl/webhook" | jget "['secret']")
GL='{"object_kind":"push","ref":"refs/heads/main","after":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","checkout_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","project":{"path_with_namespace":"acme/team/app","git_http_url":"https://gitlab.example.com/acme/team/app.git"}}'
check "GitLab: событие принято (ветка не отслеживается)" "$(hook gitlab e2e-gl "$GL" -H "X-Gitlab-Token: $GS" -H 'X-Gitlab-Event: Push Hook' | jget "['skipped']")" True
check "GitLab: неверный токен отклонён" "$(hook gitlab e2e-gl "$GL" -H 'X-Gitlab-Token: wrong' -o /dev/null -w '%{http_code}')" 401
check "GitLab: без токена отклонён" "$(hook gitlab e2e-gl "$GL" -o /dev/null -w '%{http_code}')" 401

BS=$(A "$API/api/projects/e2e-bb/webhook" | jget "['secret']")
BB='{"repository":{"full_name":"acme/app"},"push":{"changes":[{"new":{"type":"branch","name":"main","target":{"hash":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}}]}}'
check "Bitbucket: подписанное событие принято" "$(hook bitbucket e2e-bb "$BB" -H "X-Hub-Signature: sha256=$(mac "$BS" "$BB")" -H 'X-Event-Key: repo:push' | jget "['skipped']")" True
check "Bitbucket: подделка отклонена" "$(hook bitbucket e2e-bb "$BB" -H 'X-Hub-Signature: sha256=00' -H 'X-Event-Key: repo:push' -o /dev/null -w '%{http_code}')" 401

TS=$(A "$API/api/projects/e2e-gt/webhook" | jget "['secret']")
GT='{"ref":"refs/heads/main","after":"dddddddddddddddddddddddddddddddddddddddd","repository":{"full_name":"acme/app","clone_url":"https://git.example.com/acme/app.git"}}'
check "Gitea: подписанное событие принято" "$(hook gitea e2e-gt "$GT" -H "X-Gitea-Signature: $(mac "$TS" "$GT")" -H 'X-Gitea-Event: push' | jget "['skipped']")" True
check "Gitea: чужой секрет (другого проекта) отклонён" "$(hook gitea e2e-gt "$GT" -H "X-Gitea-Signature: $(mac "$GS" "$GT")" -o /dev/null -w '%{http_code}')" 401
check "разработчику секрет вебхука не выдаётся" "$(curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer invalid' "$API/api/projects/e2e-gl/webhook")" 401
check "токен репозитория не возвращается" "$(A -X PUT "$API/api/projects/e2e-gl/git-token" -d '{"token":"glpat-SECRET-XYZ"}' >/dev/null; A "$API/api/projects/e2e-gl/source" | grep -c glpat-SECRET)" 0
check "цепочка аудита цела" "$(A "$API/api/audit/verify" | jget "['ok']")" True
echo "ИТОГО: PASS=$PASS FAIL=$FAIL"; [ "$FAIL" -eq 0 ]
