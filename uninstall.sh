#!/usr/bin/env bash
# Удаление VPN. Перед удалением делает резервную копию ключей и конфигов в /root/vpn-backup-ДАТА.tar.gz
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "запустите от root" >&2; exit 1; }
B="/root/vpn-backup-$(date +%F-%H%M).tar.gz"
tar czf "$B" /etc/vpn /etc/amnezia/amneziawg /root/vpn-configs 2>/dev/null || true
systemctl disable --now vpn-bot vpn-tiktok.timer vpn-watchdog.timer awg-quick@awg0 2>/dev/null || true
/usr/local/sbin/vpn-rules down 2>/dev/null || true
rm -f /etc/systemd/system/vpn-watchdog.{service,timer} /etc/systemd/system/vpn-bot.service /etc/systemd/system/vpn-tiktok.{service,timer} /usr/local/bin/vpn /usr/local/bin/vpn-bot /usr/local/sbin/vpn-{rules,watchdog} \
      /etc/sysctl.d/99-vpn.conf /etc/fail2ban/jail.d/sshd-vpn.local
rm -rf /etc/vpn /root/vpn-configs /etc/amnezia/amneziawg/awg0.conf
systemctl daemon-reload
echo "VPN удалён. Резервная копия: $B"
echo "Пакеты AmneziaWG остались: apt remove amneziawg amneziawg-tools — если нужно убрать и их."
