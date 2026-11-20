#!/bin/bash
# vpn-warp — policy routing для трафика из VPN: что выходит в интернет через Cloudflare WARP,
# а что — с IP сервера. Ставит warp.sh; вызывается из wg-quick@warp (PostUp/PostDown), сторожем,
# vpn tiktok и vpn route.
#
#   rules-up    правила: адреса из списка WARP (TikTok + vpn route ... warp) и устройства с ☁️ -> метка ->
#               своя таблица маршрутов через warp; адреса из списка VPN (vpn route ... vpn) — никогда в WARP
#   rules-down  снять правила
#   refresh     обновить списки адресов (сети ByteDance, адреса доменов) и подсказки для DNS сервера
#   check       всё ли на месте (код 0 — да)
#   test        через какой IP сервер выходит в интернет по WARP
#
# Если интерфейс warp упал, маршрута в таблице нет — помеченные пакеты идут обычным путём, с IP сервера.

SET=vpn_warp        # адреса -> через WARP
NOSET=vpn_nowarp    # адреса -> всегда с IP сервера, даже у устройств «весь трафик через WARP»
MARK=0x77
TABLE=7777
WARP=warp
ROUTE_WARP=/etc/vpn/route-warp.txt
ROUTE_VPN=/etc/vpn/route-vpn.txt
DNSMASQ_CONF=/etc/dnsmasq.d/vpn-warp.conf
ENV=/etc/vpn/env
# shellcheck disable=SC1090
[[ -f $ENV ]] && . "$ENV"
IFACE="${IFACE:-awg0}"

TIKTOK_DOMAINS="tiktok.com tiktokv.com tiktokcdn.com tiktokcdn-us.com tiktokv.us tiktokv.eu tiktokw.us ttwstatic.com
ibytedtos.com ibyteimg.com byteimg.com byteoversea.com bytedance.com byteeffecttos-g.com musical.ly ttlivecdn.com"
TIKTOK_HOSTS="www.tiktok.com m.tiktok.com api16-normal-c-useast1a.tiktokv.com api22-normal-c-alisg.tiktokv.com
v16m.tiktokcdn.com v19.tiktokcdn.com p16-sign-va.tiktokcdn.com log.tiktokv.com mon.tiktokv.com
webcast.tiktok.com im-api-va.tiktok.com lf16-tiktok-web.tiktokcdn-us.com"

ensure_sets() {
  # Адреса живут 2 суток с последнего обновления: сервис переезжает — старые уходят сами.
  ipset create "$SET" hash:net timeout 172800 -exist
  ipset create "$NOSET" hash:net timeout 172800 -exist
}

# Строки файла маршрутов без комментариев: домены и IPv4-сети.
entries() { [[ -f $1 ]] && sed -E 's/#.*//; s/[[:space:]]+//g' "$1" | grep -E '^[a-z0-9.*/-]+$' | grep -v '^$'; }
domains() { entries "$1" | grep -vE '^[0-9./]+$' | sed 's/^\*\.//'; }
cidrs()   { entries "$1" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(/[0-9]+)?$'; }

rules_down() {
  while iptables -t mangle -D PREROUTING -j VPN_WARP 2>/dev/null; do :; done
  iptables -t mangle -F VPN_WARP 2>/dev/null; iptables -t mangle -X VPN_WARP 2>/dev/null
  while iptables -t mangle -D FORWARD -j VPN_WARP_MSS 2>/dev/null; do :; done
  iptables -t mangle -F VPN_WARP_MSS 2>/dev/null; iptables -t mangle -X VPN_WARP_MSS 2>/dev/null
  while iptables -t nat -D POSTROUTING -j VPN_WARP_NAT 2>/dev/null; do :; done
  iptables -t nat -F VPN_WARP_NAT 2>/dev/null; iptables -t nat -X VPN_WARP_NAT 2>/dev/null
  while ip rule del fwmark "$MARK" lookup "$TABLE" 2>/dev/null; do :; done
  ip route flush table "$TABLE" 2>/dev/null
  return 0
}

rules_up() {
  rules_down
  ensure_sets
  iptables -t mangle -N VPN_WARP
  # Локальные адреса (DNS сервера 10.8.0.1 и т.п.) — никогда в WARP.
  for n in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10; do
    iptables -t mangle -A VPN_WARP -d "$n" -j RETURN
  done
  # Список «через VPN» (vpn route ДОМЕН vpn) — с IP сервера всегда, даже у устройств с ☁️.
  iptables -t mangle -A VPN_WARP -m set --match-set "$NOSET" dst -j RETURN
  # Список «через WARP»: TikTok и vpn route ДОМЕН warp.
  iptables -t mangle -A VPN_WARP -i "$IFACE" -m set --match-set "$SET" dst -j MARK --set-mark "$MARK"
  # Устройства «весь трафик через WARP» (vpn warp ИМЯ on): все их пакеты — с меткой.
  if [[ -f /etc/vpn/warp-devices ]]; then
    while read -r ip; do
      [[ $ip =~ ^[0-9.]+$ ]] && iptables -t mangle -A VPN_WARP -i "$IFACE" -s "$ip" -j MARK --set-mark "$MARK"
    done </etc/vpn/warp-devices
  fi
  iptables -t mangle -A PREROUTING -j VPN_WARP
  # Помеченные — через warp. Нет маршрута (warp упал) — ядро идёт к следующему правилу, обычным путём.
  ip route replace default dev "$WARP" table "$TABLE"
  ip rule add fwmark "$MARK" lookup "$TABLE" priority 7777
  iptables -t nat -N VPN_WARP_NAT
  iptables -t nat -A VPN_WARP_NAT -o "$WARP" -j MASQUERADE
  iptables -t nat -I POSTROUTING 1 -j VPN_WARP_NAT
  # MTU warp 1280 -> MSS 1240, как и у awg0.
  iptables -t mangle -N VPN_WARP_MSS
  iptables -t mangle -A VPN_WARP_MSS -o "$WARP" -p tcp --tcp-flags SYN,RST SYN \
    -m tcpmss --mss 1241:65535 -j TCPMSS --set-mss 1240
  iptables -t mangle -I FORWARD 1 -j VPN_WARP_MSS
  # Ответы приходят с warp, а «обратный» маршрут к ним — через основной интерфейс:
  # строгая проверка источника (rp_filter=1) их бы отбросила. 2 — нестрогая.
  sysctl -qw net.ipv4.conf.all.rp_filter=2 "net.ipv4.conf.$WARP.rp_filter=2" 2>/dev/null
  refresh
}

resolve() {  # домены -> их IPv4 сейчас (и www.), через 1.1.1.1 — как у устройств
  local d
  for d in "$@"; do
    dig +short +time=2 +tries=1 A "$d" @1.1.1.1 2>/dev/null
    [[ $d == www.* ]] || dig +short +time=2 +tries=1 A "www.$d" @1.1.1.1 2>/dev/null
  done | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'
}

fill() {  # набор, затем строки-адреса на stdin
  local set=$1 n
  sort -u | while read -r n; do ipset add "$set" "$n" -exist; done
}

dnsmasq_conf() {
  # DNS сервера (блокировка рекламы) добавляет адреса доменов в списки в момент запроса —
  # так в список попадают ровно те адреса, на которые пошло устройство.
  systemctl is-active --quiet dnsmasq || return 0
  local tmp w v
  tmp=$(mktemp)
  # shellcheck disable=SC2086
  w="$(printf '%s\n' $TIKTOK_DOMAINS; domains "$ROUTE_WARP")"
  v="$(domains "$ROUTE_VPN")"
  [[ -n $w ]] && echo "ipset=/$(sort -u <<<"$w" | paste -sd/)/$SET" >>"$tmp"
  [[ -n $v ]] && echo "ipset=/$(sort -u <<<"$v" | paste -sd/)/$NOSET" >>"$tmp"
  if ! cmp -s "$tmp" "$DNSMASQ_CONF"; then
    install -m 644 "$tmp" "$DNSMASQ_CONF"
    systemctl restart dnsmasq
  fi
  rm -f "$tmp"
}

refresh() {
  ensure_sets
  # Через WARP: сети TikTok/ByteDance, адреса его доменов, свой список (домены и сети).
  {
    /usr/local/bin/vpn tiktok --print 2>/dev/null
    # shellcheck disable=SC2086
    resolve $TIKTOK_HOSTS
    cidrs "$ROUTE_WARP"
    # shellcheck disable=SC2046
    resolve $(domains "$ROUTE_WARP")
  } | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(/[0-9]+)?$' | fill "$SET"
  # Через VPN (с IP сервера): свой список.
  {
    cidrs "$ROUTE_VPN"
    # shellcheck disable=SC2046
    resolve $(domains "$ROUTE_VPN")
  } | fill "$NOSET"
  dnsmasq_conf
}

check() {
  ipset list -n "$SET" >/dev/null 2>&1 &&
  ipset list -n "$NOSET" >/dev/null 2>&1 &&
  iptables -t mangle -C PREROUTING -j VPN_WARP 2>/dev/null &&
  iptables -t mangle -C VPN_WARP -m set --match-set "$NOSET" dst -j RETURN 2>/dev/null &&
  iptables -t nat -C POSTROUTING -j VPN_WARP_NAT 2>/dev/null &&
  ip rule show | grep -q "fwmark $MARK lookup $TABLE" &&
  ip route show table "$TABLE" | grep -q "dev $WARP"
}

case "${1:-}" in
  rules-up) rules_up ;;
  rules-down) rules_down ;;
  refresh) refresh ;;
  check) check ;;
  test) curl -4 -s --max-time 10 --interface "$WARP" https://www.cloudflare.com/cdn-cgi/trace | grep -E '^(ip|warp|loc)=' ;;
  *) echo "Использование: $0 rules-up|rules-down|refresh|check|test" >&2; exit 1 ;;
esac
