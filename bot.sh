#!/usr/bin/env bash
# Личный Telegram-бот для VPN (только для вас: остальным он не отвечает).
#
#   sudo bash bot.sh 'ТОКЕН_ОТ_BOTFATHER' ВАШ_TELEGRAM_ID   — поставить / обновить
#   sudo bash bot.sh off                                    — выключить и удалить
#
# Токен: @BotFather -> /newbot. Ваш ID: @userinfobot -> /start. Несколько ID — через запятую.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE=/etc/vpn/bot.env
UNIT=/etc/systemd/system/vpn-bot.service

die() { echo "Ошибка: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "запустите от root"
[[ -f /etc/vpn/server.json ]] || die "сначала установите VPN: bash install.sh"

if [[ "${1:-}" == "off" ]]; then
  systemctl disable --now vpn-bot 2>/dev/null || true
  rm -f "$UNIT" "$ENV_FILE" /usr/local/bin/vpn-bot
  systemctl daemon-reload
  echo "Бот выключен и удалён. VPN работает как раньше."
  exit 0
fi

TOKEN="${1:-}"
ADMIN="${2:-}"
# Повторный запуск без параметров — обновить код, настройки оставить.
if [[ -z "$TOKEN" && -f "$ENV_FILE" ]]; then
  TOKEN="$(sed -n 's/^BOT_TOKEN=//p' "$ENV_FILE")"
  ADMIN="$(sed -n 's/^ADMIN_IDS=//p' "$ENV_FILE")"
fi
[[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || die "использование: bash bot.sh 'ТОКЕН' ВАШ_ID (токен вида 123456:ABC...)"
[[ "$ADMIN" =~ ^[0-9]+(,[0-9]+)*$ ]] || die "Telegram ID — только цифры (узнать: @userinfobot)"

echo "==> Проверяю токен"
NAME="$(curl -fsS --max-time 15 "https://api.telegram.org/bot$TOKEN/getMe" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["username"])')" \
  || die "Telegram не принял токен (или нет связи с api.telegram.org)"

install -m 755 "$SCRIPT_DIR/lib/vpn-bot.py" /usr/local/bin/vpn-bot
umask 077
printf 'BOT_TOKEN=%s\nADMIN_IDS=%s\n' "$TOKEN" "$ADMIN" >"$ENV_FILE"
umask 022

cat >"$UNIT" <<'EOF'
[Unit]
Description=VPN: личный Telegram-бот
After=network-online.target awg-quick@awg0.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/vpn-bot
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable vpn-bot >/dev/null 2>&1
systemctl restart vpn-bot
sleep 3
systemctl is-active --quiet vpn-bot || { journalctl -u vpn-bot -n 20 --no-pager; die "бот не запустился"; }

# Если вы ещё не нажали /start в боте, Telegram не даст ему написать первым — это не ошибка.
/usr/local/bin/vpn-bot notify "✅ Бот подключён к VPN-серверу. Нажми /start" || true

cat <<EOF

Готово: бот @$NAME работает и отвечает только ID $ADMIN.
Откройте его в Telegram и нажмите /start.
Сторож теперь пишет в бота, а после смены портов/IP бот сам присылает новые конфиги.
Логи: journalctl -u vpn-bot -f
EOF
