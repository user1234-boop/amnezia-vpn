#!/usr/bin/env bash
# Telegram-бот для VPN. Админ (вы) управляет сервером и одобряет заявки; остальные могут только
# попросить VPN и получить свои конфиги после вашего одобрения.
#
#   sudo bash bot.sh 'ТОКЕН_ОТ_BOTFATHER' @ваш_ник   — поставить / обновить (или числовой ID вместо ника)
#   sudo bash bot.sh                                 — обновить код, настройки оставить
#   sudo bash bot.sh off                             — выключить и удалить
#
# Токен: @BotFather -> /newbot. С ником: при первом вашем /start бот закрепит ваш числовой ID,
# и дальше права админа привязаны к нему (смена ника их не передаст).
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
old() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | head -n 1; }
OLD_IDS="$(old ADMIN_IDS)"
OLD_USER="$(old ADMIN_USERNAME)"
# Повторный запуск без параметров — обновить код, настройки оставить.
if [[ -z "$TOKEN" && -f "$ENV_FILE" ]]; then
  TOKEN="$(old BOT_TOKEN)"
  ADMIN="${OLD_IDS:-@$OLD_USER}"
fi
[[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || die "использование: bash bot.sh 'ТОКЕН' @ваш_ник (токен вида 123456:ABC...)"
IDS=""; USERNAME=""
if [[ "$ADMIN" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
  IDS="$ADMIN"
elif [[ "$ADMIN" =~ ^@?[A-Za-z0-9_]{4,32}$ ]]; then
  USERNAME="${ADMIN#@}"
  # Тот же ник, а ID уже закреплён прошлым /start — оставляем закреплённый ID.
  [[ "${USERNAME,,}" == "${OLD_USER,,}" ]] && IDS="$OLD_IDS"
else
  die "второй параметр — ваш ник (@dyssanu) или числовой Telegram ID"
fi

echo "==> Проверяю токен"
NAME="$(curl -fsS --max-time 15 "https://api.telegram.org/bot$TOKEN/getMe" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["username"])')" \
  || die "Telegram не принял токен (или нет связи с api.telegram.org)"

install -m 755 "$SCRIPT_DIR/lib/vpn-bot.py" /usr/local/bin/vpn-bot
umask 077
printf 'BOT_TOKEN=%s\nADMIN_USERNAME=%s\nADMIN_IDS=%s\n' "$TOKEN" "$USERNAME" "$IDS" >"$ENV_FILE"
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
[[ -n "$IDS" ]] && { /usr/local/bin/vpn-bot notify "✅ Бот обновлён и работает" || true; }

cat <<EOF

Готово: бот @$NAME работает.
Админ: ${IDS:+ID $IDS}${USERNAME:+${IDS:+, }@$USERNAME}$([[ -z "$IDS" ]] && echo " — откройте бота и нажмите /start ПРЯМО СЕЙЧАС: первый /start от этого ника закрепит права админа")
Остальные могут нажать /start и попросить VPN — заявка придёт вам с кнопками «Выдать» / «Отказать».
Сторож пишет вам в бота, а после смены портов/IP бот сам рассылает новые конфиги всем.
Логи: journalctl -u vpn-bot -f
EOF
