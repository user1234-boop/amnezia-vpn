#!/usr/bin/env python3
"""vpn — управление личным VPN на AmneziaWG.

  vpn add ИМЯ [--full]    новое устройство (по умолчанию режим «обход»: через VPN только заблокированное)
  vpn list                устройства: подключены ли, когда последний раз, трафик
  vpn qr ИМЯ [1|2|3]      QR-код конфига прямо в терминале
  vpn show ИМЯ [1|2|3]    текст конфига
  vpn mode ИМЯ split|full сменить режим устройства
  vpn del ИМЯ             удалить устройство
  vpn status              здоровье сервера: VPN, правила, порты, сторож
  vpn rotate [--ports|--obfs]  сменить порты и/или маскировку (конфиги придётся импортировать заново)
  vpn set-ports ОСН [Р1 Р2]    задать порты вручную
  vpn regen               пересобрать файлы конфигов (после правки списка сетей)
  vpn nets                сколько сетей в режиме «обход»
"""
import argparse
import ipaddress
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time

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
MAX_NETS = 100
FULL = "0.0.0.0/0, ::/0"

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


def split_nets(srv):
    nets = read_nets(NETS_BASE) + read_nets(NETS_EXTRA)
    v4 = list(ipaddress.collapse_addresses(n for n in nets if n.version == 4))
    v6 = list(ipaddress.collapse_addresses(n for n in nets if n.version == 6))
    # Адрес самого сервера в туннель заворачивать нельзя — туннель замкнётся на себя.
    me = ipaddress.ip_network(f"{srv['ip']}/32")
    v4 = list(ipaddress.collapse_addresses(
        sub for n in v4 for sub in (n.address_exclude(me) if n.supernet_of(me) else [n])))
    return v4 + v6


def split_allowed(srv):
    nets = split_nets(srv)
    if len(nets) > MAX_NETS:
        die(f"в режиме «обход» {len(nets)} сетей, а можно не больше {MAX_NETS} "
            f"(приложения Amnezia с большим списком работают плохо). Сократите {NETS_EXTRA}")
    return ", ".join(map(str, nets))


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
    lines = ["[Interface]",
             f"PrivateKey = {c['priv']}",
             f"Address = {c['ip']}/32",
             f"DNS = {DNS}",
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
        if have_qr:
            subprocess.run(["qrencode", "-o", p[:-5] + ".png", "-s", "6", "-r", p], check=False)


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


# ---------- команды ----------

def cmd_init(a):
    os.makedirs(ETC, mode=0o700, exist_ok=True)
    if os.path.exists(SERVER):
        srv = load(SERVER)
        if a.ip:
            srv["ip"] = a.ip
        if a.wan:
            srv["wan"] = a.wan
        save(SERVER, srv)
        apply(srv, restart=True)
        regen_all(srv)
        print(f"Сервер уже настроен: порты {srv['port']} (+ резерв {', '.join(map(str, srv['backup']))})")
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
    return "режим «обход»: через VPN только заблокированное" if m == "split" else "режим «весь трафик»"


def print_files(c):
    print(f"Файлы (скачайте через SFTP в Termius): {OUT}/{c['name']}/")
    for v, info in VARIANTS.items():
        print(f"  {c['name']}-{v}.conf  — {info['title']}")
    print(f"QR-код в терминале: vpn qr {c['name']}   (резервные: vpn qr {c['name']} 2 / 3)")


def cmd_qr(a):
    srv = server()
    c = get_client(a.name)
    if not shutil.which("qrencode"):
        die("нет qrencode: apt install qrencode")
    text = client_conf(c, srv, a.variant)
    print(f"{c['name']}-{a.variant}: {VARIANTS[a.variant]['title']}, {mode_title(c['mode'])}")
    if c["mode"] == "split":
        print("Конфиг длинный (список сетей) — если QR не влезает в экран, уменьшите шрифт в Termius\n"
              f"или скачайте {conf_path(c['name'], a.variant)[:-5]}.png / .conf через SFTP.")
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
    print(f"{'Имя':<13}{'Режим':<8}{'Адрес':<12}{'Подключение':<16}{'Трафик ↓/↑'}")
    for c in cs.values():
        d = dump.get(c["pub"], {})
        traffic = f"{human(d.get('tx', 0))} / {human(d.get('rx', 0))}" if d else "—"
        print(f"{c['name']:<13}{('обход' if c['mode'] == 'split' else 'весь'):<8}{c['ip']:<12}"
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
    print(f"Сервер: {srv['ip']}, MTU {MTU}, устройств: {len(clients())}, подключено сейчас: {online}")
    cnt = port_counters()
    print("Порты UDP (сколько пакетов пришло с момента запуска):")
    for i, p in enumerate([srv["port"], *srv["backup"]], 1):
        print(f"  {p:<6} конфиги *-{i}.conf  — {cnt.get(p, 0)} пакетов")
    print("Журнал сторожа: tail /var/log/vpn-watchdog.log")


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


def cmd_regen(a):
    srv = server()
    regen_all(srv)
    print(f"Конфиги пересобраны в {OUT} ({len(split_nets(srv))} сетей в режиме «обход»). Импортируйте заново.")


def cmd_nets(a):
    srv = server()
    nets = split_nets(srv)
    print(f"В режиме «обход» через VPN идут {len(nets)} сетей (лимит {MAX_NETS}).")
    print(f"Основной список: {NETS_BASE} (обновляется при git pull + install.sh)")
    print(f"Свои сети: {NETS_EXTRA} (по одной на строку), потом: vpn regen")
    if a.verbose:
        print("\n".join(map(str, nets)))


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
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("rotate"); g = s.add_mutually_exclusive_group()
    g.add_argument("--ports", action="store_true", help="только порты"); g.add_argument("--obfs", action="store_true", help="только маскировку")
    s.set_defaults(fn=cmd_rotate)
    s = sub.add_parser("set-ports"); s.add_argument("main", type=int); s.add_argument("backup", type=int, nargs="*")
    s.set_defaults(fn=cmd_set_ports)
    sub.add_parser("regen").set_defaults(fn=cmd_regen)
    s = sub.add_parser("nets"); s.add_argument("-v", "--verbose", action="store_true"); s.set_defaults(fn=cmd_nets)
    s = sub.add_parser("apply"); s.add_argument("--restart", action="store_true"); s.set_defaults(fn=cmd_apply)
    a = p.parse_args()
    if not a.cmd:
        p.print_help()
        return
    a.fn(a)


if __name__ == "__main__":
    main()
