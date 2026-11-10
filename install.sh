#!/usr/bin/env bash
# Установка личного VPN на AmneziaWG (Ubuntu 22.04 / 24.04). Telegram-бот — отдельно: bot.sh.
#
#   sudo bash install.sh                 — всё по умолчанию: случайный порт 1024–9999 (не 443), MTU 1280,
#                                          устройства iphone (режим «обход») и pc (весь трафик)
#   sudo bash install.sh --port 7385     — свой основной порт (1024–9999, не 443)
#   sudo bash install.sh --devices "iphone android pc:full"
#   sudo bash install.sh --ip 1.2.3.4    — внешний IP вручную (иначе определяется сам)
#
# Повторный запуск безопасен: ключи, порты и устройства сохраняются, обновляются только скрипты.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Шаги ниже могут пересобирать конфиги; бот разошлёт их один раз в конце — только изменившиеся.
export VPN_NO_PUSH=1
conf_hashes() { for d in /root/vpn-configs/*/; do [[ -d $d ]] && echo "$(basename "$d") $(cat "$d"*.conf 2>/dev/null | sha256sum | cut -c1-16)"; done; }
BEFORE="$(conf_hashes 2>/dev/null || true)"
PORT=""
IP=""
DEVICES="iphone pc:full"

die() { echo "Ошибка: $*" >&2; exit 1; }
step() { echo "==> $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --ip) IP="$2"; shift 2 ;;
    --devices) DEVICES="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) die "неизвестный параметр: $1 (см. --help)" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "запустите от root: sudo bash install.sh"
. /etc/os-release
[[ "$ID" == "ubuntu" ]] || die "нужна Ubuntu 22.04 или 24.04 (сейчас: $PRETTY_NAME). Переустановите ОС в панели хостинга."
if [[ -n "$PORT" ]]; then
  [[ "$PORT" =~ ^[0-9]{4}$ && "$PORT" -ge 1024 && "$PORT" != 443 ]] || die "--port: число 1024–9999 (не 443)"
fi

# На свежем сервере Ubuntu сама ставит обновления (unattended-upgrades) и держит блокировку apt —
# ждём её до 15 минут и пишем об этом, а не висим молча.
APT=(apt-get -o DPkg::Lock::Timeout=900 -o Dpkg::Use-Pty=0)
wait_apt() {
  if pgrep -x unattended-upgr >/dev/null || pgrep -f "^/usr/bin/python3 /usr/bin/unattended-upgrade" >/dev/null; then
    echo "    Ubuntu сейчас сама ставит обновления — жду, пока закончит (обычно 2–10 минут)..."
  fi
}
dpkg --configure -a >/dev/null 2>&1 || true  # если прошлую установку прервали посередине

step "Устанавливаю пакеты"
wait_apt
"${APT[@]}" update -qq
"${APT[@]}" install -y -q software-properties-common python3 iptables iproute2 curl qrencode dkms \
  | grep -E "^(Setting up|Настраивается)" | sed 's/^/    /' || true

if ! command -v awg >/dev/null || ! modinfo amneziawg >/dev/null 2>&1; then
  step "Устанавливаю AmneziaWG"
  echo "    подключаю репозиторий ppa:amnezia/ppa..."
  timeout 180 add-apt-repository -y ppa:amnezia/ppa >/dev/null \
    || die "не удалось подключить репозиторий Amnezia (нет связи с launchpad.net?) — пришлите вывод: add-apt-repository -y ppa:amnezia/ppa"
  "${APT[@]}" update -qq
  echo "    ставлю заголовки ядра $(uname -r)..."
  "${APT[@]}" install -y -qq "linux-headers-$(uname -r)" >/dev/null \
    || die "нет заголовков для ядра $(uname -r) — пришлите вывод: apt-get install linux-headers-$(uname -r)"
  echo "    собираю модуль ядра AmneziaWG (1–5 минут, вывод ниже)..."
  "${APT[@]}" install -y -q amneziawg amneziawg-tools 2>&1 \
    | grep -E "Building|Installing|DKMS|dkms|Setting up|Error|error" | sed 's/^/    /' || true
  modinfo amneziawg >/dev/null 2>&1 && command -v awg >/dev/null \
    || die "не удалось установить AmneziaWG — пришлите вывод: apt-get install amneziawg; dkms status"
  # Метапакет заголовков — чтобы модуль сам пересобирался после обновлений ядра (не критично).
  FLAVOUR="$(uname -r | sed 's/^[0-9.-]*-//')"
  "${APT[@]}" install -y -qq "linux-headers-$FLAVOUR" >/dev/null 2>&1 || true
fi
modprobe amneziawg || die "модуль amneziawg не загрузился — пришлите вывод: dkms status; dmesg | tail -20"
echo amneziawg >/etc/modules-load.d/amneziawg.conf

step "Сетевые настройки (пересылка, BBR, буферы UDP)"
cat >/etc/sysctl.d/99-vpn.conf <<'EOF'
net.ipv4.ip_forward=1
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
net.core.rmem_max=33554432
net.core.wmem_max=33554432
net.core.rmem_default=1048576
net.core.wmem_default=1048576
net.core.netdev_max_backlog=16384
net.ipv4.tcp_mtu_probing=1
EOF
sysctl -q --system || true

step "Ставлю скрипты"
install -d -m 700 /etc/vpn /etc/vpn/clients /root/vpn-configs
install -m 755 "$SCRIPT_DIR/lib/vpn.py" /usr/local/bin/vpn
install -m 755 "$SCRIPT_DIR/lib/vpn-rules.sh" /usr/local/sbin/vpn-rules
install -m 755 "$SCRIPT_DIR/lib/vpn-watchdog.sh" /usr/local/sbin/vpn-watchdog
install -m 644 "$SCRIPT_DIR/lib/blocked-nets.txt" /etc/vpn/blocked-nets.txt
if [[ -f /etc/vpn/bot.env ]]; then  # бот уже настроен (bot.sh) — обновляем и его
  install -m 755 "$SCRIPT_DIR/lib/vpn-bot.py" /usr/local/bin/vpn-bot
  systemctl restart vpn-bot 2>/dev/null || true
fi
[[ -f /etc/vpn/extra-nets.txt ]] || cat >/etc/vpn/extra-nets.txt <<'EOF'
# Свои сети «через VPN» для режима «обход» — по одной на строку, например:
# 203.0.113.0/24
# После правки: vpn regen
EOF

if [[ -z "$IP" ]]; then
  IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org || curl -4 -fsS --max-time 10 https://ifconfig.me || true)"
  rm -f /etc/vpn/ip-fixed
else
  touch /etc/vpn/ip-fixed
fi
[[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "не удалось определить внешний IP, укажите: --ip АДРЕС"
WAN="$(ip -4 route show default | awk '{print $5; exit}')"
[[ -n "$WAN" ]] || die "не нашёл сетевой интерфейс с выходом в интернет"

step "Настраиваю сервер AmneziaWG"
systemctl enable "awg-quick@awg0" >/dev/null 2>&1
if [[ -f /etc/vpn/server.json ]]; then
  vpn init --ip "$IP" --wan "$WAN"
  CUR="$(python3 -c "import json; print(json.load(open('/etc/vpn/server.json'))['port'])")"
  if [[ -n "$PORT" && "$PORT" != "$CUR" ]]; then vpn set-ports "$PORT"; fi
else
  vpn init --ip "$IP" --wan "$WAN" ${PORT:+--port "$PORT"}
fi
systemctl is-active --quiet awg-quick@awg0 \
  || { journalctl -u awg-quick@awg0 -n 20 --no-pager; die "AmneziaWG не запустился"; }

read -r MAIN B1 B2 < <(python3 -c "import json; s=json.load(open('/etc/vpn/server.json')); print(s['port'], *s['backup'])")
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  for p in $MAIN $B1 $B2; do ufw allow "$p"/udp comment "AmneziaWG" >/dev/null; done
  # ufw по умолчанию запрещает пересылку — разрешаем для VPN.
  sed -i 's/^DEFAULT_FORWARD_POLICY="DROP"/DEFAULT_FORWARD_POLICY="ACCEPT"/' /etc/default/ufw
  ufw reload >/dev/null
  vpn-rules up
fi

step "Ставлю сторожа (проверка и самовосстановление раз в минуту)"
cat >/etc/systemd/system/vpn-watchdog.service <<'EOF'
[Unit]
Description=VPN: проверка и самовосстановление AmneziaWG
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/vpn-watchdog
EOF
cat >/etc/systemd/system/vpn-watchdog.timer <<'EOF'
[Unit]
Description=VPN: сторож раз в минуту

[Timer]
OnBootSec=60
OnUnitActiveSec=60
AccuracySec=5

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now vpn-watchdog.timer >/dev/null 2>&1

step "TikTok всегда через VPN в режиме «обход» (проверка раз в час)"
cat >/etc/systemd/system/vpn-tiktok.service <<'UNIT'
[Unit]
Description=VPN: обновить сети TikTok, если он переехал
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vpn tiktok --check
StandardOutput=append:/var/log/vpn-tiktok.log
StandardError=append:/var/log/vpn-tiktok.log
UNIT
cat >/etc/systemd/system/vpn-tiktok.timer <<'UNIT'
[Unit]
Description=VPN: сети TikTok раз в час

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now vpn-tiktok.timer >/dev/null 2>&1
if [[ ! -f /etc/vpn/tiktok-nets.txt ]]; then
  echo "    собираю сети TikTok (до минуты)..."
  vpn tiktok 2>&1 | sed 's/^/    /' || echo "    не получилось — повторится само в течение часа"
fi

if [[ ! -f /etc/vpn/adblock-on && ! -f /etc/vpn/adblock-disabled ]]; then
  step "Блокировка рекламы (выключить: bash adblock.sh off)"
  bash "$SCRIPT_DIR/adblock.sh" 2>&1 | sed 's/^/    /' || echo "    не получилось — попробуйте позже: bash adblock.sh"
elif [[ -f /etc/vpn/adblock-on ]]; then
  bash "$SCRIPT_DIR/adblock.sh" >/dev/null 2>&1 || true   # обновить скрипт и список
fi

step "Защита SSH от подбора пароля (fail2ban)"
if "${APT[@]}" install -y -qq fail2ban >/dev/null 2>&1; then
  cat >/etc/fail2ban/jail.d/sshd-vpn.local <<'EOF'
[sshd]
enabled = true
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
EOF
  systemctl enable fail2ban >/dev/null 2>&1
  systemctl restart fail2ban >/dev/null 2>&1 || echo "    fail2ban не запустился (не страшно, VPN работает)"
fi

if [[ -z "$(ls -A /etc/vpn/clients)" ]]; then
  step "Создаю устройства: $DEVICES"
  for d in $DEVICES; do
    name="${d%%:*}"
    if [[ "$d" == *:full ]]; then vpn add "$name" --full >/dev/null; else vpn add "$name" >/dev/null; fi
  done
else
  vpn regen >/dev/null
fi

# Бот: разослать конфиги, которые изменились (тем, чьи они), — один раз.
AFTER="$(conf_hashes)"
CHANGED="$(comm -13 <(sort <<<"$BEFORE") <(sort <<<"$AFTER") | awk '{print $1}' | paste -sd' ')"
if [[ -n "$CHANGED" && -n "$BEFORE" && -x /usr/local/bin/vpn-bot && -f /etc/vpn/bot.env ]]; then
  VPN_ONLY="$CHANGED" /usr/local/bin/vpn-bot send-all "Обновились настройки VPN" >/dev/null 2>&1 &
  echo "Изменились конфиги: $CHANGED — бот пришлёт их (тебе и владельцам)."
fi

cat <<EOF

================ Готово ================
Сервер:        $IP
Порты UDP:     $MAIN — основной (конфиги *-1.conf)
               $B1 — резерв (*-2.conf),  $B2 — резерв (*-3.conf)
MTU:           1280
Сторож:        раз в минуту, журнал /var/log/vpn-watchdog.log

Если у хостинга есть файрвол в панели — откройте там UDP $MAIN, $B1, $B2.

Конфиги: /root/vpn-configs/  (скачать через SFTP в Termius)
EOF
vpn list
cat <<EOF

Конфиг «обхода» длинный (сотни сетей) — переносите файл через SFTP или бота; QR есть у «весь трафик».
Как пойдёт сайт:       vpn nets youtube.com sberbank.ru
Диагностика скорости:  bash diag.sh
Состояние сервера:     vpn status
Все команды:           vpn --help
Telegram-бот (по желанию): bash bot.sh 'ТОКЕН' ВАШ_ID
EOF
