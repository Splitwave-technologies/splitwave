#!/bin/sh
# Установка агента SplitWave на этот сервер.
#   curl -fsSL __PLATFORM_URL__/agent/install.sh | sudo sh -s -- --token <ОДНОРАЗОВЫЙ_ТОКЕН>
# Что делает: проверяет Docker и Python, скачивает агента в /opt/dsp-agent, подключает сервер к платформе (токен одноразовый),
# создаёт службу systemd dsp-agent. Удаление: sudo sh /opt/dsp-agent/uninstall.sh
set -eu
URL="__PLATFORM_URL__"
TOKEN=""; CA_FILE=""; ALLOW_HTTP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="$2"; shift 2;;
    --url) URL="$2"; shift 2;;
    --ca-file) CA_FILE="$2"; shift 2;;
    --allow-http) ALLOW_HTTP=1; shift;;
    *) echo "неизвестный аргумент: $1" >&2; exit 2;;
  esac
done
[ -n "$TOKEN" ] || { echo "нужен --token (создаётся на странице «Серверы»)" >&2; exit 2; }
[ "$(id -u)" = "0" ] || { echo "запустите от root (sudo)" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "Docker не найден: установите Docker и повторите" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "Python 3 не найден: установите python3 и повторите" >&2; exit 1; }
case "$URL" in
  https://*) ;;
  *) if [ "$ALLOW_HTTP" = "1" ]; then echo "ВНИМАНИЕ: адрес платформы без HTTPS — токены и секреты пойдут открытым текстом. Допустимо только в тестовой сети." >&2
     else echo "ОШИБКА: адрес платформы без HTTPS. Настройте HTTPS на платформе или (только для испытательной сети) добавьте --allow-http." >&2; exit 1; fi;;
esac
[ "$ALLOW_HTTP" = "1" ] && export DSP_AGENT_ALLOW_HTTP=1

mkdir -p /opt/dsp-agent /etc/dsp-agent
CA_ARG=""
[ -z "$CA_FILE" ] || { cp "$CA_FILE" /etc/dsp-agent/ca.pem; CA_ARG="--ca-file /etc/dsp-agent/ca.pem"; }
python3 - "$URL" <<'PY'
import sys, urllib.request, hashlib
url = sys.argv[1].rstrip("/")
body = urllib.request.urlopen(url + "/agent/dsp_agent.py", timeout=30).read()
want = urllib.request.urlopen(url + "/agent/dsp_agent.py.sha256", timeout=30).read().decode().strip()
if hashlib.sha256(body).hexdigest() != want:
    sys.exit("контрольная сумма агента не совпала: установка прервана")
open("/opt/dsp-agent/dsp_agent.py", "wb").write(body)
print("агент загружен, sha256", want[:16])
PY
chmod 0755 /opt/dsp-agent/dsp_agent.py
python3 /opt/dsp-agent/dsp_agent.py enroll --url "$URL" --token "$TOKEN" $CA_ARG
cat > /etc/systemd/system/dsp-agent.service <<'UNIT'
[Unit]
Description=SplitWave agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/dsp-agent/dsp_agent.py run
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNIT
[ "$ALLOW_HTTP" = "1" ] && sed -i '/^\[Service\]/a Environment=DSP_AGENT_ALLOW_HTTP=1' /etc/systemd/system/dsp-agent.service || true
cat > /opt/dsp-agent/uninstall.sh <<'UN'
#!/bin/sh
systemctl disable --now dsp-agent 2>/dev/null || true
rm -f /etc/systemd/system/dsp-agent.service
rm -rf /opt/dsp-agent /etc/dsp-agent
systemctl daemon-reload
echo "агент удалён; запущенные им контейнеры не тронуты"
UN
systemctl daemon-reload
systemctl enable --now dsp-agent
echo "готово: агент работает (systemctl status dsp-agent)"
