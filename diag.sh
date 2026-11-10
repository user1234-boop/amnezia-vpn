#!/usr/bin/env bash
# Диагностика скорости VPN (как diag.sh в MOST) — короткий отчёт для скриншота.
#
#   sudo bash diag.sh        — проверить (ничего не меняет)
#   sudo bash diag.sh fix    — включить сетевые настройки для скорости (буферы UDP, BBR, очередь, conntrack)
set -uo pipefail
[[ $EUID -eq 0 ]] || { echo "запустите от root"; exit 1; }
SYSCTL=/etc/sysctl.d/99-vpn-speed.conf

if [[ "${1:-}" == "fix" ]]; then
  # AmneziaWG — это UDP: маленькие буферы при большой скорости = потери пакетов.
  # BBR + fq — TCP внутри туннеля меньше проседает на потерях по дороге.
  modprobe tcp_bbr 2>/dev/null || true
  cat >"$SYSCTL" <<'EOF'
# Настройки сети для скорости VPN (diag.sh fix)
net.core.rmem_max = 33554432
net.core.wmem_max = 33554432
net.core.rmem_default = 4194304
net.core.wmem_default = 4194304
net.core.netdev_max_backlog = 16384
net.core.netdev_budget = 600
net.ipv4.udp_rmem_min = 16384
net.ipv4.udp_wmem_min = 16384
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_mtu_probing = 1
net.netfilter.nf_conntrack_max = 262144
net.netfilter.nf_conntrack_udp_timeout = 60
net.netfilter.nf_conntrack_udp_timeout_stream = 180
EOF
  sysctl -q -p "$SYSCTL" 2>&1 | grep -v 'No such file' || true
  # Кольцевые буферы сетевой карты — до максимума и очередь fq; повторяется при загрузке (vpn-speed.service).
  command -v ethtool >/dev/null || apt-get install -y -qq ethtool >/dev/null 2>&1 || true
  cat >/usr/local/sbin/vpn-speed <<'SPEED'
#!/bin/bash
# Буферы сетевой карты и очередь fq (diag.sh fix)
WAN="$(ip -4 route show default | awk '{print $5; exit}')"
[[ -n "$WAN" ]] || exit 0
if command -v ethtool >/dev/null; then
  RX=$(ethtool -g "$WAN" 2>/dev/null | awk '/maximums/{m=1} m && /^RX:/{print $2; exit}')
  TX=$(ethtool -g "$WAN" 2>/dev/null | awk '/maximums/{m=1} m && /^TX:/{print $2; exit}')
  [[ $RX =~ ^[0-9]+$ ]] && ethtool -G "$WAN" rx "$RX" 2>/dev/null
  [[ $TX =~ ^[0-9]+$ ]] && ethtool -G "$WAN" tx "$TX" 2>/dev/null
fi
tc qdisc replace dev "$WAN" root fq 2>/dev/null
exit 0
SPEED
  chmod 755 /usr/local/sbin/vpn-speed
  cat >/etc/systemd/system/vpn-speed.service <<'UNIT'
[Unit]
Description=VPN: буферы сетевой карты и очередь для скорости
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/vpn-speed
[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable vpn-speed >/dev/null 2>&1
  /usr/local/sbin/vpn-speed
  WAN="$(ip -4 route show default | awk '{print $5; exit}')"
  echo "Готово: настройки включены и сохранятся после перезагрузки."
  echo "    буферы UDP: $(sysctl -n net.core.rmem_max) · TCP: $(sysctl -n net.ipv4.tcp_congestion_control) · очередь: $(tc qdisc show dev "$WAN" | head -1 | awk '{print $2}')"
  # Туннель пересоздаёт сокет — так он берёт новые буферы (устройства переподключатся за пару секунд).
  systemctl restart awg-quick@awg0 && echo "    перезапущен awg0"
  echo "Проверьте снова: bash diag.sh"
  exit 0
fi

h() { echo; echo "== $*"; }
WAN="$(ip -4 route show default | awk '{print $5; exit}')"

echo "VPN diag · $(hostname) · $(date '+%d.%m %H:%M')"
h "Сервер"
echo "ядер: $(nproc) · нагрузка: $(cut -d' ' -f1-3 /proc/loadavg) · память свободна: $(free -m | awk '/Mem/{print $7}') МБ"
echo "процессор: $(awk -F: '/model name/{print $2; exit}' /proc/cpuinfo | xargs)"
echo "softirq (сеть), %: $(top -bn1 | awk '/%Cpu/{for(i=1;i<=NF;i++) if($i ~ /si/) print $(i-1)}' | head -1)"

h "Настройки сети"
for k in net.core.rmem_max net.core.wmem_max net.core.netdev_max_backlog net.ipv4.tcp_congestion_control net.core.default_qdisc; do
  printf '%s = %s\n' "${k##*.}" "$(sysctl -n $k 2>/dev/null)"
done
CT="$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null || echo '?')"
CTM="$(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null || echo '?')"
echo "conntrack: $CT из $CTM"
echo "очередь $WAN: $(tc qdisc show dev "$WAN" 2>/dev/null | head -1 | awk '{print $2}')"

h "Потери пакетов (с запуска сервера)"
nstat -az 2>/dev/null | awk '$1 ~ /UdpRcvbufErrors|UdpSndbufErrors|UdpInErrors|TcpRetransSegs|TcpOutSegs/ {printf "%s %s\n", $1, $2}'
d=0; sq=0; while read -r _ a b _; do d=$((d + 16#$a)); sq=$((sq + 16#$b)); done </proc/net/softnet_stat
echo "softnet отброшено: $d · не успели: $sq"
for i in $WAN awg0; do
  ip link show "$i" >/dev/null 2>&1 || continue
  ip -s link show "$i" | awk -v i="$i" '/mtu/{for(n=1;n<=NF;n++) if($n=="mtu") m=$(n+1)}
    /RX:/{getline; rx=$1; rd=$4} /TX:/{getline; tx=$1; td=$4}
    END {printf "%s: mtu %s · принято %.1f ГБ, отброшено %s · отправлено %.1f ГБ, отброшено %s\n", i, m, rx/1e9, rd, tx/1e9, td}'
done

if command -v awg >/dev/null; then
  h "VPN"
  now=$(date +%s)
  awg show awg0 latest-handshakes 2>/dev/null | awk -v n="$now" '{t++; if ($2 && n-$2 < 180) on++} END {print "устройств:", t, "· подключены сейчас:", on+0}'
  if [[ -f /etc/vpn/adblock-on ]]; then
    systemctl is-active --quiet dnsmasq && echo "блокировка рекламы: работает" || echo "блокировка рекламы: НЕ работает"
  fi
fi

h "Скорость канала сервера (без VPN)"
down=$(curl -4 -s -o /dev/null -w '%{speed_download}' --max-time 20 'https://speed.cloudflare.com/__down?bytes=25000000')
[[ "${down%%.*}" -gt 0 ]] 2>/dev/null || down=$(curl -4 -s -o /dev/null -w '%{speed_download}' --max-time 20 \
  'https://proof.ovh.net/files/100Mb.dat')
echo "скачивание: $(awk -v b="${down:-0}" 'BEGIN{printf "%.0f", b*8/1e6}') Мбит/с"
up=$(head -c 30000000 /dev/urandom | curl -4 -s -o /dev/null -w '%{speed_upload}' --max-time 20 --data-binary @- 'https://speed.cloudflare.com/__up')
echo "отдача: $(awk -v b="${up:-0}" 'BEGIN{printf "%.0f", b*8/1e6}') Мбит/с"

h "Связь с интернетом"
ping -c 20 -i 0.2 -q 1.1.1.1 2>/dev/null | awk -F'[ /,]+' '/packet loss/{for(i=1;i<=NF;i++) if($i ~ /%/) l=$i} /rtt|round-trip/{a=$8; m=$10} END {print "до 1.1.1.1: потери", l, "· пинг", a, "мс · разброс", m, "мс"}'
echo
echo "Пришлите скриншот всего отчёта."
