#!/bin/bash
# vpn-watchdog — сторож VPN. Запускается раз в минуту (vpn-watchdog.timer) и сам чинит то, что сломалось:
#   * не загружен модуль ядра (после обновления ядра) -> пересобирает и загружает;
#   * выключилась пересылка пакетов -> включает;
#   * упал интерфейс awg0 или слушает не тот порт -> перезапускает; не помогло 3 раза -> пересоздаёт
#     конфиг сервера из сохранённых настроек и перезапускает;
#   * пропали правила iptables (их снёс ufw, docker, другой скрипт) -> ставит заново;
#   * сменился сетевой интерфейс выхода -> переписывает NAT;
#   * сменился внешний IP сервера -> пересобирает конфиги устройств под новый IP.
# Журнал: /var/log/vpn-watchdog.log

IFACE=awg0
LOG=/var/log/vpn-watchdog.log
FAILS=/run/vpn-watchdog.fails
SERVER=/etc/vpn/server.json
LOCK=/run/vpn-watchdog.lock

exec 9>"$LOCK"
flock -n 9 || exit 0

log() {
  echo "$(date '+%F %T') $*" >>"$LOG"; logger -t vpn-watchdog "$*"
  # Если настроен Telegram-бот — пишем туда же (в фоне, чтобы сторож не ждал сеть).
  if [[ -x /usr/local/bin/vpn-bot && -f /etc/vpn/bot.env ]]; then
    (timeout 30 /usr/local/bin/vpn-bot notify "🛠 Сторож: $*" >/dev/null 2>&1 &)
  fi
}
jget() { python3 -c "import json,sys; print(json.load(open('$SERVER'))['$1'])"; }

[[ -f $SERVER ]] || exit 0

# Журнал не должен расти бесконечно.
if [[ -f $LOG ]] && (( $(stat -c %s "$LOG") > 1048576 )); then
  tail -n 2000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# 1. Модуль ядра
if ! lsmod | grep -q '^amneziawg'; then
  if ! modprobe amneziawg 2>/dev/null; then
    log "модуль amneziawg не загружается (ядро $(uname -r)) — пересобираю"
    export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
    apt-get install -y -qq "linux-headers-$(uname -r)" >/dev/null 2>&1
    dkms autoinstall >/dev/null 2>&1
    if modprobe amneziawg 2>/dev/null; then log "модуль пересобран и загружен"
    else log "ОШИБКА: модуль не собрался, нужна ручная проверка: dkms status; dmesg | tail"; exit 1; fi
  fi
fi

# 2. Пересылка пакетов
if [[ "$(sysctl -n net.ipv4.ip_forward)" != 1 ]]; then
  sysctl -qw net.ipv4.ip_forward=1
  log "пересылка пакетов была выключена — включил"
fi

# 3. Интерфейс выхода в интернет
WAN_NOW="$(ip -4 route show default | awk '{print $5; exit}')"
if [[ -n "$WAN_NOW" && "$WAN_NOW" != "$(jget wan)" ]]; then
  log "интерфейс выхода сменился: $(jget wan) -> $WAN_NOW — обновляю NAT"
  /usr/local/bin/vpn init --wan "$WAN_NOW" >/dev/null 2>&1 && log "NAT обновлён"
fi

# 4. Интерфейс VPN
PORT="$(jget port)"
healthy() {
  ip link show "$IFACE" >/dev/null 2>&1 && [[ "$(awg show "$IFACE" listen-port 2>/dev/null)" == "$PORT" ]]
}
if ! healthy; then
  n=$(( $(cat "$FAILS" 2>/dev/null || echo 0) + 1 ))
  echo "$n" >"$FAILS"
  if (( n <= 3 )); then
    log "VPN не работает (попытка $n) — перезапускаю"
    systemctl restart "awg-quick@$IFACE" 2>>"$LOG"
  else
    log "VPN не поднимается $((n - 1)) раз подряд — пересоздаю конфиг сервера из /etc/vpn и перезапускаю"
    /usr/local/bin/vpn apply --restart >>"$LOG" 2>&1
  fi
  if healthy; then log "VPN снова работает"; rm -f "$FAILS"; fi
else
  rm -f "$FAILS"
fi

# 5. Правила iptables
if healthy && ! /usr/local/sbin/vpn-rules check; then
  log "правила iptables пропали — ставлю заново"
  /usr/local/sbin/vpn-rules up 2>>"$LOG" && log "правила восстановлены"
fi

# 6. Внешний IP (раз в 10 минут, чтобы не дёргать сервисы). Если IP задан вручную (install.sh --ip),
#    есть файл /etc/vpn/ip-fixed и IP не трогаем.
if (( 10#$(date +%M) % 10 == 0 )); then
  [[ -f /etc/vpn/ip-fixed ]] || IP_NOW="$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || curl -4 -fsS --max-time 8 https://ifconfig.me 2>/dev/null)"
  if [[ "${IP_NOW:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ && "$IP_NOW" != "$(jget ip)" ]]; then
    log "внешний IP сменился: $(jget ip) -> $IP_NOW — пересобираю конфиги устройств"
    /usr/local/bin/vpn init --ip "$IP_NOW" >>"$LOG" 2>&1
    log "ВНИМАНИЕ: новые конфиги в /root/vpn-configs — импортируйте их на устройства (если есть бот — он их пришлёт)"
  fi
  # Есть ли у самого сервера интернет (если нет — проблема у хостинга, а не у VPN).
  if ! ping -c 2 -W 3 -q 1.1.1.1 >/dev/null 2>&1 && ! ping -c 2 -W 3 -q 8.8.8.8 >/dev/null 2>&1; then
    log "у сервера нет интернета (1.1.1.1 и 8.8.8.8 не отвечают) — проверьте хостинг"
  fi
fi
exit 0
