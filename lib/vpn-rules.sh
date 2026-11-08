#!/bin/bash
# vpn-rules — правила iptables для AmneziaWG. Вызывается из awg0.conf (PostUp/PostDown) и сторожем.
#
#   up     поставить правила (сначала снимает старые)
#   down   снять
#   check  все ли правила на месте (код возврата 0 — да)
#
# Все правила живут в своих цепочках VPN_*, поэтому снимаются целиком, какие бы порты ни были раньше.
#   nat    VPN_PRE   резервные порты -> основной порт AmneziaWG
#   nat    VPN_POST  выход клиентов в интернет (MASQUERADE)
#   filter VPN_IN    приём UDP на портах VPN (если INPUT по умолчанию закрыт)
#   filter VPN_FWD   пересылка клиентов + запрет торрентов и порта 25 (жалобы хостинга)
#   mangle VPN_MSS   TCP MSS 1240 под MTU 1280 — иначе на мобильном интернете сайты «висят»
#   mangle VPN_CNT   счётчики пакетов по портам (vpn status): видно, до какого порта доходит трафик

ENV=/etc/vpn/env
[[ -f $ENV ]] || { echo "нет $ENV" >&2; exit 1; }
# shellcheck disable=SC1090
. "$ENV"
# Свои настройки (не перезаписываются): например BLOCK_TORRENTS=0
# shellcheck disable=SC1091
[[ -f /etc/vpn/options ]] && . /etc/vpn/options

TORRENT_SIGS=("BitTorrent protocol" "peer_id=" "info_hash" "/announce" ".torrent" "magnet:?"
              "d1:ad2:id20:" "get_peers" "announce_peer" "find_node")

unhook() {  # table chain from
  while iptables -t "$1" -D "$3" -j "$2" 2>/dev/null; do :; done
  iptables -t "$1" -F "$2" 2>/dev/null
  iptables -t "$1" -X "$2" 2>/dev/null
  return 0
}

down() {
  unhook nat VPN_PRE PREROUTING
  unhook nat VPN_POST POSTROUTING
  unhook filter VPN_IN INPUT
  unhook filter VPN_FWD FORWARD
  unhook mangle VPN_MSS FORWARD
  unhook mangle VPN_CNT PREROUTING
}

up() {
  down
  set -e
  ALL_PORTS="$PORT $BACKUP_PORTS"

  iptables -t mangle -N VPN_CNT
  for p in $ALL_PORTS; do iptables -t mangle -A VPN_CNT -p udp --dport "$p" -j RETURN; done
  iptables -t mangle -I PREROUTING 1 -j VPN_CNT

  iptables -t nat -N VPN_PRE
  for p in $BACKUP_PORTS; do iptables -t nat -A VPN_PRE -p udp --dport "$p" -j REDIRECT --to-ports "$PORT"; done
  iptables -t nat -I PREROUTING 1 -j VPN_PRE

  iptables -t nat -N VPN_POST
  iptables -t nat -A VPN_POST -s "$SUBNET" -o "$WAN" -j MASQUERADE
  iptables -t nat -A POSTROUTING -j VPN_POST

  iptables -N VPN_IN
  iptables -A VPN_IN -p udp --dport "$PORT" -j ACCEPT  # резервные после REDIRECT приходят уже на $PORT
  iptables -I INPUT 1 -j VPN_IN

  iptables -N VPN_FWD
  iptables -A VPN_FWD -i "$IFACE" -p tcp --dport 25 -j REJECT --reject-with tcp-reset
  if [[ "${BLOCK_TORRENTS:-1}" == 1 ]]; then
    for sig in "${TORRENT_SIGS[@]}"; do
      iptables -A VPN_FWD -i "$IFACE" -m string --algo bm --string "$sig" -j DROP
    done
  fi
  iptables -A VPN_FWD -i "$IFACE" -j ACCEPT
  iptables -A VPN_FWD -o "$IFACE" -j ACCEPT
  iptables -I FORWARD 1 -j VPN_FWD

  # MTU 1280 минус 40 байт заголовков IPv4 и TCP.
  local mss=$(( ${MTU:-1280} - 40 ))
  iptables -t mangle -N VPN_MSS
  for dir in -i -o; do
    iptables -t mangle -A VPN_MSS "$dir" "$IFACE" -p tcp --tcp-flags SYN,RST SYN \
      -m tcpmss --mss "$((mss + 1)):65535" -j TCPMSS --set-mss "$mss"
  done
  iptables -t mangle -I FORWARD 1 -j VPN_MSS
}

check() {
  iptables -t nat -C PREROUTING -j VPN_PRE 2>/dev/null &&
  iptables -t nat -C POSTROUTING -j VPN_POST 2>/dev/null &&
  iptables -C INPUT -j VPN_IN 2>/dev/null &&
  iptables -C FORWARD -j VPN_FWD 2>/dev/null &&
  iptables -t mangle -C FORWARD -j VPN_MSS 2>/dev/null &&
  iptables -t mangle -C PREROUTING -j VPN_CNT 2>/dev/null &&
  iptables -t nat -C VPN_POST -s "$SUBNET" -o "$WAN" -j MASQUERADE 2>/dev/null
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  check) check ;;
  *) echo "Использование: $0 up|down|check" >&2; exit 1 ;;
esac
