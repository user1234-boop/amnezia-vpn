#!/bin/bash
# vpn-warp — трафик TikTok с VPN уходит в интернет через Cloudflare WARP (не с IP сервера).
# Ставит warp.sh; вызывается из wg-quick@warp (PostUp/PostDown), сторожем и vpn tiktok.
#
#   rules-up    правила: адреса TikTok из VPN -> метка -> таблица маршрутов через warp
#   rules-down  снять правила
#   refresh     обновить список адресов TikTok (сети ByteDance + текущие адреса доменов)
#   check       всё ли на месте (код 0 — да)
#   test        через какой IP сервер выходит в интернет по WARP
#
# Если интерфейс warp упал, маршрута в таблице нет — пакеты TikTok идут обычным путём, с IP сервера.

SET=vpn_warp
MARK=0x77
TABLE=7777
WARP=warp
ENV=/etc/vpn/env
# shellcheck disable=SC1090
[[ -f $ENV ]] && . "$ENV"
IFACE="${IFACE:-awg0}"

ensure_set() {
  # Адреса живут 2 суток с последнего обновления: TikTok переезжает — старые уходят сами.
  ipset create "$SET" hash:net timeout 172800 -exist
}

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
  ensure_set
  # Пакеты из VPN к адресам TikTok — с меткой. Локальные адреса (DNS сервера 10.8.0.1 и т.п.) — никогда.
  iptables -t mangle -N VPN_WARP
  for n in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10; do
    iptables -t mangle -A VPN_WARP -d "$n" -j RETURN
  done
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
  # Ответы TikTok приходят с warp, а «обратный» маршрут к ним — через основной интерфейс:
  # строгая проверка источника (rp_filter=1) их бы отбросила. 2 — нестрогая.
  sysctl -qw net.ipv4.conf.all.rp_filter=2 "net.ipv4.conf.$WARP.rp_filter=2" 2>/dev/null
  refresh
}

refresh() {
  ensure_set
  # Сети TikTok/ByteDance и адреса его доменов прямо сейчас (с этого сервера и через 1.1.1.1).
  {
    /usr/local/bin/vpn tiktok --print 2>/dev/null
    for h in www.tiktok.com m.tiktok.com api16-normal-c-useast1a.tiktokv.com api22-normal-c-alisg.tiktokv.com \
             v16m.tiktokcdn.com v19.tiktokcdn.com p16-sign-va.tiktokcdn.com log.tiktokv.com mon.tiktokv.com \
             webcast.tiktok.com im-api-va.tiktok.com lf16-tiktok-web.tiktokcdn-us.com; do
      dig +short +time=2 +tries=1 A "$h" @1.1.1.1 2>/dev/null
    done
  } | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(/[0-9]+)?$' | sort -u | while read -r n; do
    ipset add "$SET" "$n" -exist
  done
}

check() {
  ipset list -n "$SET" >/dev/null 2>&1 &&
  iptables -t mangle -C PREROUTING -j VPN_WARP 2>/dev/null &&
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
