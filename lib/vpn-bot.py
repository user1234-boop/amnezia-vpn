#!/usr/bin/env python3
"""vpn-bot — Telegram-бот для VPN: админ управляет сервером и выдаёт доступ, остальные просят VPN.

Админ (ADMIN_IDS в /etc/vpn/bot.env; при первом /start от ADMIN_USERNAME его ID закрепляется навсегда):
  устройства, конфиги, QR, статус, смена портов, заявки на доступ.
Остальные: /start -> «Попросить VPN» -> админ одобряет -> им приходят свои конфиги.
Без внешних библиотек: только стандартный Python. Все действия с VPN — через команду vpn.

  vpn-bot                 запустить бота (так его запускает systemd: vpn-bot.service)
  vpn-bot notify ТЕКСТ    написать админам (так пишет сторож)
  vpn-bot send-all ТЕКСТ  прислать новые конфиги: админу — все, каждому человеку — его (после смены портов/IP)
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
USERS_FILE = "/etc/vpn/bot-users.json"
CLIENTS = "/etc/vpn/clients"
OUT = "/root/vpn-configs"
VPN = "/usr/local/bin/vpn"
ADBLOCK_FLAG = "/etc/vpn/adblock-on"
NAME_RE = re.compile(r"[A-Za-z0-9_]{1,12}")
VARIANT_TITLES = {1: "основной", 2: "резерв: другой порт", 3: "резерв: маскировка под DNS"}
REQUEST_COOLDOWN = 3600  # после отказа повторная заявка не раньше чем через час

ADMIN_MENU = [["📱 Устройства", "🙋 Заявки"], ["➕ Добавить", "👥 Люди"], ["📊 Статус", "🔄 Сменить порты"]]
GUEST_MENU = [["🙏 Попросить VPN"], ["❓ Как подключить"]]
USER_MENU = [["📄 Мои конфиги", "❓ Как подключить"], ["🙏 Попросить ещё конфиг"]]

ADMIN_HELP = (
    "Ты админ этого VPN 🛡\n\n"
    "📱 <b>Устройства</b> — конфиги, QR-коды, режим, удаление (видно, чьё устройство)\n"
    "🙋 <b>Заявки</b> — кто просит VPN; новые заявки приходят сразу\n"
    "➕ <b>Добавить</b> — устройство для себя\n"
    "👥 <b>Люди</b> — кому выдан доступ, заблокированные\n"
    "📊 <b>Статус</b> — всё ли работает на сервере\n"
    "🔄 <b>Сменить порты</b> — если оператор начал резать VPN; новые конфиги разошлются всем сами\n\n"
    "На каждое устройство 3 конфига: <b>-1</b> основной, <b>-2</b> и <b>-3</b> резервные. "
    "Импортируй все три, включай один."
)
GUEST_HELP = (
    "Привет! Это частный VPN 🛡\n\n"
    "Нажми <b>🙏 Попросить VPN</b> — владелец получит заявку. Когда он её одобрит, "
    "сюда придут файлы для подключения и инструкция."
)
HOWTO = (
    "<b>Как подключить</b>\n\n"
    "1. Поставь приложение <b>AmneziaWG</b> (App Store / Google Play; на компьютер — AmneziaWG для Windows/Mac).\n"
    "2. Нажми <b>📄 Мои конфиги</b> — придут 3 файла.\n"
    "3. Телефон: нажми на файл → «Поделиться» → <b>AmneziaWG</b>. Компьютер: скачай файл, "
    "в AmneziaWG — «Импорт из файла». Добавь все три.\n"
    "4. Включи туннель <b>-1</b>. Если не подключается — выключи его и включи <b>-2</b>, потом <b>-3</b>. "
    "Включай только один за раз.\n\n"
    "QR-код (если под конфигами есть кнопки QR) удобен, когда бот открыт на компьютере, а сканируешь телефоном: "
    "AmneziaWG → + → Сканировать QR-код.\n\n"
    "Торренты через этот VPN не работают."
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
        sys.exit(f"нет {ENV_FILE} — настройте бота: bash bot.sh ТОКЕН @ваш_ник")
    return env


ENV = load_env()
TOKEN = ENV.get("BOT_TOKEN", "")
ADMINS = {int(x) for x in re.split(r"[,\s]+", ENV.get("ADMIN_IDS", "")) if x.isdigit()}
ADMIN_USERNAME = ENV.get("ADMIN_USERNAME", "").lstrip("@").lower()
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


def upload(method, chat_id, field, path, caption=None, filename=None, inline=None):
    """sendDocument / sendPhoto: файл multipart-запросом."""
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        content = f.read()
    parts = [(b"chat_id", str(chat_id).encode())]
    if caption:
        parts += [(b"caption", caption.encode()), (b"parse_mode", b"HTML")]
    if inline:
        parts.append((b"reply_markup", json.dumps({"inline_keyboard": inline}).encode()))
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


# ---------- данные ----------

def load_users():
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_users(users):
    tmp = USERS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)
    os.replace(tmp, USERS_FILE)


def who(u):
    """Как показывать человека: Имя (@ник, id)."""
    name = html.escape(u.get("name") or "без имени")
    nick = f"@{html.escape(u['username'])}, " if u.get("username") else ""
    return f"{name} ({nick}<code>{u['id']}</code>)"


def remember(users, frm):
    """Обновить имя/ник человека в базе (их можно менять в Telegram)."""
    uid = str(frm["id"])
    u = users.setdefault(uid, {"id": frm["id"], "status": "new", "devices": []})
    u["name"] = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
    u["username"] = frm.get("username") or ""
    return u


def owner_of(device, users=None):
    for u in (users or load_users()).values():
        if device in u.get("devices", []):
            return u
    return None


def bind_admin(frm):
    """Первый /start от ADMIN_USERNAME закрепляет его числовой ID как админа (ник потом не важен)."""
    if ADMINS or not ADMIN_USERNAME or (frm.get("username") or "").lower() != ADMIN_USERNAME:
        return False
    ADMINS.add(frm["id"])
    with open(ENV_FILE) as f:
        lines = [l for l in f if not l.startswith("ADMIN_IDS=")]
    lines.append(f"ADMIN_IDS={frm['id']}\n")
    fd = os.open(ENV_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.writelines(lines)
    os.replace(ENV_FILE + ".tmp", ENV_FILE)
    return True


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
    return "🔀 обход (всё, кроме российских сайтов)" if m == "split" else "🌍 весь трафик"


def new_device_name(u):
    """Имя устройства для человека: из ника (латиница), иначе u + конец ID; с цифрой, если занято."""
    base = re.sub(r"[^A-Za-z0-9_]", "", u.get("username") or "")[:10] or f"u{str(u['id'])[-6:]}"
    taken = set(devices())
    if base not in taken:
        return base
    for i in range(2, 100):
        name = f"{base[:10]}{i}"
        if name not in taken:
            return name
    return f"u{uuid.uuid4().hex[:8]}"


def send_configs(chat_id, name):
    if name not in devices():
        send(chat_id, f"Устройства «{html.escape(name)}» нет.")
        return
    for v, title in VARIANT_TITLES.items():
        path = f"{OUT}/{name}/{name}-{v}.conf"
        if os.path.exists(path):
            upload("sendDocument", chat_id, "document", path, caption=f"<b>{name}-{v}</b> — {title}")
    qrs = qr_buttons(name)
    if qrs:
        send(chat_id, f"QR-коды <b>{name}</b> (если ставишь на другое устройство):", inline=[qrs])


def qr_buttons(name):
    """Кнопки QR только для вариантов, где QR есть: конфиг «обхода» (сотни сетей) в QR не влезает."""
    return [btn(f"🔳 QR {v}", f"qr:{name}:{v}") for v in VARIANT_TITLES
            if os.path.exists(f"{OUT}/{name}/{name}-{v}.png")]


def ads_enabled():
    return os.path.exists(ADBLOCK_FLAG)


def send_qr(chat_id, name, v):
    path = f"{OUT}/{name}/{name}-{v}.png"
    if not os.path.exists(path):
        send(chat_id, "Для этого конфига QR-кода нет (он слишком длинный). Пришлю файлы — "
                      "на телефоне нажми на файл → «Поделиться» → AmneziaWG.")
        return send_configs(chat_id, name)
    upload("sendDocument", chat_id, "document", path, filename=f"{name}-{v}-qr.png",
           caption=f"QR <b>{name}-{v}</b> ({VARIANT_TITLES[v]}). AmneziaWG → + → Сканировать QR-код.")


# ---------- админ ----------

def device_card(chat_id, name):
    d = devices().get(name)
    if not d:
        return send(chat_id, f"Устройства «{html.escape(name)}» нет.")
    ok, lst = vpn("list")
    line = next((l for l in lst.splitlines() if l.startswith(name + " ")), "")
    other = "full" if d["mode"] == "split" else "split"
    owner = owner_of(name)
    send(chat_id,
         f"📱 <b>{name}</b>\nЧьё: {who(owner) if owner else 'твоё'}\nРежим: {mode_name(d['mode'])}\n"
         f"Адрес: {d['ip']}\n" + (pre(line) if line else ""),
         inline=[[btn("📄 Файлы конфигов", f"files:{name}")],
                 *([qr_buttons(name)] if qr_buttons(name) else []),
                 [btn(f"Режим → {'весь трафик' if other == 'full' else 'обход'}", f"mode:{name}:{other}")],
                 *([[btn("🛡 Реклама: блокируется → выключить" if d.get("ads", True) else
                         "🛡 Реклама: не блокируется → включить",
                         f"ads:{name}:{'off' if d.get('ads', True) else 'on'}")]] if ads_enabled() else []),
                 [btn("🗑 Удалить", f"delask:{name}"), btn("⬅️ Назад", "list")]])


def list_devices(chat_id):
    ds = devices()
    if not ds:
        return send(chat_id, "Устройств нет. Нажми ➕ Добавить.")
    ok, out = vpn("list")
    users = load_users()
    rows = []
    for n, d in ds.items():
        o = owner_of(n, users)
        tag = f" — {o.get('name') or o['id']}" if o else ""
        rows.append([btn(f"{'🔀' if d['mode'] == 'split' else '🌍'} {n}{tag}", f"dev:{n}")])
    send(chat_id, "Устройства:\n" + pre(out) + "\nВыбери устройство:", inline=rows)


def request_card(chat_id, u):
    n = len(u.get("devices", []))
    send(chat_id,
         f"🙋 <b>Заявка на VPN</b>\n{who(u)}\n" + (f"Уже есть конфигов: {n}\n" if n else "") +
         "\nКакой режим выдать?\n🔀 <b>Обход</b> — через VPN всё, кроме российских сайтов (для телефона, AmneziaWG)\n"
         "🌍 <b>Весь трафик</b> — для компьютера",
         inline=[[btn("✅ Обход", f"ok:{u['id']}:split"), btn("✅ Весь трафик", f"ok:{u['id']}:full")],
                 [btn("❌ Отказать", f"no:{u['id']}"), btn("🚫 Заблокировать", f"ban:{u['id']}")]])


def list_requests(chat_id):
    pending = [u for u in load_users().values() if u.get("status") == "pending"]
    if not pending:
        return send(chat_id, "Новых заявок нет.")
    for u in pending:
        request_card(chat_id, u)


def list_people(chat_id):
    users = load_users()
    approved = [u for u in users.values() if u.get("devices")]
    banned = [u for u in users.values() if u.get("status") == "banned"]
    if not approved and not banned:
        return send(chat_id, "Пока никому не выдан доступ.")
    lines = ["👥 <b>С доступом</b>:"] + [f"• {who(u)}: {', '.join(u['devices'])}" for u in approved]
    if banned:
        lines += ["", "🚫 <b>Заблокированы</b>:"] + [f"• {who(u)}" for u in banned]
    inline = [[btn(f"🔓 Разблокировать {u.get('name') or u['id']}", f"unban:{u['id']}")] for u in banned]
    send(chat_id, "\n".join(lines) + "\n\nОтключить человека: 📱 Устройства → его устройство → 🗑 Удалить.",
         inline=inline or None)


def admin_message(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if text in ("/start", "/help"):
        STATE.pop(chat_id, None)
        return send(chat_id, ADMIN_HELP, kb=ADMIN_MENU)
    if text in ("📱 Устройства", "/list"):
        STATE.pop(chat_id, None)
        return list_devices(chat_id)
    if text in ("🙋 Заявки", "/requests"):
        STATE.pop(chat_id, None)
        return list_requests(chat_id)
    if text in ("👥 Люди", "/people"):
        STATE.pop(chat_id, None)
        return list_people(chat_id)
    if text in ("➕ Добавить", "/add"):
        STATE[chat_id] = "name"
        return send(chat_id, "Как назвать устройство? Латиница, цифры или _, до 12 символов.\n"
                             "Например: <code>iphone2</code>, <code>laptop</code>")
    if text in ("📊 Статус", "/status"):
        STATE.pop(chat_id, None)
        ok, out = vpn("status")
        return send(chat_id, pre(out))
    if text in ("🔄 Сменить порты", "/rotate"):
        STATE.pop(chat_id, None)
        return send(chat_id,
                    "Что сменить? После смены <b>старые конфиги у всех перестанут работать</b> — "
                    "новые я разошлю сам: тебе все, каждому человеку его.\n\n"
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
                    inline=[[btn("🔀 Обход: всё, кроме российских сайтов", f"add:{text}:split")],
                            [btn("🌍 Весь трафик", f"add:{text}:full")],
                            [btn("Отмена", "cancel")]])
    send(chat_id, "Выбери действие в меню 👇", kb=ADMIN_MENU)


def admin_callback(chat_id, kind, rest):
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
    if kind == "ads":
        name, _, state = rest.partition(":")
        ok, out = vpn("ads", name, "on" if state == "on" else "off")
        if not ok:
            return send(chat_id, pre(out))
        what = "блокируется" if state == "on" else "больше не блокируется"
        send(chat_id, f"✅ <b>{name}</b>: реклама {what}. Старые туннели {name}-1/2/3 удали, импортируй новые:")
        send_configs(chat_id, name)
        owner = owner_of(name)
        if owner:
            send(owner["id"], f"🔄 Твой VPN <b>{name}</b>: реклама {what}.\n"
                              "Удали старые туннели в AmneziaWG и добавь новые файлы:")
            send_configs(owner["id"], name)
        return
    if kind == "mode":
        name, _, mode = rest.partition(":")
        ok, out = vpn("mode", name, mode)
        if not ok:
            return send(chat_id, pre(out))
        send(chat_id, f"✅ <b>{name}</b>: {mode_name(mode)}. Старые туннели {name}-1/2/3 удали, импортируй новые:")
        send_configs(chat_id, name)
        owner = owner_of(name)
        if owner:
            send(owner["id"], f"🔄 Твой VPN <b>{name}</b> переключён: {mode_name(mode)}.\n"
                              "Удали старые туннели в AmneziaWG и добавь новые файлы:")
            send_configs(owner["id"], name)
        return
    if kind == "delask":
        owner = owner_of(rest)
        return send(chat_id, f"Удалить <b>{rest}</b>?" + (f" Это VPN человека {who(owner)} — ему придёт "
                                                         "сообщение, что доступ отключён." if owner else ""),
                    inline=[[btn("🗑 Да, удалить", f"del:{rest}"), btn("Отмена", "cancel")]])
    if kind == "del":
        ok, out = vpn("del", rest)
        if ok:
            users = load_users()
            owner = owner_of(rest, users)
            if owner:
                owner["devices"].remove(rest)
                if not owner["devices"]:
                    owner["status"] = "new"
                save_users(users)
                send(owner["id"], f"⛔️ Доступ к VPN (<b>{rest}</b>) отключён владельцем.",
                     kb=USER_MENU if owner["devices"] else GUEST_MENU)
        return send(chat_id, ("✅ " if ok else "") + html.escape(out))
    if kind == "rot":
        flag = {"ports": ["--ports"], "obfs": ["--obfs"], "all": []}.get(rest, ["--ports"])
        send(chat_id, "Меняю, это займёт несколько секунд…")
        ok, out = vpn("rotate", *flag)
        send(chat_id, pre(out))
        if ok:
            send_all("Сменились настройки VPN")
        return
    if kind in ("ok", "no", "ban", "unban"):
        return decide(chat_id, kind, rest)


def decide(chat_id, kind, rest):
    uid, _, mode = rest.partition(":")
    users = load_users()
    u = users.get(uid)
    if not u:
        return send(chat_id, "Такого человека нет в базе.")
    if kind == "unban":
        u["status"] = "new"
        save_users(users)
        return send(chat_id, f"🔓 {who(u)} разблокирован — может снова прислать заявку.")
    if u.get("status") != "pending" and kind != "ban":
        return send(chat_id, f"Заявка {who(u)} уже разобрана.")
    if kind == "ok":
        name = new_device_name(u)
        ok, out = vpn("add", name, *(["--full"] if mode == "full" else []))
        if not ok:
            return send(chat_id, f"Не получилось создать конфиг:\n{pre(out)}")
        u["status"] = "approved"
        u.setdefault("devices", []).append(name)
        save_users(users)
        send(chat_id, f"✅ Выдал {who(u)}: устройство <b>{name}</b> ({mode_name(mode)}).")
        send(u["id"], f"✅ Тебе выдали VPN! Режим: {mode_name(mode)}.\n\n" + HOWTO, kb=USER_MENU)
        return send_configs(u["id"], name)
    if kind == "no":
        u["status"] = "denied"
        u["denied_at"] = int(time.time())
        save_users(users)
        send(u["id"], "❌ Заявку на VPN отклонили.", kb=USER_MENU if u.get("devices") else GUEST_MENU)
        return send(chat_id, f"❌ Отказано: {who(u)}")
    if kind == "ban":
        u["status"] = "banned"
        save_users(users)
        return send(chat_id, f"🚫 {who(u)} заблокирован: его заявки больше не придут. "
                             "Разблокировать — 👥 Люди.")


# ---------- остальные ----------

def guest_message(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    users = load_users()
    u = remember(users, msg["from"])
    save_users(users)
    menu = USER_MENU if u.get("devices") else GUEST_MENU
    if u.get("status") == "banned":
        return  # заблокированным не отвечаем
    if text in ("🙏 Попросить VPN", "🙏 Попросить ещё конфиг", "/request"):
        return ask_access(chat_id, u, users)
    if text in ("📄 Мои конфиги", "/configs"):
        if not u.get("devices"):
            return send(chat_id, "У тебя пока нет VPN. Нажми 🙏 Попросить VPN.", kb=GUEST_MENU)
        for name in u["devices"]:
            send(chat_id, f"📱 <b>{name}</b>")
            send_configs(chat_id, name)
        return
    if text in ("❓ Как подключить", "/help"):
        return send(chat_id, HOWTO, kb=menu)
    if text == "/start" and u.get("devices"):
        return send(chat_id, "С возвращением! Твои конфиги — по кнопке 📄 Мои конфиги.", kb=menu)
    send(chat_id, GUEST_HELP, kb=menu)


def ask_access(chat_id, u, users):
    if u.get("status") == "pending":
        return send(chat_id, "⏳ Заявка уже отправлена, жди ответа.")
    if u.get("status") == "denied" and time.time() - u.get("denied_at", 0) < REQUEST_COOLDOWN:
        return send(chat_id, "Заявку недавно отклонили. Попробуй позже.")
    if not ADMINS:
        return send(chat_id, "Бот ещё не настроен владельцем. Попробуй позже.")
    u["status"] = "pending"
    u["asked_at"] = int(time.time())
    save_users(users)
    send(chat_id, "📨 Заявка отправлена владельцу. Как только он одобрит — сюда придут файлы для подключения.")
    for a in ADMINS:
        request_card(a, u)


def guest_callback(chat_id, frm, kind, rest):
    if kind != "qr":
        return
    name, _, v = rest.partition(":")
    u = load_users().get(str(frm["id"]), {})
    if name in u.get("devices", []):  # только свои устройства
        send_qr(chat_id, name, int(v) if v in ("1", "2", "3") else 1)


# ---------- общее ----------

STATE = {}  # chat_id -> что ждём от админа


def send_all(header):
    """Новые конфиги: админам — все устройства, каждому человеку — его."""
    ds = devices()
    only = os.environ.get("VPN_ONLY", "").split()
    if only:  # только изменившиеся устройства
        ds = {n: d for n, d in ds.items() if n in only}
    if not ds:
        return
    users = load_users()
    for a in ADMINS:
        send(a, f"🔄 {html.escape(header)}. Старые конфиги больше не работают — вот новые "
                f"({len(ds)} устр.). Людям я разошлю их конфиги сам.")
        for name in ds:
            if not owner_of(name, users):
                send(a, f"📱 <b>{name}</b> — {mode_name(ds[name]['mode'])}")
                send_configs(a, name)
    for u in users.values():
        mine = [n for n in u.get("devices", []) if n in ds]
        if not mine or u.get("status") == "banned":
            continue
        send(u["id"], "🔄 VPN обновился — старые подключения больше не работают.\n"
                      "Удали старые туннели в AmneziaWG и добавь эти файлы:")
        for name in mine:
            send_configs(u["id"], name)


def handle(upd):
    msg = upd.get("message")
    cq = upd.get("callback_query")
    frm = (msg or cq or {}).get("from", {})
    if not frm or frm.get("is_bot"):
        return
    if msg and msg.get("chat", {}).get("type") != "private":
        return
    chat = (msg or cq.get("message", {})).get("chat", {}).get("id")
    try:
        if frm["id"] not in ADMINS and bind_admin(frm):
            send(chat, "👑 Ты админ этого бота. Твой Telegram ID закреплён — даже если сменишь ник, "
                       "права останутся у тебя.")
        is_admin = frm["id"] in ADMINS
        if msg:
            (admin_message if is_admin else guest_message)(msg)
        elif cq:
            api("answerCallbackQuery", callback_query_id=cq["id"])
            kind, _, rest = cq.get("data", "").partition(":")
            if is_admin:
                admin_callback(chat, kind, rest)
            else:
                guest_callback(chat, frm, kind, rest)
    except Exception as e:
        print(f"ошибка: {e!r}", file=sys.stderr)
        if chat and frm.get("id") in ADMINS:
            send(chat, f"⚠️ Ошибка: {html.escape(repr(e))}")


def run():
    if not TOKEN or not (ADMINS or ADMIN_USERNAME):
        sys.exit("в /etc/vpn/bot.env нет BOT_TOKEN или админа (ADMIN_IDS / ADMIN_USERNAME)")
    me = api("getMe")
    print(f"бот @{(me or {}).get('username', '?')} запущен, админы: {sorted(ADMINS) or '@' + ADMIN_USERNAME}",
          flush=True)
    api("setMyCommands", commands=[{"command": "start", "description": "Меню"},
                                   {"command": "help", "description": "Как подключить"}])
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
        if sys.argv[1] == "notify":
            for a in ADMINS:
                send(a, html.escape(text))
        else:
            send_all(text)
        return
    run()


if __name__ == "__main__":
    main()
