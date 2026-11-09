#!/usr/bin/env python3
"""vpn-bot — личный Telegram-бот для VPN: конфиги, QR-коды, устройства, статус, смена портов.

Отвечает только владельцам из ADMIN_IDS (/etc/vpn/bot.env), остальных молча игнорирует.
Без внешних библиотек: только стандартный Python. Все действия — через команду vpn.

  vpn-bot                 запустить бота (так его запускает systemd: vpn-bot.service)
  vpn-bot notify ТЕКСТ    написать владельцам (так пишет сторож)
  vpn-bot send-all ТЕКСТ  написать и прислать конфиги всех устройств (после смены портов/IP)
"""
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ENV_FILE = "/etc/vpn/bot.env"
CLIENTS = "/etc/vpn/clients"
OUT = "/root/vpn-configs"
VPN = "/usr/local/bin/vpn"
NAME_RE = re.compile(r"[A-Za-z0-9_]{1,12}")
VARIANT_TITLES = {1: "основной", 2: "резерв: другой порт", 3: "резерв: маскировка под DNS"}

MENU = [["📱 Устройства", "➕ Добавить"], ["📊 Статус", "🔄 Сменить порты"]]
HELP = (
    "Это твой VPN 🛡\n\n"
    "📱 <b>Устройства</b> — конфиги, QR-коды, режим, удаление\n"
    "➕ <b>Добавить</b> — новое устройство\n"
    "📊 <b>Статус</b> — всё ли работает на сервере\n"
    "🔄 <b>Сменить порты</b> — если оператор начал резать VPN; новые конфиги придут сюда же\n\n"
    "На каждое устройство 3 конфига: <b>-1</b> основной, <b>-2</b> и <b>-3</b> резервные "
    "(другие порты и маскировка). Импортируй все три, включай один. Не работает -1 — включи -2 или -3.\n\n"
    "Файл .conf: нажми на него → «Поделиться» → AmneziaWG.\n"
    "QR-код удобен, когда конфиг ставишь на другое устройство: открой бота на ПК и сканируй телефоном."
)


def load_env():
    env = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env[k] = v
    except FileNotFoundError:
        sys.exit(f"нет {ENV_FILE} — настройте бота: bash bot.sh ТОКЕН ВАШ_ID")
    return env


ENV = load_env()
TOKEN = ENV.get("BOT_TOKEN", "")
ADMINS = {int(x) for x in re.split(r"[,\s]+", ENV.get("ADMIN_IDS", "")) if x.isdigit()}
API = ENV.get("API_URL", "https://api.telegram.org") + f"/bot{TOKEN}/"


# ---------- Telegram API ----------

def api(method, http_timeout=30, **params):
    data = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(API + method, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as r:
            return json.load(r).get("result")
    except urllib.error.HTTPError as e:
        print(f"{method}: HTTP {e.code} {e.read()[:300]!r}", file=sys.stderr)
    except Exception as e:  # сеть, таймаут
        print(f"{method}: {e}", file=sys.stderr)
    return None


def upload(method, chat_id, field, path, caption=None, filename=None):
    """sendDocument / sendPhoto: файл multipart-запросом."""
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        content = f.read()
    parts = [(b"chat_id", str(chat_id).encode())]
    if caption:
        parts += [(b"caption", caption.encode()), (b"parse_mode", b"HTML")]
    body = b""
    for k, v in parts:
        body += b"--" + boundary.encode() + b"\r\nContent-Disposition: form-data; name=\"" + k + b"\"\r\n\r\n" + v + b"\r\n"
    fn = (filename or os.path.basename(path)).encode()
    body += (b"--" + boundary.encode() + b"\r\nContent-Disposition: form-data; name=\"" + field.encode()
             + b"\"; filename=\"" + fn + b"\"\r\nContent-Type: application/octet-stream\r\n\r\n" + content + b"\r\n")
    body += b"--" + boundary.encode() + b"--\r\n"
    req = urllib.request.Request(API + method, data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r).get("result")
    except Exception as e:
        print(f"{method}: {e}", file=sys.stderr)


def send(chat_id, text, kb=None, inline=None):
    markup = None
    if inline is not None:
        markup = {"inline_keyboard": inline}
    elif kb:
        markup = {"keyboard": [[{"text": t} for t in row] for row in kb], "resize_keyboard": True, "is_persistent": True}
    return api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
               disable_web_page_preview=True, reply_markup=markup)


def btn(text, data):
    return {"text": text, "callback_data": data}


def pre(text):
    return f"<pre>{html.escape(text.strip())}</pre>"


# ---------- VPN ----------

def vpn(*args):
    """Запуск команды vpn. VPN_NO_PUSH — чтобы vpn сам не дёргал бота (бот пришлёт конфиги сам)."""
    r = subprocess.run([VPN, *args], capture_output=True, text=True, timeout=300,
                       env={**os.environ, "VPN_NO_PUSH": "1"})
    out = (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()
    return r.returncode == 0, out


def devices():
    out = {}
    try:
        for fn in sorted(os.listdir(CLIENTS)):
            if fn.endswith(".json"):
                with open(f"{CLIENTS}/{fn}") as f:
                    c = json.load(f)
                out[c["name"]] = c
    except FileNotFoundError:
        pass
    return out


def mode_name(m):
    return "🔀 обход (только заблокированное)" if m == "split" else "🌍 весь трафик"


def send_configs(chat_id, name, qr=False):
    d = devices().get(name)
    if not d:
        send(chat_id, f"Устройства «{html.escape(name)}» нет.")
        return
    for v, title in VARIANT_TITLES.items():
        path = f"{OUT}/{name}/{name}-{v}.conf"
        if os.path.exists(path):
            upload("sendDocument", chat_id, "document", path, caption=f"<b>{name}-{v}</b> — {title}")
    if qr:
        send_qr(chat_id, name, 1)


def send_qr(chat_id, name, v):
    path = f"{OUT}/{name}/{name}-{v}.png"
    if not os.path.exists(path):
        send(chat_id, "QR-кода нет (не установлен qrencode?). Пришлю файл.")
        return send_configs(chat_id, name)
    upload("sendDocument", chat_id, "document", path, filename=f"{name}-{v}-qr.png",
           caption=f"QR <b>{name}-{v}</b> ({VARIANT_TITLES[v]}). AmneziaWG → + → Сканировать QR-код.\n"
                   "Отправлен файлом, без сжатия: так он точно сканируется.")


def device_card(chat_id, name):
    d = devices().get(name)
    if not d:
        return send(chat_id, f"Устройства «{html.escape(name)}» нет.")
    ok, lst = vpn("list")
    line = next((l for l in lst.splitlines() if l.startswith(name + " ")), "")
    other = "full" if d["mode"] == "split" else "split"
    send(chat_id,
         f"📱 <b>{name}</b>\nРежим: {mode_name(d['mode'])}\nАдрес: {d['ip']}\n" + (pre(line) if line else ""),
         inline=[[btn("📄 Файлы конфигов", f"files:{name}")],
                 [btn("🔳 QR 1", f"qr:{name}:1"), btn("🔳 QR 2", f"qr:{name}:2"), btn("🔳 QR 3", f"qr:{name}:3")],
                 [btn(f"Режим → {'весь трафик' if other == 'full' else 'обход'}", f"mode:{name}:{other}")],
                 [btn("🗑 Удалить", f"delask:{name}"), btn("⬅️ Назад", "list")]])


def list_devices(chat_id):
    ds = devices()
    if not ds:
        return send(chat_id, "Устройств нет. Нажми ➕ Добавить.")
    ok, out = vpn("list")
    rows = [[btn(f"{'🔀' if d['mode'] == 'split' else '🌍'} {n}", f"dev:{n}")] for n, d in ds.items()]
    send(chat_id, "Устройства:\n" + pre(out) + "\nВыбери устройство:", inline=rows)


# ---------- обработка ----------

STATE = {}  # chat_id -> что ждём от пользователя


def on_message(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if text in ("/start", "/help", "❓ Помощь"):
        STATE.pop(chat_id, None)
        return send(chat_id, HELP, kb=MENU)
    if text == "📱 Устройства" or text == "/list":
        STATE.pop(chat_id, None)
        return list_devices(chat_id)
    if text == "➕ Добавить" or text == "/add":
        STATE[chat_id] = "name"
        return send(chat_id, "Как назвать устройство? Латиница, цифры или _, до 12 символов.\n"
                             "Например: <code>iphone2</code>, <code>mama</code>, <code>laptop</code>")
    if text == "📊 Статус" or text == "/status":
        STATE.pop(chat_id, None)
        ok, out = vpn("status")
        return send(chat_id, pre(out))
    if text == "🔄 Сменить порты" or text == "/rotate":
        STATE.pop(chat_id, None)
        return send(chat_id,
                    "Что сменить? После смены <b>старые конфиги перестанут работать</b> — "
                    "новые для всех устройств я пришлю сюда.\n\n"
                    "• <b>Порты</b> — если оператор режет UDP на текущих портах\n"
                    "• <b>Маскировку</b> — если пакеты доходят (в 📊 Статус счётчики растут), а подключения нет\n"
                    "• <b>Всё</b> — если непонятно",
                    inline=[[btn("Порты", "rot:ports"), btn("Маскировку", "rot:obfs"), btn("Всё", "rot:all")],
                            [btn("Отмена", "cancel")]])
    if STATE.get(chat_id) == "name":
        if not NAME_RE.fullmatch(text):
            return send(chat_id, "Нужно латиницей, цифрами или _, до 12 символов. Попробуй ещё раз:")
        if text in devices():
            return send(chat_id, f"«{text}» уже есть. Другое имя:")
        STATE.pop(chat_id, None)
        return send(chat_id, f"Режим для <b>{text}</b>?",
                    inline=[[btn("🔀 Обход: только заблокированное", f"add:{text}:split")],
                            [btn("🌍 Весь трафик", f"add:{text}:full")],
                            [btn("Отмена", "cancel")]])
    send(chat_id, "Выбери действие в меню 👇", kb=MENU)


def on_callback(cq):
    chat_id = cq["message"]["chat"]["id"]
    data = cq.get("data", "")
    api("answerCallbackQuery", callback_query_id=cq["id"])
    kind, _, rest = data.partition(":")
    if kind == "cancel":
        return send(chat_id, "Отменено.")
    if kind == "list":
        return list_devices(chat_id)
    if kind == "dev":
        return device_card(chat_id, rest)
    if kind == "files":
        return send_configs(chat_id, rest)
    if kind == "qr":
        name, _, v = rest.partition(":")
        return send_qr(chat_id, name, int(v) if v in ("1", "2", "3") else 1)
    if kind == "add":
        name, _, mode = rest.partition(":")
        ok, out = vpn("add", name, *(["--full"] if mode == "full" else []))
        if not ok:
            return send(chat_id, pre(out))
        send(chat_id, f"✅ <b>{name}</b> добавлен ({mode_name(mode)}). Конфиги:")
        return send_configs(chat_id, name)
    if kind == "mode":
        name, _, mode = rest.partition(":")
        ok, out = vpn("mode", name, mode)
        if not ok:
            return send(chat_id, pre(out))
        send(chat_id, f"✅ <b>{name}</b>: {mode_name(mode)}. Удали старые туннели {name}-1/2/3 в приложении "
                      "и импортируй новые:")
        return send_configs(chat_id, name)
    if kind == "delask":
        return send(chat_id, f"Удалить <b>{rest}</b>? Его конфиги перестанут работать.",
                    inline=[[btn("🗑 Да, удалить", f"del:{rest}"), btn("Отмена", "cancel")]])
    if kind == "del":
        ok, out = vpn("del", rest)
        return send(chat_id, ("✅ " if ok else "") + html.escape(out))
    if kind == "rot":
        flag = {"ports": ["--ports"], "obfs": ["--obfs"], "all": []}.get(rest, ["--ports"])
        send(chat_id, "Меняю, это займёт несколько секунд…")
        ok, out = vpn("rotate", *flag)
        send(chat_id, pre(out))
        if ok:
            send_all(chat_id, "Новые конфиги — импортируй их, старые удали:")


def send_all(chat_id, header):
    ds = devices()
    send(chat_id, f"{html.escape(header)}\n\nУстройств: {len(ds)}. На каждое по 3 файла.")
    for name in ds:
        send(chat_id, f"📱 <b>{name}</b> — {mode_name(ds[name]['mode'])}")
        send_configs(chat_id, name)


def handle(upd):
    msg = upd.get("message")
    cq = upd.get("callback_query")
    user = (msg or cq or {}).get("from", {}).get("id")
    if user not in ADMINS:
        return  # чужим не отвечаем вообще
    if msg and msg.get("chat", {}).get("type") != "private":
        return
    try:
        if msg:
            on_message(msg)
        elif cq:
            on_callback(cq)
    except Exception as e:
        chat = (msg or cq.get("message", {})).get("chat", {}).get("id")
        print(f"ошибка: {e!r}", file=sys.stderr)
        if chat:
            send(chat, f"⚠️ Ошибка: {html.escape(repr(e))}")


def run():
    if not TOKEN or not ADMINS:
        sys.exit("в /etc/vpn/bot.env нет BOT_TOKEN или ADMIN_IDS")
    me = api("getMe")
    print(f"бот @{(me or {}).get('username', '?')} запущен, владельцы: {sorted(ADMINS)}", flush=True)
    api("setMyCommands", commands=[
        {"command": "start", "description": "Меню"},
        {"command": "list", "description": "Устройства"},
        {"command": "add", "description": "Добавить устройство"},
        {"command": "status", "description": "Состояние сервера"},
        {"command": "rotate", "description": "Сменить порты/маскировку"},
    ])
    offset = None
    while True:
        updates = api("getUpdates", http_timeout=70, timeout=50, offset=offset,
                      allowed_updates=["message", "callback_query"])
        if updates is None:
            time.sleep(5)  # нет сети / Telegram недоступен — пробуем снова
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            handle(upd)


def main():
    if len(sys.argv) >= 2 and sys.argv[1] in ("notify", "send-all"):
        text = " ".join(sys.argv[2:]) or "VPN"
        for chat_id in ADMINS:
            if sys.argv[1] == "notify":
                send(chat_id, html.escape(text))
            else:
                send_all(chat_id, f"🔄 {text}. Старые конфиги больше не работают.")
        return
    run()


if __name__ == "__main__":
    main()
