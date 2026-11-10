#!/usr/bin/env bash
# Блокировка рекламы и трекеров для устройств в VPN (как adblock.sh в MOST).
#
# Ставит DNS-сервер dnsmasq на адресе VPN-сервера (10.8.0.1). Он отвечает «пусто» на домены рекламы
# и слежки (фильтр AdGuard DNS — в нём и российские рекламные сети). Список обновляется раз в сутки.
# Конфиги устройств получают DNS = 10.8.0.1; у отдельного устройства блокировку можно выключить:
# vpn ads ИМЯ off (или кнопкой в боте).
#
# Рекламу ВНУТРИ приложения YouTube так не убрать: она идёт с тех же серверов, что и видео.
#
#   sudo bash adblock.sh        — включить
#   sudo bash adblock.sh off    — выключить (конфиги снова с обычным DNS)
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1

die() { echo "Ошибка: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "запустите от root"
[[ -f /etc/vpn/server.json ]] || die "сначала установите VPN: bash install.sh"

CONF=/etc/dnsmasq.d/vpn-adblock.conf
LIST=/var/lib/vpn/adblock.conf
UPDATE=/usr/local/sbin/vpn-adblock-update
FLAG=/etc/vpn/adblock-on
DNS_IP=10.8.0.1

if [[ "${1:-}" == "off" ]]; then
  systemctl disable --now vpn-adblock-update.timer dnsmasq 2>/dev/null || true
  rm -f "$CONF" /etc/systemd/system/vpn-adblock-update.{service,timer} /etc/systemd/system/dnsmasq.service.d/vpn.conf "$FLAG"
  touch /etc/vpn/adblock-disabled   # чтобы install.sh не включал её снова
  systemctl daemon-reload
  VPN_PUSH_REASON="Выключена блокировка рекламы" vpn regen
  echo "Блокировка рекламы выключена. Конфиги снова с обычным DNS — импортируйте их заново."
  exit 0
fi

echo "==> Ставлю dnsmasq"
mkdir -p /etc/dnsmasq.d /var/lib/vpn
[[ -f "$LIST" ]] || : >"$LIST"
cat >"$CONF" <<EOF
# DNS для устройств VPN с блокировкой рекламы. Слушает только интерфейс VPN — из интернета закрыт.
# Только адрес VPN: на Ubuntu 24.04 порт 53 на 127.0.0.53 занят systemd-resolved, а сам пакет
# добавляет --bind-interfaces (с ним нельзя bind-dynamic — dnsmasq не запускается).
listen-address=$DNS_IP
bind-interfaces
no-resolv
no-hosts
server=1.1.1.1
server=8.8.8.8
cache-size=10000
domain-needed
bogus-priv
conf-file=$LIST
EOF
apt-get -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef \
  install -y -qq dnsmasq curl dnsutils >/dev/null || true
command -v dnsmasq >/dev/null || die "не удалось установить dnsmasq"
# Стандартный конфиг пакета занял бы порт 53 на всех адресах (там уже systemd-resolved) — читаем только
# /etc/dnsmasq.d и не делаем dnsmasq системным DNS самого сервера.
touch /etc/default/dnsmasq
sed -i '/^CONFIG_DIR=/d; /^DNSMASQ_EXCEPT=/d; /^IGNORE_RESOLVCONF=/d' /etc/default/dnsmasq
printf 'CONFIG_DIR=/etc/dnsmasq.d,.dpkg-dist,.dpkg-old,.dpkg-new\nDNSMASQ_EXCEPT="lo"\nIGNORE_RESOLVCONF=yes\n' \
  >>/etc/default/dnsmasq
dpkg --configure -a >/dev/null 2>&1 || true
# Запускать после VPN (адрес 10.8.0.1 появляется вместе с awg0) и подниматься после сбоя самому.
mkdir -p /etc/systemd/system/dnsmasq.service.d
cat >/etc/systemd/system/dnsmasq.service.d/vpn.conf <<'UNIT'
[Unit]
After=awg-quick@awg0.service
Wants=awg-quick@awg0.service

[Service]
Restart=on-failure
RestartSec=5
UNIT
systemctl daemon-reload

cat >"$UPDATE" <<'EOF'
#!/bin/bash
# Обновить список рекламных доменов (фильтр AdGuard DNS) для dnsmasq.
LIST=/var/lib/vpn/adblock.conf
URL=https://adguardteam.github.io/AdGuardSDNSFilter/Filters/filter.txt
# Не блокировать (домен и все поддомены): плеер YouTube ждёт рекламную вставку от серверов Google
# и зависает, если её заблокировать. Свои исключения — по домену на строку в /etc/vpn/adblock-allow.txt.
ALLOW="youtube.com youtu.be youtube-nocookie.com googlevideo.com ytimg.com ggpht.com googleapis.com gstatic.com
google.com doubleclick.net googlesyndication.com googleadservices.com googletagservices.com"
allow_re() {
  { printf '%s\n' $ALLOW; [[ -f /etc/vpn/adblock-allow.txt ]] && grep -oE '^[a-z0-9._-]+' /etc/vpn/adblock-allow.txt; } \
    | sed 's/\./\\./g' | paste -sd'|' | sed 's/.*/(^|\\.)(&)$/'
}
tmp=$(mktemp); raw=$(mktemp)
# «||домен^» — блокировать домен и поддомены; «@@||домен^» — исключение, такие не блокируем.
if curl -fsSL --max-time 60 "$URL" -o "$raw" \
   && sed -nE 's/^\|\|([a-z0-9._-]+)\^$/\1/p' "$raw" | sort -u \
      | grep -vxF -f <(sed -nE 's/^@@\|\|([a-z0-9._-]+)\^.*$/\1/p' "$raw"; echo .) \
      | grep -vE "$(allow_re)" \
      | sed 's|.*|address=/&/#|' >"$tmp" \
   && [[ $(wc -l <"$tmp") -gt 10000 ]]; then
  install -m 644 "$tmp" "$LIST"
  systemctl restart dnsmasq
  logger -t vpn-adblock "список обновлён: $(wc -l <"$LIST") доменов"
else
  logger -t vpn-adblock "не удалось скачать список, оставляю прежний"
fi
rm -f "$tmp" "$raw"
EOF
chmod 755 "$UPDATE"

cat >/etc/systemd/system/vpn-adblock-update.service <<EOF
[Unit]
Description=VPN: обновить список рекламных доменов
After=network-online.target
[Service]
Type=oneshot
ExecStart=$UPDATE
EOF
cat >/etc/systemd/system/vpn-adblock-update.timer <<'EOF'
[Unit]
Description=VPN: обновлять список рекламных доменов раз в сутки
[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload

echo "==> Скачиваю список рекламных доменов"
"$UPDATE"
echo "    доменов в списке: $(wc -l <"$LIST")"
systemctl enable dnsmasq vpn-adblock-update.timer >/dev/null 2>&1
systemctl restart dnsmasq
systemctl start vpn-adblock-update.timer
sleep 2
if ! systemctl is-active --quiet dnsmasq || ! ss -lun | grep -q "$DNS_IP:53 "; then
  journalctl -u dnsmasq -n 20 --no-pager
  die "dnsmasq не запустился — пришлите этот вывод. Устройства пока работают с обычным DNS."
fi

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow in on awg0 to any port 53 comment "VPN DNS" >/dev/null
fi
/usr/local/sbin/vpn-rules up   # правило «DNS из VPN разрешён» (если INPUT закрыт)

# Проверка: рекламный домен должен вернуть 0.0.0.0, обычный — настоящий адрес.
AD_HOST="$(grep -m1 -oE '^address=/[a-z0-9.-]+\.[a-z]+/#' "$LIST" | cut -d/ -f2)"
AD="$(dig +short @"$DNS_IP" "${AD_HOST:-ads.example.com}" 2>/dev/null | head -1 || true)"
OK="$(dig +short @"$DNS_IP" example.com 2>/dev/null | head -1 || true)"
echo "    проверка: $AD_HOST -> ${AD:-?} (должно быть 0.0.0.0), example.com -> ${OK:-?}"

rm -f /etc/vpn/adblock-disabled
NEW=0; [[ -f "$FLAG" ]] || NEW=1
touch "$FLAG"
if [[ $NEW == 1 ]]; then
  VPN_PUSH_REASON="Включена блокировка рекламы" vpn regen
fi

cat <<EOF

Готово: реклама и трекеры блокируются для устройств с DNS $DNS_IP.
Конфиги пересобраны — импортируйте заново (бот пришлёт их сам).
Выключить у одного устройства: vpn ads ИМЯ off   (или кнопка «🛡 Реклама» в боте)
Выключить для всех: bash adblock.sh off
Рекламу внутри приложения YouTube так не убрать — она идёт с тех же серверов, что и видео.
EOF
