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
  # В бота — то же сообщение не чаще раза в час (иначе повторяющийся сбой спамит каждую минуту).
  local key last
  key="/run/vpn-watchdog.sent.$(printf '%s' "$*" | md5sum | cut -c1-12)"
  last=$(stat -c %Y "$key" 2>/dev/null || echo 0)
  (( $(date +%s) - last < 3600 )) && return 0
  touch "$key"
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

# 7. Блокировка рекламы: если dnsmasq упал, у устройств с ней не открываются сайты — поднимаем.
if [[ -f /etc/vpn/adblock-on ]] && healthy && ! ss -lun | grep -q "10.8.0.1:53 "; then
  log "DNS с блокировкой рекламы (dnsmasq) не работает — перезапускаю"
  systemctl restart dnsmasq 2>>"$LOG"
  sleep 2
  ss -lun | grep -q "10.8.0.1:53 " && log "dnsmasq снова работает"
fi

# 8. TikTok через Cloudflare WARP (warp.sh): туннель поднят, WARP отвечает, список адресов свежий.
#    WARP не отвечает — убираем маршрут, чтобы TikTok шёл хотя бы с IP сервера, а не пропадал.
if [[ -f /etc/vpn/warp-on ]]; then
  if ! ip link show warp >/dev/null 2>&1; then
    systemctl restart wg-quick@warp 2>>"$LOG"
    if ip link show warp >/dev/null 2>&1; then
      log "туннель WARP падал — поднял"; rm -f /run/vpn-watchdog.warp-fails
    else
      n=$(( $(cat /run/vpn-watchdog.warp-fails 2>/dev/null || echo 0) + 1 )); echo "$n" >/run/vpn-watchdog.warp-fails
      log "туннель WARP не поднимается (TikTok идёт с IP сервера) — проверьте: bash warp.sh status"
      if (( n >= 60 )); then  # час без успеха — перестаём пытаться
        rm -f /etc/vpn/warp-on /run/vpn-watchdog.warp-fails
        log "WARP так и не поднялся за час — сторож больше его не трогает. Включить снова: bash warp.sh"
      fi
    fi
  fi
  if ip link show warp >/dev/null 2>&1; then
    if /usr/local/sbin/vpn-warp test 2>/dev/null | grep -q 'warp=on'; then
      if ! /usr/local/sbin/vpn-warp check; then
        /usr/local/sbin/vpn-warp rules-up 2>>"$LOG" && log "WARP работает — TikTok снова идёт через WARP"
      elif (( 10#$(date +%M) % 10 == 5 )); then
        /usr/local/sbin/vpn-warp refresh 2>>"$LOG"
      fi
    elif ip route show table 7777 2>/dev/null | grep -q .; then
      ip route flush table 7777
      log "WARP не отвечает — TikTok временно идёт с IP сервера"
    fi
  fi
fi

# 9. Ресурсы сервера (как мониторинг в MOST): о проблеме, которая держится 2 проверки подряд, —
#    🔴 в бота; когда прошла — 🟢.
alert() {  # имя, 1/0 (плохо?), текст проблемы, текст «прошло»
  local f="/run/vpn-watchdog.alert.$1" n
  if [[ $2 == 1 ]]; then
    n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 )); echo "$n" >"$f"
    if (( n == 2 )); then
      echo "$(date '+%F %T') 🔴 $3" >>"$LOG"
      [[ -x /usr/local/bin/vpn-bot && -f /etc/vpn/bot.env ]] && (timeout 30 /usr/local/bin/vpn-bot notify "🔴 $3" >/dev/null 2>&1 &)
    fi
  elif [[ -f "$f" ]]; then
    if (( $(cat "$f") >= 2 )); then
      echo "$(date '+%F %T') 🟢 $4" >>"$LOG"
      [[ -x /usr/local/bin/vpn-bot && -f /etc/vpn/bot.env ]] && (timeout 30 /usr/local/bin/vpn-bot notify "🟢 $4" >/dev/null 2>&1 &)
    fi
    rm -f "$f"
  fi
}
read -r _ u1 n1 s1 i1 w1 q1 sq1 _ </proc/stat; sleep 1; read -r _ u2 n2 s2 i2 w2 q2 sq2 _ </proc/stat
busy=$(( (u2 + n2 + s2 + q2 + sq2) - (u1 + n1 + s1 + q1 + sq1) )); idle=$(( (i2 + w2) - (i1 + w1) ))
CPU=$(( 100 * busy / (busy + idle > 0 ? busy + idle : 1) ))
MEM=$(awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END {printf "%d", 100 * (1 - a / t)}' /proc/meminfo)
DISK=$(df -P / | awk 'NR==2 {gsub("%", "", $5); print $5}')
alert cpu  $(( CPU >= 90 ))  "процессор сервера загружен на ${CPU}% — VPN может тормозить" "процессор снова в норме (${CPU}%)"
alert mem  $(( MEM >= 90 ))  "память сервера занята на ${MEM}%" "память снова в норме (${MEM}%)"
alert disk $(( DISK >= 90 )) "диск сервера заполнен на ${DISK}% — почистите место (журналы: journalctl --vacuum-size=100M)" "место на диске есть (${DISK}%)"
exit 0
