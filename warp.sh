#!/usr/bin/env bash
# TikTok через Cloudflare WARP. Если TikTok не пускает IP сервера (хостинг помечен как VPN),
# трафик TikTok из VPN уходит в интернет через бесплатный WARP — TikTok видит IP Cloudflare.
# Всё остальное идёт как раньше. Конфиги на устройствах менять не нужно.
#
#   sudo bash warp.sh          — включить
#   sudo bash warp.sh off      — выключить
#   sudo bash warp.sh status   — работает ли и через какой IP
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DIR=/etc/vpn/warp
CONF=/etc/wireguard/warp.conf
FLAG=/etc/vpn/warp-on
DOMAINS="tiktok.com tiktokv.com tiktokcdn.com tiktokcdn-us.com tiktokv.us tiktokv.eu tiktokw.us ttwstatic.com
ibytedtos.com ibyteimg.com byteimg.com byteoversea.com bytedance.com byteeffecttos-g.com musical.ly ttlivecdn.com"

die() { echo "Ошибка: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "запустите от root"
[[ -f /etc/vpn/server.json ]] || die "сначала установите VPN: bash install.sh"

if [[ "${1:-}" == "status" ]]; then
  if [[ ! -f $FLAG ]]; then echo "WARP для TikTok выключен (включить: bash warp.sh)"; exit 0; fi
  ip link show warp >/dev/null 2>&1 && echo "✅ интерфейс warp поднят" || echo "❌ интерфейс warp не поднят"
  /usr/local/sbin/vpn-warp check && echo "✅ маршрут TikTok -> WARP на месте" || echo "❌ маршрута TikTok -> WARP нет (TikTok идёт с IP сервера)"
  echo "адресов TikTok в списке: $(ipset list vpn_warp 2>/dev/null | grep -cE '^[0-9]')"
  echo "выход через WARP:"; /usr/local/sbin/vpn-warp test | sed 's/^/  /'
  exit 0
fi

if [[ "${1:-}" == "off" ]]; then
  rm -f "$FLAG"
  systemctl disable --now wg-quick@warp 2>/dev/null || true
  /usr/local/sbin/vpn-warp rules-down 2>/dev/null || true
  ipset destroy vpn_warp 2>/dev/null || true
  rm -f /etc/dnsmasq.d/vpn-warp.conf
  systemctl is-active --quiet dnsmasq && systemctl restart dnsmasq
  echo "WARP для TikTok выключен: TikTok снова идёт с IP сервера."
  exit 0
fi

echo "==> Ставлю WireGuard и ipset"
apt-get -o DPkg::Lock::Timeout=600 install -y -qq wireguard-tools ipset dnsutils curl >/dev/null \
  || die "не удалось поставить пакеты"
modprobe wireguard || die "в ядре нет модуля wireguard"
install -m 755 "$SCRIPT_DIR/lib/vpn-warp.sh" /usr/local/sbin/vpn-warp
install -m 755 "$SCRIPT_DIR/lib/vpn.py" /usr/local/bin/vpn
install -m 755 "$SCRIPT_DIR/lib/vpn-watchdog.sh" /usr/local/sbin/vpn-watchdog

mkdir -p "$DIR" /etc/wireguard
chmod 700 "$DIR"
cd "$DIR"
if [[ ! -f wgcf-profile.conf ]]; then
  echo "==> Регистрирую бесплатный аккаунт Cloudflare WARP (wgcf)"
  case "$(uname -m)" in
    x86_64) ARCH=amd64 ;; aarch64) ARCH=arm64 ;; *) die "неизвестная архитектура $(uname -m)" ;;
  esac
  URL="$(curl -fsS --max-time 20 https://api.github.com/repos/ViRb3/wgcf/releases/latest \
    | grep -oE "https://[^\"]+wgcf_[0-9.]+_linux_${ARCH}\"" | tr -d '"' | head -1 || true)"
  URL="${URL:-https://github.com/ViRb3/wgcf/releases/download/v2.2.22/wgcf_2.2.22_linux_${ARCH}}"
  curl -fsSL --max-time 60 "$URL" -o wgcf || die "не скачался wgcf ($URL)"
  chmod 755 wgcf
  [[ -f wgcf-account.toml ]] || ./wgcf register --accept-tos >/dev/null || die "Cloudflare не зарегистрировал аккаунт WARP"
  ./wgcf generate >/dev/null || die "не получился профиль WARP"
fi

echo "==> Настраиваю туннель warp"
PRIV="$(awk -F' *= *' '$1=="PrivateKey"{print $2; exit}' wgcf-profile.conf)"
PUB="$(awk -F' *= *' '$1=="PublicKey"{print $2; exit}' wgcf-profile.conf)"
ADDR="$(awk -F' *= *' '$1=="Address"{print $2}' wgcf-profile.conf | tr ',' '\n' | grep -m1 -E '^[0-9.]+/' | xargs)"
EP="$(awk -F' *= *' '$1=="Endpoint"{print $2; exit}' wgcf-profile.conf)"
[[ -n "$PRIV" && -n "$PUB" && -n "$ADDR" && -n "$EP" ]] || die "в профиле WARP не хватает данных: $DIR/wgcf-profile.conf"
umask 077
cat >"$CONF" <<EOF
# Cloudflare WARP только для TikTok (warp.sh). Table = off: обычный трафик сервера сюда не идёт.
[Interface]
PrivateKey = $PRIV
Address = $ADDR
MTU = 1280
Table = off
PostUp = /usr/local/sbin/vpn-warp rules-up
PostDown = /usr/local/sbin/vpn-warp rules-down

[Peer]
PublicKey = $PUB
AllowedIPs = 0.0.0.0/0
Endpoint = $EP
PersistentKeepalive = 25
EOF
umask 022

# DNS с блокировкой рекламы сам добавляет адреса доменов TikTok в список в момент запроса.
if systemctl is-active --quiet dnsmasq; then
  printf 'ipset=/%s/vpn_warp\n' "$(echo $DOMAINS | tr ' ' '/')" >/etc/dnsmasq.d/vpn-warp.conf
fi

touch "$FLAG"
ipset create vpn_warp hash:net timeout 172800 -exist
systemctl enable wg-quick@warp >/dev/null 2>&1
systemctl restart wg-quick@warp || { journalctl -u wg-quick@warp -n 20 --no-pager; die "туннель warp не поднялся"; }
[[ -f /etc/dnsmasq.d/vpn-warp.conf ]] && systemctl restart dnsmasq

sleep 3
echo "==> Проверяю"
T="$(/usr/local/sbin/vpn-warp test || true)"
echo "$T" | sed 's/^/    /'
if ! grep -q 'warp=on' <<<"$T"; then
  echo "ВНИМАНИЕ: WARP не отвечает. TikTok пока идёт как раньше, с IP сервера. Пришлите вывод: bash warp.sh status"
  exit 1
fi
cat <<EOF

Готово: трафик TikTok из VPN теперь идёт через Cloudflare WARP (IP выше, warp=on).
Конфиги на телефоне менять НЕ нужно. Выключи и включи VPN, закрой TikTok полностью и открой снова.
Проверить: bash warp.sh status   ·   Выключить: bash warp.sh off
EOF
