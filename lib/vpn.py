#!/usr/bin/env python3
"""vpn — управление личным VPN на AmneziaWG.

  vpn add ИМЯ [--full]    новое устройство (по умолчанию «обход»: через VPN всё, кроме российских сайтов)
  vpn list                устройства: подключены ли, когда последний раз, трафик
  vpn qr ИМЯ [1|2|3]      QR-код конфига прямо в терминале
  vpn show ИМЯ [1|2|3]    текст конфига
  vpn mode ИМЯ split|full сменить режим устройства
  vpn del ИМЯ             удалить устройство
  vpn ads ИМЯ on|off      блокировка рекламы у устройства (включается на сервере: bash adblock.sh)
  vpn status              здоровье сервера: VPN, правила, порты, сторож
  vpn rotate [--ports|--obfs]  сменить порты и/или маскировку (конфиги придётся импортировать заново)
  vpn set-ports ОСН [Р1 Р2]    задать порты вручную
  vpn regen               пересобрать файлы конфигов (после правки списка сетей)
  vpn nets [САЙТ ...]     режим «обход»: сколько сетей и как пойдут сайты (vpn nets youtube.com sberbank.ru)
  vpn tiktok [--check]    обновить сети TikTok (каждый час это делает vpn-tiktok.timer)
"""
import argparse
import bisect
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

ETC = "/etc/vpn"
SERVER = f"{ETC}/server.json"
CLIENTS = f"{ETC}/clients"
ENV_FILE = f"{ETC}/env"
NETS_BASE = f"{ETC}/blocked-nets.txt"
NETS_EXTRA = f"{ETC}/extra-nets.txt"
OUT = "/root/vpn-configs"
IFACE = "awg0"
AWG_CONF = f"/etc/amnezia/amneziawg/{IFACE}.conf"
SUBNET = ipaddress.ip_network("10.8.0.0/24")
MTU = 1280
DNS = "1.1.1.1, 1.0.0.1"
SPLIT_LIMIT = 1000   # сетей в режиме «обход»; своё значение: SPLIT_LIMIT=... в /etc/vpn/options
OPTIONS = f"{ETC}/options"
TIKTOK_FILE = f"{ETC}/tiktok-nets.txt"   # собирается сам: vpn tiktok (и каждый час vpn-tiktok.timer)
RU_FILE = f"{ETC}/ru-nets.txt"           # российские сети: в режиме «обход» идут напрямую

# TikTok всегда через VPN в режиме «обход»: сети TikTok/ByteDance (как у MOST) + адреса,
# на которые домены TikTok указывают прямо сейчас (из Нидерландов). Адреса CDN TikTok меняются,
# поэтому раз в час сервер проверяет, все ли они покрыты, и обновляет список, только если нет.
TIKTOK_ASNS = (138699, 396986)  # TikTok Pte. Ltd., ByteDance Inc.
TIKTOK_ASN_MAX = 40             # самых крупных IPv4-сетей из этих AS
TIKTOK_HOSTS = (
    "tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "webcast.tiktok.com",
    "tiktokv.com", "api.tiktokv.com", "api16-normal-c-useast1a.tiktokv.com", "api16-normal-c-useast2a.tiktokv.com",
    "api16-normal-c-alisg.tiktokv.com", "api19-normal-c-useast1a.tiktokv.com", "api22-normal-c-useast2a.tiktokv.com",
    "api22-normal-c-alisg.tiktokv.com", "api16-normal-useast5.tiktokv.us", "api16-normal-no1a.tiktokv.eu",
    "log.tiktokv.com", "log22-normal-c-useast2a.tiktokv.com", "mon.tiktokv.com", "mon-va.tiktokv.com",
    "mcs.tiktokv.com", "im-api-va.tiktok.com", "webcast16-normal-c-useast1a.tiktokv.com",
    "v16m.tiktokcdn.com", "v19.tiktokcdn.com", "v16-webapp.tiktok.com", "v16-webapp-prime.tiktok.com",
    "v19-webapp-prime.tiktok.com", "v16m-default.akamaized.net", "v16m-webapp.tiktokcdn-us.com",
    "p16-sign-va.tiktokcdn.com", "p16-sign-sg.tiktokcdn.com", "p16-sign.tiktokcdn-us.com",
    "p77-sign-va.tiktokcdn.com", "lf16-tiktok-web.tiktokcdn-us.com", "sf16-website-login.neutral.ttwstatic.com",
    "lf16-tiktok-common.ibytedtos.com", "sf-tb-sg.ibytedtos.com", "lf16-effectcdn.byteeffecttos-g.com",
)
TIKTOK_DNS_KEEP = 14 * 86400    # адрес по DNS держим в списке 14 дней после последнего появления
RU_SOURCES = (
    "https://www.ipdeny.com/ipblocks/data/aggregated/ru-aggregated.zone",
    "https://stat.ripe.net/data/country-resource-list/data.json?resource=RU&v4_format=prefix",
)
# Локальные и служебные сети: никогда не заворачиваем в VPN (домашний роутер, принтеры).
LOCAL_NETS = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
              "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/3")
FULL = "0.0.0.0/0, ::/0"
QR_MAX = 2900  # больше QR-код не вмещает
ADBLOCK_FLAG = f"{ETC}/adblock-on"   # ставит adblock.sh
ADBLOCK_DNS = str(SUBNET[1])          # dnsmasq с блокировкой рекламы на адресе сервера в VPN

# Порты: не 443, не больше 4 цифр. Плюс не берём известные порты VPN/прокси/VoIP —
# их ТСПУ и хостинги проверяют в первую очередь.
PORT_MIN, PORT_MAX = 1024, 9999
BAD_PORTS = {443, 1080, 1194, 1701, 1723, 1812, 1813, 3128, 3389, 3478, 3479, 4500, 5060,
             5061, 5349, 8000, 8080, 8443, 8888, 9000, 9001, 9050, 9051}

# Три варианта конфига на устройство: разные порты и разные «пустышки» перед рукопожатием.
# Jc/Jmin/Jmax задаёт только клиент (сервер пустышки просто отбрасывает), поэтому у вариантов
# они свои, а S1/S2/H1–H4 обязаны совпадать с сервером.
VARIANTS = {
    1: {"title": "основной", "junk": None, "i1": False},
    2: {"title": "резерв: другой порт и пустышки", "junk": (4, 40, 120), "i1": False},
    3: {"title": "резерв: маскировка под DNS", "junk": (3, 40, 90), "i1": True},
}

rnd = random.SystemRandom()
NAME_RE = re.compile(r"[A-Za-z0-9_]{1,12}")  # имя-N.conf ≤ 15 символов: ограничение iOS на имя туннеля


def die(msg):
    print(f"Ошибка: {msg}", file=sys.stderr)
    sys.exit(1)


def sh(cmd, inp=None, check=True):
    r = subprocess.run(cmd, input=inp, capture_output=True, text=True,
                       shell=isinstance(cmd, str), executable="/bin/bash" if isinstance(cmd, str) else None)
    if check and r.returncode != 0:
        die(f"{cmd if isinstance(cmd, str) else ' '.join(cmd)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def genkey():
    priv = sh(["awg", "genkey"])
    return priv, sh(["awg", "pubkey"], priv + "\n")


def load(path):
    with open(path) as f:
        return json.load(f)


def save(path, data):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def server():
    if not os.path.exists(SERVER):
        die("сервер ещё не настроен — запустите install.sh")
    return load(SERVER)


def clients():
    os.makedirs(CLIENTS, mode=0o700, exist_ok=True)
    out = {}
    for fn in sorted(os.listdir(CLIENTS)):
        if fn.endswith(".json"):
            c = load(f"{CLIENTS}/{fn}")
            out[c["name"]] = c
    return out


# ---------- порты и маскировка ----------

def used_udp_ports():
    used = set()
    for line in sh("ss -Hlnu", check=False).splitlines():
        parts = line.split()
        if len(parts) >= 4:
            port = parts[3].rsplit(":", 1)[-1]
            if port.isdigit():
                used.add(int(port))
    return used


def pick_ports(n, avoid=()):
    taken = used_udp_ports() | BAD_PORTS | set(avoid)
    pool = [p for p in range(PORT_MIN, PORT_MAX + 1) if p not in taken]
    return rnd.sample(pool, n)


def check_port(p):
    if not (PORT_MIN <= p <= PORT_MAX):
        die(f"порт {p}: нужен {PORT_MIN}–{PORT_MAX} (не больше 4 цифр)")
    if p == 443:
        die("порт 443 не используем")


def gen_obfs():
    s1 = rnd.randint(15, 150)
    s2 = rnd.choice([x for x in range(15, 151) if x != s1 + 56])  # иначе пакеты разной роли совпадут по длине
    h = rnd.sample(range(5, 2**31 - 1), 4)
    jmin = rnd.randint(40, 80)
    return {"Jc": rnd.randint(3, 6), "Jmin": jmin, "Jmax": jmin + rnd.randint(50, 150),
            "S1": s1, "S2": s2, "H1": h[0], "H2": h[1], "H3": h[2], "H4": h[3]}


def dns_i1(seed):
    """Маскирующий пакет I1 в форме ответа DNS: часть мобильных сетей пропускает его охотнее случайного."""
    r = random.Random(seed)
    label = r.randint(20, 62)
    answers = r.randint(1, 2)
    head = f"85800001{answers:04x}00000000{label:02x}"
    tail = "0463646e730669636c6f756403636f6d0000010001"  # .cdns.icloud.com, A, IN
    ttl = r.choice(["0000003c", "0000012c", "00000384", "00000e10"])
    for _ in range(answers):
        o1 = r.randint(1, 221)
        o1 = {10: 222, 127: 223}.get(o1, o1)
        tail += f"c00c00010001{ttl}0004{o1:02x}{r.randint(0, 255):02x}{r.randint(0, 255):02x}{r.randint(1, 254):02x}"
    return f"<r 2><b 0x{head}><rc {label}><b 0x{tail}>"


# ---------- сети для режима «обход» ----------

def read_nets(path):
    nets = []
    try:
        with open(path) as f:
            for n, line in enumerate(f, 1):
                s = line.split("#")[0].strip()
                if not s:
                    continue
                try:
                    nets.append(ipaddress.ip_network(s, strict=False))
                except ValueError:
                    die(f"{path}:{n}: «{s}» — не сеть")
    except FileNotFoundError:
        pass
    return nets


def option(name, default):
    """Свои настройки из /etc/vpn/options (строки ИМЯ=значение)."""
    try:
        with open(OPTIONS) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k == name and v:
                    return v
    except FileNotFoundError:
        pass
    return default


def ru_nets():
    nets = read_nets(RU_FILE)
    if not nets:
        try:
            nets = fetch_ru()
        except Exception as e:
            print(f"Внимание: не скачался список российских сетей ({e}) — режим «обход» временно "
                  "только по списку заблокированных сервисов", file=sys.stderr)
    return [n for n in nets if n.version == 4]


def gaps(direct):
    """Промежутки IPv4-пространства между сетями direct: (начало, конец)."""
    out, cur = [], 0
    for net in ipaddress.collapse_addresses(direct):
        start, end = int(net.network_address), int(net.broadcast_address)
        if start > cur:
            out.append((cur, start - 1))
        cur = max(cur, end + 1)
    if cur <= 2**32 - 1:
        out.append((cur, 2**32 - 1))
    return out


def cidrs(start, end):
    return list(ipaddress.summarize_address_range(ipaddress.IPv4Address(start), ipaddress.IPv4Address(end)))


def split_nets(srv):
    """Сети режима «обход»: ВСЁ, кроме российских и локальных сетей (как раздельный туннель у MOST).

    Российские сайты, банки, Госуслуги идут напрямую с устройства, всё зарубежное — через VPN.
    Чтобы список уложился в SPLIT_LIMIT сетей, самые мелкие зарубежные промежутки между
    российскими сетями отдаются «напрямую» (доли процента адресов). Список заблокированных
    сервисов (blocked-nets.txt, extra-nets.txt) и TikTok идут через VPN всегда.
    """
    limit = int(option("SPLIT_LIMIT", SPLIT_LIMIT))
    me = ipaddress.ip_network(f"{srv['ip']}/32")
    forced = [n for n in (read_nets(NETS_BASE) + read_nets(NETS_EXTRA)
                          + [t[0] for t in read_tiktok()]) if n.version == 4]
    ru = ru_nets()
    tunnel = []
    if ru:
        local = [ipaddress.ip_network(n) for n in LOCAL_NETS]
        gs = [(a, b, len(cidrs(a, b))) for a, b in gaps(ru + local + [me])]
        sizes = sorted({b - a + 1 for a, b, _ in gs})
        lo, hi = 0, len(sizes) - 1   # наименьший размер промежутка, при котором влезаем в limit
        while lo < hi:
            mid = (lo + hi) // 2
            if sum(n for a, b, n in gs if b - a + 1 >= sizes[mid]) <= limit:
                hi = mid
            else:
                lo = mid + 1
        tunnel = [net for a, b, _ in gs if b - a + 1 >= sizes[lo] for net in cidrs(a, b)]
    v4 = ipaddress.collapse_addresses(tunnel + forced)
    # Адрес самого сервера в туннель заворачивать нельзя — туннель замкнётся на себя.
    v4 = list(ipaddress.collapse_addresses(
        sub for n in v4 for sub in (n.address_exclude(me) if n.supernet_of(me) else [n])))
    # IPv6 — весь в туннель: у сервера IPv6 нет, приложения сразу переходят на IPv4 (так у MOST).
    return v4 + [ipaddress.ip_network("::/0")]


def split_allowed(srv):
    return ", ".join(map(str, split_nets(srv)))


# ---------- конфиги ----------

def write_env(srv):
    wan = srv["wan"]
    text = (f"IFACE={IFACE}\nPORT={srv['port']}\nBACKUP_PORTS=\"{' '.join(map(str, srv['backup']))}\"\n"
            f"WAN={wan}\nSUBNET={SUBNET}\nMTU={MTU}\n")
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def write_server_conf(srv):
    o = srv["obfs"]
    lines = ["[Interface]",
             f"PrivateKey = {srv['priv']}",
             f"Address = {SUBNET[1]}/{SUBNET.prefixlen}",
             f"ListenPort = {srv['port']}",
             f"MTU = {MTU}",
             *(f"{k} = {o[k]}" for k in ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4")),
             "PostUp = /usr/local/sbin/vpn-rules up",
             "PostDown = /usr/local/sbin/vpn-rules down"]
    for c in clients().values():
        lines += ["", f"# {c['name']}", "[Peer]", f"PublicKey = {c['pub']}", f"AllowedIPs = {c['ip']}/32"]
    os.makedirs(os.path.dirname(AWG_CONF), exist_ok=True)
    fd = os.open(AWG_CONF + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(AWG_CONF + ".tmp", AWG_CONF)


def client_conf(c, srv, variant, allowed_split=None):
    v = VARIANTS[variant]
    o = dict(srv["obfs"])
    if v["junk"]:
        o["Jc"], o["Jmin"], o["Jmax"] = v["junk"]
    port = [srv["port"], *srv["backup"]][variant - 1]
    allowed = FULL if c["mode"] == "full" else (allowed_split or split_allowed(srv))
    if ads_on(c) and c["mode"] != "full":
        allowed = f"{ADBLOCK_DNS}/32, {allowed}"  # 10.x — локальная сеть, в «обходе» её нет: добавляем DNS
    lines = ["[Interface]",
             f"PrivateKey = {c['priv']}",
             f"Address = {c['ip']}/32",
             f"DNS = {ADBLOCK_DNS if ads_on(c) else DNS}",
             f"MTU = {MTU}",
             *(f"{k} = {o[k]}" for k in ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"))]
    if v["i1"]:
        lines.append(f"I1 = {dns_i1(c['priv'])}")
    lines += ["", "[Peer]",
              f"PublicKey = {srv['pub']}",
              f"Endpoint = {srv['ip']}:{port}",
              f"AllowedIPs = {allowed}",
              "PersistentKeepalive = 25"]
    return "\n".join(lines) + "\n"


def ads_on(c):
    """Блокируется ли реклама у устройства: adblock.sh включён и у устройства её не выключали."""
    return os.path.exists(ADBLOCK_FLAG) and c.get("ads", True)


def conf_path(name, variant):
    return f"{OUT}/{name}/{name}-{variant}.conf"


def write_client_files(c, srv, allowed_split=None):
    d = f"{OUT}/{c['name']}"
    os.makedirs(d, mode=0o700, exist_ok=True)
    have_qr = shutil.which("qrencode")
    for v in VARIANTS:
        p = conf_path(c["name"], v)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(client_conf(c, srv, v, allowed_split))
        png = p[:-5] + ".png"
        if have_qr and os.path.getsize(p) <= QR_MAX:
            subprocess.run(["qrencode", "-o", png, "-s", "6", "-r", p], check=False)
        elif os.path.exists(png):
            os.remove(png)  # конфиг слишком длинный для QR — старую картинку убираем


def regen_all(srv):
    allowed = split_allowed(srv)
    os.makedirs(OUT, mode=0o700, exist_ok=True)
    for c in clients().values():
        write_client_files(c, srv, allowed)


def iface_up():
    return subprocess.run(["ip", "link", "show", IFACE], capture_output=True).returncode == 0


def apply(srv, restart=False):
    write_env(srv)
    write_server_conf(srv)
    if restart or not iface_up():
        sh(["systemctl", "restart", f"awg-quick@{IFACE}"])
    else:
        sh(f"awg syncconf {IFACE} <(awg-quick strip {IFACE})")


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "vpn-nets/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def fetch_ru():
    """Российские сети (ipdeny, запасной — RIPEstat) -> RU_FILE."""
    err = None
    for url in RU_SOURCES:
        try:
            body = http_get(url).strip()
            if body.startswith("{"):
                lines = json.loads(body)["data"]["resources"]["ipv4"]
            else:
                lines = body.splitlines()
            nets = []
            for p in lines:
                p = p.strip()
                if not p or p.startswith("#"):
                    continue
                if "-" in p:
                    a, b = (ipaddress.IPv4Address(x.strip()) for x in p.split("-"))
                    nets += ipaddress.summarize_address_range(a, b)
                else:
                    nets.append(ipaddress.ip_network(p, strict=False))
            nets = list(ipaddress.collapse_addresses(n for n in nets if n.version == 4))
            if len(nets) < 1000:
                raise ValueError(f"подозрительно мало сетей: {len(nets)}")
            with open(RU_FILE + ".tmp", "w") as f:
                f.write(f"# российские сети ({url}), {time.strftime('%F')}\n" + "\n".join(map(str, nets)) + "\n")
            os.replace(RU_FILE + ".tmp", RU_FILE)
            return nets
        except Exception as e:
            err = e
    raise RuntimeError(err)


def fetch_asn(asn):
    data = json.loads(http_get(f"https://stat.ripe.net/data/announced-prefixes/data.json?resource=AS{asn}"))
    return [ipaddress.ip_network(p["prefix"], strict=False) for p in data["data"]["prefixes"]]


def resolve_tiktok():
    ips = set()
    for host in TIKTOK_HOSTS:
        try:
            for info in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM):
                ips.add(ipaddress.ip_address(info[4][0]))
        except OSError:
            pass
    return ips


def read_tiktok():
    """Строки TIKTOK_FILE: «сеть # asn» или «сеть # dns ВРЕМЯ»."""
    out = []
    try:
        with open(TIKTOK_FILE) as f:
            for line in f:
                net, _, note = line.partition("#")
                net, note = net.strip(), note.split()
                if net and note:
                    out.append((ipaddress.ip_network(net), note[0], int(note[1]) if len(note) > 1 else 0))
    except FileNotFoundError:
        pass
    return out


def cmd_tiktok(a):
    if a.print:  # для vpn-warp: сети TikTok (IPv4) по одной на строку
        for n, _, _ in read_tiktok():
            if n.version == 4:
                print(n)
        return
    srv = server()
    ips = resolve_tiktok()
    if not ips:
        die("не удалось получить адреса TikTok по DNS — есть ли интернет на сервере?")
    try:
        before = split_allowed(srv)
    except SystemExit:
        before = None
    # Российские сети: обновляем раз в неделю (нужны, чтобы не завернуть их в VPN).
    if not os.path.exists(RU_FILE) or time.time() - os.path.getmtime(RU_FILE) > 7 * 86400:
        try:
            fetch_ru()
        except Exception as e:
            if not os.path.exists(RU_FILE):
                die(f"не скачался список российских сетей: {e}")
    ru = read_nets(RU_FILE)
    ru_starts = sorted((int(n.network_address), int(n.broadcast_address)) for n in ru)
    starts = [lo for lo, _ in ru_starts]

    def is_ru(ip):
        i = bisect.bisect_right(starts, int(ip)) - 1
        return i >= 0 and ru_starts[i][1] >= int(ip)

    if a.check and os.path.exists(TIKTOK_FILE) and before:
        covered = [ipaddress.ip_network(n.strip()) for n in before.split(",")]
        missing = [ip for ip in ips if not is_ru(ip) and not any(ip in n for n in covered if n.version == 4)]
        if not missing:
            print(f"{time.strftime('%F %T')} TikTok: все {len(ips)} адресов уже идут через VPN")
            return
        print(f"{time.strftime('%F %T')} TikTok переехал: {len(missing)} новых адресов из {len(ips)} — обновляю")

    now = int(time.time())
    old = read_tiktok()
    dns = {n: ts for n, kind, ts in old if kind == "dns" and now - ts < TIKTOK_DNS_KEEP}
    for ip in ips:
        if not is_ru(ip):  # российские кэши TikTok не трогаем
            dns[ipaddress.ip_network(f"{ip}/24", strict=False)] = now
    asn_nets = []
    old_asn = [n for n, kind, _ in old if kind == "asn"]
    fresh = os.path.exists(TIKTOK_FILE) and time.time() - os.path.getmtime(TIKTOK_FILE) < 86400
    for asn in (() if fresh and old_asn else TIKTOK_ASNS):  # сети ByteDance меняются редко — раз в сутки
        try:
            asn_nets += fetch_asn(asn)
        except Exception as e:
            print(f"сети AS{asn} не скачались: {e}", file=sys.stderr)
    if asn_nets:
        v4 = sorted((n for n in ipaddress.collapse_addresses(n for n in asn_nets if n.version == 4)),
                    key=lambda n: n.prefixlen)[:TIKTOK_ASN_MAX]
        v6 = sorted((n for n in ipaddress.collapse_addresses(n for n in asn_nets if n.version == 6)),
                    key=lambda n: n.prefixlen)[:2]
        asn_nets = v4 + v6
    else:  # свежие уже есть или RIPEstat недоступен — оставляем прежние
        asn_nets = old_asn

    lines = [f"# Собрано автоматически: vpn tiktok, {time.strftime('%F %T')}. Руками не править.",
             *(f"{n} # asn" for n in asn_nets),
             *(f"{n} # dns {ts}" for n, ts in sorted(dns.items()))]
    with open(TIKTOK_FILE + ".tmp", "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(TIKTOK_FILE + ".tmp", TIKTOK_FILE)

    after = split_allowed(srv)
    if after == before:
        print(f"TikTok: {len(ips)} адресов, список сетей не изменился — конфиги прежние.")
        return
    regen_all(srv)
    final = [n for n in split_nets(srv) if n.version == 4]
    cov = sum(1 for ip in ips if any(ip in n for n in final))
    print(f"TikTok в режиме «обход»: через VPN {cov} из {len(ips)} его адресов"
          f"{' — все' if cov == len(ips) else ' (остальные — российские кэши или не влезли в лимит)'}.")
    print(f"Конфиги «обхода» пересобраны ({len(split_nets(srv))} сетей) — импортируйте заново.")
    push_bot("Обновился список сетей (TikTok)")


BOT = "/usr/local/bin/vpn-bot"
BOT_ENV = f"{ETC}/bot.env"


def push_bot(reason):
    """Если настроен Telegram-бот — он сам пришлёт новые конфиги всех устройств (в фоне)."""
    if os.environ.get("VPN_NO_PUSH") or not (os.path.exists(BOT) and os.path.exists(BOT_ENV)):
        return
    subprocess.Popen([BOT, "send-all", reason], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    print("Новые конфиги придут в Telegram-бот.")


# ---------- команды ----------

def cmd_init(a):
    os.makedirs(ETC, mode=0o700, exist_ok=True)
    if os.path.exists(SERVER):
        srv = load(SERVER)
        ip_changed = bool(a.ip) and a.ip != srv["ip"]
        if a.ip:
            srv["ip"] = a.ip
        if a.wan:
            srv["wan"] = a.wan
        save(SERVER, srv)
        apply(srv, restart=True)
        regen_all(srv)
        print(f"Сервер уже настроен: порты {srv['port']} (+ резерв {', '.join(map(str, srv['backup']))})")
        if ip_changed:
            push_bot(f"Сменился IP сервера: теперь {srv['ip']}")
        return
    if not a.ip or not a.wan:
        die("нужны --ip и --wan")
    if a.port:
        check_port(a.port)
    main = a.port or pick_ports(1)[0]
    backup = pick_ports(2, avoid=[main])
    priv, pub = genkey()
    srv = {"priv": priv, "pub": pub, "ip": a.ip, "wan": a.wan, "port": main, "backup": backup,
           "obfs": gen_obfs(), "created": int(time.time())}
    save(SERVER, srv)
    apply(srv, restart=True)
    print(f"Сервер создан: UDP {main} (резервные {backup[0]}, {backup[1]})")


def next_ip():
    used = {c["ip"] for c in clients().values()}
    for host in list(SUBNET.hosts())[1:]:
        if str(host) not in used:
            return str(host)
    die("кончились адреса (253 устройства)")


def get_client(name):
    c = clients().get(name)
    if not c:
        die(f"устройства «{name}» нет. Список: vpn list")
    return c


def cmd_add(a):
    srv = server()
    if not NAME_RE.fullmatch(a.name):
        die("имя — латиница, цифры или _, до 12 символов (например iphone, pc, mama)")
    if a.name in clients():
        die(f"«{a.name}» уже есть. Показать: vpn qr {a.name}")
    priv, pub = genkey()
    c = {"name": a.name, "priv": priv, "pub": pub, "ip": next_ip(),
         "mode": "full" if a.full else "split", "created": int(time.time())}
    write_client_files(c, srv)
    save(f"{CLIENTS}/{a.name}.json", c)
    try:
        apply(srv)
    except SystemExit:
        os.remove(f"{CLIENTS}/{a.name}.json")
        shutil.rmtree(f"{OUT}/{a.name}", ignore_errors=True)
        raise
    print(f"Устройство «{a.name}» добавлено ({mode_title(c['mode'])}).")
    print_files(c)


def mode_title(m):
    return "режим «обход»: через VPN всё, кроме российских сайтов" if m == "split" else "режим «весь трафик»"


def print_files(c):
    print(f"Файлы (скачайте через SFTP в Termius): {OUT}/{c['name']}/")
    for v, info in VARIANTS.items():
        print(f"  {c['name']}-{v}.conf  — {info['title']}")
    if c["mode"] == "full":
        print(f"QR-код в терминале: vpn qr {c['name']}   (резервные: vpn qr {c['name']} 2 / 3)")
    else:
        print("Конфиг «обхода» длинный (сотни сетей) — QR-кода нет, переносите файл.")


def cmd_qr(a):
    srv = server()
    c = get_client(a.name)
    if not shutil.which("qrencode"):
        die("нет qrencode: apt install qrencode")
    text = client_conf(c, srv, a.variant)
    print(f"{c['name']}-{a.variant}: {VARIANTS[a.variant]['title']}, {mode_title(c['mode'])}")
    if len(text.encode()) > QR_MAX:
        die(f"конфиг «обхода» слишком длинный для QR-кода ({len(text.encode())} байт). Перенесите файл "
            f"{conf_path(c['name'], a.variant)} через SFTP в Termius или через бота")
    subprocess.run(["qrencode", "-t", "ansiutf8", "-m", "2"], input=text, text=True)


def cmd_show(a):
    c = get_client(a.name)
    print(client_conf(c, server(), a.variant), end="")


def cmd_mode(a):
    srv = server()
    c = get_client(a.name)
    c["mode"] = a.mode
    save(f"{CLIENTS}/{c['name']}.json", c)
    write_client_files(c, srv)
    print(f"«{c['name']}»: {mode_title(a.mode)}. Импортируйте конфиг заново:")
    print_files(c)


def cmd_del(a):
    srv = server()
    get_client(a.name)
    os.remove(f"{CLIENTS}/{a.name}.json")
    shutil.rmtree(f"{OUT}/{a.name}", ignore_errors=True)
    apply(srv)
    print(f"«{a.name}» удалён, его конфиги больше не работают.")


def peers_dump():
    out = {}
    for line in sh(["awg", "show", IFACE, "dump"], check=False).splitlines()[1:]:
        f = line.split("\t")
        if len(f) >= 7:
            out[f[0]] = {"endpoint": f[2], "hs": int(f[4] or 0), "rx": int(f[5] or 0), "tx": int(f[6] or 0)}
    return out


def human(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def ago(ts):
    if not ts:
        return "ни разу"
    s = int(time.time()) - ts
    if s < 180:
        return "🟢 сейчас"
    if s < 3600:
        return f"{s // 60} мин назад"
    if s < 86400:
        return f"{s // 3600} ч назад"
    return f"{s // 86400} дн назад"


def cmd_list(a):
    cs = clients()
    if not cs:
        print("Устройств нет. Добавить: vpn add iphone")
        return
    dump = peers_dump()
    print(f"{'Имя':<13}{'Режим':<8}{'Рекл':<6}{'Адрес':<12}{'Подключение':<16}{'Трафик ↓/↑'}")
    for c in cs.values():
        d = dump.get(c["pub"], {})
        traffic = f"{human(d.get('tx', 0))} / {human(d.get('rx', 0))}" if d else "—"
        ads = ("🛡" if ads_on(c) else "—") if os.path.exists(ADBLOCK_FLAG) else ""
        print(f"{c['name']:<13}{('обход' if c['mode'] == 'split' else 'весь'):<8}{ads:<6}{c['ip']:<12}"
              f"{ago(d.get('hs', 0)):<16}{traffic}")


def port_counters():
    out = {}
    for line in sh("iptables -t mangle -L VPN_CNT -nvx", check=False).splitlines()[2:]:
        m = re.search(r"^\s*(\d+)\s+(\d+).*dpt:(\d+)", line)
        if m:
            out[int(m.group(3))] = int(m.group(1))
    return out


def cmd_status(a):
    srv = server()
    ok = lambda b: "✅" if b else "❌"
    active = subprocess.run(["systemctl", "is-active", "--quiet", f"awg-quick@{IFACE}"]).returncode == 0
    module = subprocess.run("lsmod | grep -q '^amneziawg'", shell=True).returncode == 0
    fwd = sh("sysctl -n net.ipv4.ip_forward", check=False) == "1"
    rules = subprocess.run("/usr/local/sbin/vpn-rules check", shell=True, capture_output=True).returncode == 0
    timer = subprocess.run(["systemctl", "is-active", "--quiet", "vpn-watchdog.timer"]).returncode == 0
    dump = peers_dump()
    online = sum(1 for d in dump.values() if d["hs"] and time.time() - d["hs"] < 180)
    print(f"{ok(module)} модуль AmneziaWG")
    print(f"{ok(active)} VPN (awg-quick@{IFACE})")
    print(f"{ok(fwd)} пересылка пакетов")
    print(f"{ok(rules)} правила iptables (NAT, резервные порты, MSS)")
    print(f"{ok(timer)} сторож (vpn-watchdog, раз в минуту)")
    if os.path.exists(ADBLOCK_FLAG):
        dns_ok = subprocess.run(["systemctl", "is-active", "--quiet", "dnsmasq"]).returncode == 0
        print(f"{ok(dns_ok)} блокировка рекламы (dnsmasq на {ADBLOCK_DNS})")
    if os.path.exists(f"{ETC}/warp-on"):
        w = subprocess.run("/usr/local/sbin/vpn-warp check", shell=True, capture_output=True).returncode == 0
        print(f"{ok(w)} TikTok через Cloudflare WARP" + ("" if w else " (сейчас идёт с IP сервера; bash warp.sh status)"))
    print(f"Сервер: {srv['ip']}, MTU {MTU}, устройств: {len(clients())}, подключено сейчас: {online}")
    r = resources(srv["wan"])
    print(f"Процессор {r['cpu']}% · память {r['mem']}% · диск {r['disk']}% · нагрузка {r['load']}")
    print(f"Канал сейчас: ↓{r['down']:.0f} ↑{r['up']:.0f} Мбит/с · с запуска сервера {r['total_gb']:.0f} ГБ")
    cnt = port_counters()
    print("Порты UDP (сколько пакетов пришло с момента запуска):")
    for i, p in enumerate([srv["port"], *srv["backup"]], 1):
        print(f"  {p:<6} конфиги *-{i}.conf  — {cnt.get(p, 0)} пакетов")
    print("Журнал сторожа: tail /var/log/vpn-watchdog.log")


def resources(wan):
    """Процессор, память, диск и канал (как экран «🖥 Серверы» в MOST)."""
    def cpu_times():
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
        return sum(v), v[3] + v[4]

    def net(iface):
        try:
            with open("/proc/net/dev") as f:
                for line in f:
                    if line.strip().startswith(iface + ":"):
                        v = line.split(":", 1)[1].split()
                        return int(v[0]), int(v[8])
        except OSError:
            pass
        return 0, 0

    t1, i1 = cpu_times()
    n1 = net(wan)
    time.sleep(1)
    t2, i2 = cpu_times()
    n2 = net(wan)
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            mem[k] = int(v.split()[0])
    du = shutil.disk_usage("/")
    with open("/proc/loadavg") as f:
        load = f.read().split()[0]
    return {"cpu": round(100 * (1 - (i2 - i1) / max(1, t2 - t1))),
            "mem": round(100 * (1 - mem["MemAvailable"] / mem["MemTotal"])),
            "disk": round(100 * du.used / du.total), "load": load,
            "down": (n2[0] - n1[0]) * 8 / 1e6, "up": (n2[1] - n1[1]) * 8 / 1e6,
            "total_gb": (n2[0] + n2[1]) / 1e9}


def cmd_ads(a):
    srv = server()
    c = get_client(a.name)
    if not os.path.exists(ADBLOCK_FLAG):
        die("блокировка рекламы не включена на сервере: bash adblock.sh")
    c["ads"] = a.state == "on"
    save(f"{CLIENTS}/{c['name']}.json", c)
    write_client_files(c, srv)
    print(f"«{c['name']}»: блокировка рекламы {'включена' if c['ads'] else 'выключена'}. Импортируйте конфиг заново:")
    print_files(c)


def cmd_rotate(a):
    srv = server()
    old = [srv["port"], *srv["backup"]]
    if not a.obfs:
        ports = pick_ports(3, avoid=old)
        srv["port"], srv["backup"] = ports[0], ports[1:]
    if not a.ports:
        srv["obfs"] = gen_obfs()
    save(SERVER, srv)
    apply(srv, restart=True)
    regen_all(srv)
    print(f"Готово. Порты: {srv['port']} (резерв {', '.join(map(str, srv['backup']))})"
          f"{', маскировка новая' if not a.ports else ''}.")
    print("Старые конфиги больше НЕ работают — импортируйте новые из", OUT)
    push_bot("Сменились порты" if a.ports else "Сменилась маскировка" if a.obfs else "Сменились порты и маскировка")


def cmd_set_ports(a):
    srv = server()
    ports = [a.main] + (a.backup or [])
    for p in ports:
        check_port(p)
    if len(set(ports)) != len(ports):
        die("порты должны быть разными")
    current = {srv["port"], *srv["backup"]}
    busy = (used_udp_ports() - current) & set(ports)
    if busy:
        die(f"порт занят другой программой: {', '.join(map(str, busy))}")
    if len(ports) == 1:
        ports += pick_ports(2, avoid=ports)
    elif len(ports) == 2:
        ports += pick_ports(1, avoid=ports)
    srv["port"], srv["backup"] = ports[0], ports[1:3]
    save(SERVER, srv)
    apply(srv, restart=True)
    regen_all(srv)
    print(f"Порты: {srv['port']} (резерв {', '.join(map(str, srv['backup']))}). Импортируйте конфиги заново из {OUT}")
    push_bot(f"Порты заданы вручную: {srv['port']}, {', '.join(map(str, srv['backup']))}")


def cmd_regen(a):
    srv = server()
    regen_all(srv)
    print(f"Конфиги пересобраны в {OUT}. Импортируйте заново.")
    push_bot(os.environ.get("VPN_PUSH_REASON", "Обновился список сетей"))


def cmd_nets(a):
    srv = server()
    nets = split_nets(srv)
    v4 = [n for n in nets if n.version == 4]
    ru = ru_nets()
    local = sum(ipaddress.ip_network(n).num_addresses for n in LOCAL_NETS)
    foreign = 2**32 - sum(n.num_addresses for n in ipaddress.collapse_addresses(ru)) - local
    via = sum(n.num_addresses for n in v4)
    print(f"Режим «обход»: через VPN всё, кроме российских сетей ({len(ru)} шт., напрямую).")
    print(f"В конфиге {len(nets)} сетей (лимит {option('SPLIT_LIMIT', SPLIT_LIMIT)}); "
          f"зарубежных адресов через VPN ≈ {min(100, 100 * via / max(1, foreign)):.1f}%.")
    tk = read_tiktok()
    if tk:
        print(f"TikTok всегда через VPN: {len(tk)} сетей ({TIKTOK_FILE}), обновление раз в час.")
    print(f"Всегда через VPN: {NETS_BASE} и свои сети {NETS_EXTRA}, потом: vpn regen")
    if a.verbose:
        print("\n".join(map(str, nets)))
    if a.check:
        for host in a.check:
            try:
                ip = ipaddress.ip_address(socket.gethostbyname(host))
            except OSError:
                print(f"  {host}: не резолвится")
                continue
            in_ru = any(ip in n for n in ru)
            route = "VPN" if any(ip in n for n in v4) else "напрямую"
            print(f"  {host:24} {str(ip):16} {'РФ ' if in_ru else 'зар'} -> {route}")


def cmd_apply(a):
    srv = server()
    apply(srv, restart=a.restart)


def main():
    if os.geteuid() != 0:
        die("нужен root: sudo vpn ...")
    p = argparse.ArgumentParser(prog="vpn", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("init"); s.add_argument("--ip"); s.add_argument("--wan"); s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("add"); s.add_argument("name"); s.add_argument("--full", action="store_true")
    s.set_defaults(fn=cmd_add)
    for name, fn in (("qr", cmd_qr), ("show", cmd_show)):
        s = sub.add_parser(name); s.add_argument("name"); s.add_argument("variant", nargs="?", type=int, default=1,
                                                                          choices=list(VARIANTS))
        s.set_defaults(fn=fn)
    s = sub.add_parser("mode"); s.add_argument("name"); s.add_argument("mode", choices=["split", "full"])
    s.set_defaults(fn=cmd_mode)
    s = sub.add_parser("del"); s.add_argument("name"); s.set_defaults(fn=cmd_del)
    s = sub.add_parser("ads"); s.add_argument("name"); s.add_argument("state", choices=["on", "off"])
    s.set_defaults(fn=cmd_ads)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("rotate"); g = s.add_mutually_exclusive_group()
    g.add_argument("--ports", action="store_true", help="только порты"); g.add_argument("--obfs", action="store_true", help="только маскировку")
    s.set_defaults(fn=cmd_rotate)
    s = sub.add_parser("set-ports"); s.add_argument("main", type=int); s.add_argument("backup", type=int, nargs="*")
    s.set_defaults(fn=cmd_set_ports)
    sub.add_parser("regen").set_defaults(fn=cmd_regen)
    s = sub.add_parser("tiktok"); s.add_argument("--check", action="store_true", help="обновить, только если TikTok переехал")
    s.add_argument("--print", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_tiktok)
    s = sub.add_parser("nets"); s.add_argument("-v", "--verbose", action="store_true")
    s.add_argument("check", nargs="*", help="сайты: как они пойдут (напр. vpn nets youtube.com sberbank.ru)")
    s.set_defaults(fn=cmd_nets)
    s = sub.add_parser("apply"); s.add_argument("--restart", action="store_true"); s.set_defaults(fn=cmd_apply)
    a = p.parse_args()
    if not a.cmd:
        p.print_help()
        return
    a.fn(a)


if __name__ == "__main__":
    main()
