"""Network discovery helpers for webterm.

Everything here is best-effort and degrades instead of failing: if a binary or an
optional package is missing the function says so in its result rather than raising.
Nothing in this module trusts user input blindly — hosts are resolved and validated
before any socket is opened, and the caller is responsible for the public-target
confirmation (see check_target in server.py).
"""

import asyncio
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import time
import urllib.request

# ---------------------------------------------------------------- small helpers

PRIVATE_OK = ("10.", "192.168.", "172.", "127.", "169.254.", "fe80:", "fc", "fd", "::1")

# A tiny slice of the IEEE OUI list: enough to label the usual suspects on a LAN.
OUI = {
    "00:50:56": "VMware", "00:0C:29": "VMware", "00:1C:14": "VMware",
    "08:00:27": "VirtualBox", "52:54:00": "QEMU/KVM", "00:16:3E": "Xen",
    "02:42:AC": "Docker", "B8:27:EB": "Raspberry Pi", "DC:A6:32": "Raspberry Pi",
    "E4:5F:01": "Raspberry Pi", "28:CD:C1": "Raspberry Pi",
    "00:1A:11": "Google", "F4:F5:D8": "Google", "DA:A1:19": "Google",
    "3C:5A:B4": "Google", "00:17:88": "Philips Hue", "EC:FA:BC": "Espressif",
    "24:0A:C4": "Espressif", "A4:CF:12": "Espressif", "50:02:91": "Espressif",
    "00:1B:63": "Apple", "AC:BC:32": "Apple", "F0:18:98": "Apple",
    "3C:07:54": "Apple", "A8:60:B6": "Apple", "00:25:00": "Apple",
    "00:1D:7E": "Cisco-Linksys", "00:18:39": "Cisco-Linksys", "C0:56:27": "Belkin",
    "00:24:B2": "Netgear", "A0:40:A0": "Netgear", "20:4E:7F": "Netgear",
    "00:1F:33": "Netgear", "C4:3D:C7": "Netgear", "E0:91:F5": "Netgear",
    "00:26:5A": "D-Link", "1C:7E:E5": "D-Link", "90:94:E4": "D-Link",
    "00:1E:58": "D-Link", "F8:1A:67": "TP-Link", "50:C7:BF": "TP-Link",
    "A4:2B:B0": "TP-Link", "C0:25:E9": "TP-Link", "14:CC:20": "TP-Link",
    "00:09:0F": "Fortinet", "00:04:F2": "Polycom", "00:80:77": "Brother",
    "00:00:48": "Epson", "00:1B:A9": "Brother", "00:21:5A": "HP",
    "3C:D9:2B": "HP", "00:1C:C4": "HP", "B4:B5:2F": "HP",
    "00:11:32": "Synology", "00:08:9B": "ICP Electronics", "24:5E:BE": "QNAP",
    "00:1F:16": "Wistron", "FC:EC:DA": "Ubiquiti", "74:83:C2": "Ubiquiti",
    "68:D7:9A": "Ubiquiti", "04:18:D6": "Ubiquiti", "78:8A:20": "Ubiquiti",
    "00:15:5D": "Hyper-V", "00:05:69": "VMware", "00:1E:C9": "Dell",
    "B0:83:FE": "Dell", "18:03:73": "Dell", "00:26:B9": "Dell",
    "8C:16:45": "Samsung", "00:12:FB": "Samsung", "5C:F6:DC": "Samsung",
    "D0:03:DF": "Amazon", "FC:65:DE": "Amazon", "44:65:0D": "Amazon",
    "00:04:20": "Slim Devices", "00:0E:58": "Sonos", "5C:AA:FD": "Sonos",
    "B8:E9:37": "Sonos", "00:1D:C9": "GainSpan", "AC:84:C6": "TP-Link",
}

TOP_PORTS = [
    21, 22, 23, 25, 53, 80, 110, 111, 135, 139, 143, 161, 389, 443, 445, 465,
    587, 631, 993, 995, 1080, 1433, 1521, 1723, 2049, 2082, 2375, 3000, 3128,
    3306, 3389, 4444, 5000, 5060, 5432, 5672, 5900, 5985, 6379, 6443, 7681,
    8000, 8008, 8080, 8086, 8088, 8443, 8888, 9000, 9090, 9200, 11211, 27017,
]

# Ports worth knocking on when deciding "is this host alive" without ICMP.
ALIVE_PORTS = [80, 443, 22, 445, 139, 3389, 53, 8080, 7681]


def is_private(host: str) -> bool:
    """True when the address is loopback, link-local or RFC1918/ULA."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def resolve(host: str) -> str:
    """Hostname -> IP. Raises ValueError with a readable message on failure."""
    host = (host or "").strip()
    if not host:
        raise ValueError("no host given")
    if len(host) > 255 or any(c.isspace() for c in host):
        raise ValueError("bad host")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        return socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)[0][4][0]
    except OSError as e:
        raise ValueError(f"cannot resolve {host}: {e.strerror or e}")


def rdns(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        return ""


def vendor(mac: str) -> str:
    return OUI.get((mac or "").upper()[:8], "")


def _run(args: list, timeout: int = 20, sudo: bool = False):
    """Run a command, returning (ok, stdout+stderr). Never raises."""
    if sudo:
        args = ["sudo", "-n"] + args
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return False, f"{args[0]}: not installed"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as e:                                    # noqa: BLE001
        return False, str(e)


def parse_ports(spec: str) -> list:
    """'22,80,8000-8010' -> [22, 80, 8000..8010]. Empty spec = the top-port list."""
    spec = (spec or "").strip().lower()
    if not spec or spec in ("top", "common", "default"):
        return list(TOP_PORTS)
    if spec in ("all", "*", "1-65535"):
        return list(range(1, 65536))
    out = []
    for chunk in re.split(r"[,\s]+", spec):
        if not chunk:
            continue
        m = re.fullmatch(r"(\d{1,5})(?:-(\d{1,5}))?", chunk)
        if not m:
            raise ValueError(f"bad port spec: {chunk}")
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if not (1 <= a <= 65535 and 1 <= b <= 65535) or b < a:
            raise ValueError(f"bad port range: {chunk}")
        out.extend(range(a, b + 1))
    if len(out) > 65535:
        raise ValueError("too many ports")
    return sorted(set(out))


def service_of(port: int) -> str:
    try:
        return socket.getservbyport(port)
    except OSError:
        return ""


# ------------------------------------------------------------- my own network

def interfaces() -> dict:
    """Local addressing: NICs, routes, gateway, DNS servers, hostname."""
    ifs = []
    ok, out = _run(["ip", "-j", "addr"])
    if ok:
        try:
            for e in json.loads(out):
                addrs = [{"family": a.get("family"), "ip": a.get("local"),
                          "prefix": a.get("prefixlen"), "scope": a.get("scope")}
                         for a in e.get("addr_info", [])]
                ifs.append({
                    "name": e.get("ifname"), "mac": (e.get("address") or "").upper(),
                    "vendor": vendor(e.get("address") or ""),
                    "mtu": e.get("mtu"), "state": e.get("operstate"),
                    "flags": e.get("flags", []), "addrs": addrs,
                })
        except (ValueError, TypeError):
            ok = False
    if not ok:                                   # no iproute2: fall back to /proc
        try:
            for idx, name in socket.if_nameindex():
                ifs.append({"name": name, "mac": "", "vendor": "", "mtu": None,
                            "state": "?", "flags": [], "addrs": []})
        except OSError:
            pass

    routes = []
    ok, out = _run(["ip", "-j", "route"])
    if ok:
        try:
            for r in json.loads(out):
                routes.append({"dst": r.get("dst"), "via": r.get("gateway", ""),
                               "dev": r.get("dev"), "proto": r.get("protocol", ""),
                               "metric": r.get("metric")})
        except (ValueError, TypeError):
            pass
    gw = next((r["via"] for r in routes if r["dst"] == "default" and r["via"]), "")

    dnsrv = []
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                if line.startswith("nameserver"):
                    dnsrv.append(line.split()[1])
    except OSError:
        pass

    primary = ""
    try:                                          # the address used to reach the world
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 53))
        primary = s.getsockname()[0]
        s.close()
    except OSError:
        pass

    nets = []
    for i in ifs:
        for a in i["addrs"]:
            if a["family"] == "inet" and not a["ip"].startswith("127."):
                nets.append(f"{a['ip']}/{a['prefix']}")
    return {"hostname": socket.gethostname(), "fqdn": socket.getfqdn(),
            "primary_ip": primary, "interfaces": ifs, "routes": routes,
            "gateway": gw, "dns": dnsrv, "networks": nets}


def public_ip() -> dict:
    """WAN address plus whatever geo/ASN the lookup service volunteers."""
    for url in ("https://ipinfo.io/json", "http://ip-api.com/json/"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "webterm"})
            with urllib.request.urlopen(req, timeout=6) as r:
                d = json.loads(r.read(8192).decode("utf-8", "replace"))
        except Exception:                                      # noqa: BLE001
            continue
        if "ip" in d:
            return {"ip": d.get("ip"), "city": d.get("city", ""),
                    "region": d.get("region", ""), "country": d.get("country", ""),
                    "org": d.get("org", ""), "loc": d.get("loc", ""),
                    "source": url}
        if d.get("status") == "success":
            return {"ip": d.get("query"), "city": d.get("city", ""),
                    "region": d.get("regionName", ""), "country": d.get("countryCode", ""),
                    "org": f"AS{d.get('as','')} {d.get('isp','')}".strip(),
                    "loc": f"{d.get('lat','')},{d.get('lon','')}", "source": url}
    return {"error": "no lookup service reachable (offline, or egress blocked)"}


# ------------------------------------------------------------------ ping / trace

RTT_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms")


def ping(host: str, count: int = 4, timeout: int = 2) -> dict:
    """ICMP ping via the system binary, with sudo and TCP fallbacks."""
    ip = resolve(host)
    count = max(1, min(int(count), 20))
    args = ["ping", "-n", "-c", str(count), "-W", str(timeout), "-i", "0.3", ip]
    ok, out = _run(args, timeout=count * (timeout + 1) + 5)
    used = "icmp"
    if not ok and ("Operation not permitted" in out or "socket" in out.lower()):
        ok, out = _run(args, timeout=count * (timeout + 1) + 5, sudo=True)
        used = "icmp (sudo)"
    if ok or RTT_RE.search(out):
        rtts = [float(x) for x in RTT_RE.findall(out)]
        loss = 100.0 - (len(rtts) * 100.0 / count)
        return {"host": host, "ip": ip, "method": used, "sent": count,
                "received": len(rtts), "loss": round(loss, 1),
                "rtts": rtts,
                "min": round(min(rtts), 3) if rtts else None,
                "avg": round(sum(rtts) / len(rtts), 3) if rtts else None,
                "max": round(max(rtts), 3) if rtts else None,
                "jitter": round(max(rtts) - min(rtts), 3) if len(rtts) > 1 else 0.0,
                "raw": out.strip()}
    return tcp_ping(ip, host, count)


def tcp_ping(ip: str, host: str, count: int = 4) -> dict:
    """When ICMP is unavailable: time a TCP handshake to a likely open port."""
    port = next((p for p in (443, 80, 22, 7681) if _connect_ms(ip, p) is not None), None)
    if port is None:
        return {"host": host, "ip": ip, "method": "tcp", "sent": count, "received": 0,
                "loss": 100.0, "rtts": [], "min": None, "avg": None, "max": None,
                "jitter": 0.0, "raw": "ICMP unavailable and no TCP port answered"}
    rtts = []
    for _ in range(count):
        ms = _connect_ms(ip, port)
        if ms is not None:
            rtts.append(round(ms, 3))
        time.sleep(0.15)
    loss = 100.0 - (len(rtts) * 100.0 / count)
    return {"host": host, "ip": ip, "method": f"tcp :{port}", "sent": count,
            "received": len(rtts), "loss": round(loss, 1), "rtts": rtts,
            "min": round(min(rtts), 3) if rtts else None,
            "avg": round(sum(rtts) / len(rtts), 3) if rtts else None,
            "max": round(max(rtts), 3) if rtts else None,
            "jitter": round(max(rtts) - min(rtts), 3) if len(rtts) > 1 else 0.0,
            "raw": "ICMP unavailable — timed TCP handshakes instead"}


def _connect_ms(ip: str, port: int, timeout: float = 1.5):
    s = socket.socket(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.perf_counter()
    try:
        s.connect((ip, port))
        return (time.perf_counter() - t0) * 1000
    except OSError:
        return None
    finally:
        s.close()


def _icmp_packet(ident: int, seq: int) -> bytes:
    head = struct.pack("!BBHHH", 8, 0, 0, ident, seq)
    payload = b"webterm-traceroute"
    csum, total = 0, head + payload
    if len(total) % 2:
        total += b"\x00"
    for i in range(0, len(total), 2):
        csum += (total[i] << 8) + total[i + 1]
    csum = (csum >> 16) + (csum & 0xFFFF)
    csum = ~(csum + (csum >> 16)) & 0xFFFF
    return struct.pack("!BBHHH", 8, 0, csum, ident, seq) + payload


def traceroute(host: str, max_hops: int = 20, timeout: float = 1.2) -> dict:
    """Raw-socket ICMP traceroute. Needs root, so the caller runs it under sudo."""
    ip = resolve(host)
    max_hops = max(1, min(int(max_hops), 40))
    hops, ident = [], os.getpid() & 0xFFFF
    try:
        rx = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        tx = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    except PermissionError:
        return {"error": "traceroute needs root (raw sockets) and sudo is unavailable",
                "host": host, "ip": ip, "hops": []}
    rx.settimeout(timeout)
    done = False
    with rx, tx:
        for ttl in range(1, max_hops + 1):
            tx.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
            t0 = time.perf_counter()
            try:
                tx.sendto(_icmp_packet(ident, ttl), (ip, 0))
            except OSError as e:
                hops.append({"ttl": ttl, "ip": "", "name": "", "ms": None, "err": str(e)})
                break
            frm, ms = "", None
            while True:
                try:
                    data, addr = rx.recvfrom(1024)
                except socket.timeout:
                    break
                kind = data[20]
                if kind == 11 or (kind == 0 and len(data) >= 28):    # exceeded or echo
                    frm = addr[0]
                    ms = round((time.perf_counter() - t0) * 1000, 3)
                    if kind == 0 or frm == ip:
                        done = True
                    break
            hops.append({"ttl": ttl, "ip": frm, "name": rdns(frm) if frm else "",
                         "ms": ms, "err": "" if frm else "*"})
            if done:
                break
    return {"host": host, "ip": ip, "hops": hops, "complete": done,
            "method": "icmp raw"}


# ------------------------------------------------------------------- discovery

def arp_table() -> dict:
    """Neighbours this machine has recently spoken to."""
    rows = []
    ok, out = _run(["ip", "-j", "neigh"])
    if ok:
        try:
            for n in json.loads(out):
                mac = (n.get("lladdr") or "").upper()
                rows.append({"ip": n.get("dst"), "mac": mac, "vendor": vendor(mac),
                             "dev": n.get("dev"), "state": " ".join(n.get("state", [])),
                             "name": ""})
        except (ValueError, TypeError):
            ok = False
    if not rows:
        try:
            with open("/proc/net/arp") as f:
                for line in f.read().splitlines()[1:]:
                    p = line.split()
                    if len(p) >= 6 and p[3] != "00:00:00:00:00:00":
                        rows.append({"ip": p[0], "mac": p[3].upper(),
                                     "vendor": vendor(p[3]), "dev": p[5],
                                     "state": p[2], "name": ""})
        except OSError:
            pass
    for r in rows:
        r["name"] = rdns(r["ip"])
    return {"neighbours": rows, "count": len(rows)}


async def _alive(ip: str, timeout: float) -> dict:
    """A host counts as up if any of a few common ports accepts a connection."""
    for port in ALIVE_PORTS:
        try:
            fut = asyncio.open_connection(ip, port)
            reader, writer = await asyncio.wait_for(fut, timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return {"ip": ip, "up": True, "via": f"tcp/{port}"}
        except (asyncio.TimeoutError, ConnectionRefusedError):
            if isinstance(getattr(asyncio, "_last", None), str):
                pass
            # refused still proves something is there
            continue
        except OSError:
            continue
    return {"ip": ip, "up": False, "via": ""}


def _ping_sweep(ips: list, timeout: int = 1) -> set:
    """One ping per host, in parallel, using whatever privilege we have."""
    up, sudo = set(), False
    probe = ["ping", "-n", "-c", "1", "-W", str(timeout)]
    ok, out = _run(probe + ["127.0.0.1"], timeout=5)
    if not ok:
        ok, out = _run(probe + ["127.0.0.1"], timeout=5, sudo=True)
        sudo = ok
        if not ok:
            return up
    procs = []
    for ip in ips:
        args = (["sudo", "-n"] if sudo else []) + probe + [ip]
        try:
            procs.append((ip, subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL)))
        except Exception:                                      # noqa: BLE001
            continue
        if len(procs) >= 256:
            break
    deadline = time.time() + timeout + 4
    for ip, p in procs:
        try:
            p.wait(max(0.1, deadline - time.time()))
            if p.returncode == 0:
                up.add(ip)
        except Exception:                                      # noqa: BLE001
            p.kill()
    return up


async def scan_hosts(cidr: str, timeout: float = 0.6) -> dict:
    """Find live hosts on a subnet: ARP cache + ping sweep + TCP knock."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError as e:
        raise ValueError(f"bad network: {e}")
    if net.num_addresses > 1024:
        raise ValueError(f"{cidr} has {net.num_addresses} addresses — /22 or smaller please")
    ips = [str(h) for h in (net.hosts() if net.num_addresses > 2 else net)]

    known = {n["ip"]: n for n in arp_table()["neighbours"]}
    loop = asyncio.get_running_loop()
    pinged = await loop.run_in_executor(None, _ping_sweep, ips)

    sem = asyncio.Semaphore(256)

    async def one(ip):
        async with sem:
            return await _alive(ip, timeout)

    results = await asyncio.gather(*[one(ip) for ip in ips])
    hosts = []
    for r in results:
        ip = r["ip"]
        up = r["up"] or ip in pinged or ip in known
        if not up:
            continue
        n = known.get(ip, {})
        hosts.append({"ip": ip, "mac": n.get("mac", ""), "vendor": n.get("vendor", ""),
                      "name": rdns(ip), "via": r["via"] or ("icmp" if ip in pinged else "arp"),
                      "state": n.get("state", "")})
    hosts.sort(key=lambda h: tuple(int(x) for x in h["ip"].split(".")) if "." in h["ip"] else ())
    return {"network": str(net), "scanned": len(ips), "hosts": hosts,
            "count": len(hosts), "icmp": bool(pinged)}


async def scan_ports(host: str, spec: str = "", timeout: float = 1.0,
                     concurrency: int = 400) -> dict:
    """Plain TCP connect scan — no raw sockets, no nmap, nothing to install."""
    ip = resolve(host)
    ports = parse_ports(spec)
    sem = asyncio.Semaphore(max(16, min(int(concurrency), 800)))
    open_ports, refused, filtered = [], 0, 0
    t0 = time.perf_counter()

    async def probe(port):
        nonlocal refused, filtered
        async with sem:
            t = time.perf_counter()
            try:
                fut = asyncio.open_connection(ip, port)
                reader, writer = await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                filtered += 1
                return
            except ConnectionRefusedError:
                refused += 1
                return
            except OSError:
                filtered += 1
                return
            ms = round((time.perf_counter() - t) * 1000, 2)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            open_ports.append({"port": port, "service": service_of(port), "ms": ms})

    await asyncio.gather(*[probe(p) for p in ports])
    open_ports.sort(key=lambda p: p["port"])
    return {"host": host, "ip": ip, "scanned": len(ports), "open": open_ports,
            "open_count": len(open_ports), "closed": refused, "filtered": filtered,
            "seconds": round(time.perf_counter() - t0, 2)}


def discover(seconds: float = 3.0) -> dict:
    """SSDP/UPnP multicast search, plus mDNS browse when python-zeroconf is present."""
    found, errors = [], []

    msg = ("M-SEARCH * HTTP/1.1\r\n"
           "HOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\n'
           "MX: 2\r\n"
           "ST: ssdp:all\r\n\r\n").encode()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.5)
        s.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + seconds
        seen = set()
        while time.time() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            txt = data.decode("utf-8", "replace")
            head = dict(re.findall(r"^([\w-]+):\s*(.*)$", txt, re.M | re.I))
            key = (addr[0], head.get("USN", head.get("usn", "")))
            if key in seen:
                continue
            seen.add(key)
            found.append({"proto": "ssdp", "ip": addr[0],
                          "name": head.get("SERVER", head.get("server", "")),
                          "type": head.get("ST", head.get("st", "")),
                          "info": head.get("LOCATION", head.get("location", ""))})
        s.close()
    except OSError as e:
        errors.append(f"ssdp: {e}")

    try:
        from zeroconf import Zeroconf, ServiceBrowser          # optional dependency
    except ImportError:
        errors.append("mdns: python-zeroconf not installed (pip install zeroconf)")
        return {"devices": found, "count": len(found), "errors": errors}

    try:
        zc = Zeroconf()
        hits = []

        class Listener:
            def add_service(self, zc_, type_, name):
                info = zc_.get_service_info(type_, name, timeout=1500)
                ips = []
                if info:
                    try:
                        ips = info.parsed_addresses()
                    except Exception:                          # noqa: BLE001
                        ips = []
                hits.append({"proto": "mdns", "ip": ips[0] if ips else "",
                             "name": name.split(".")[0], "type": type_,
                             "info": f"port {info.port}" if info and info.port else ""})

            def update_service(self, *a):
                pass

            def remove_service(self, *a):
                pass

        types = ["_http._tcp.local.", "_https._tcp.local.", "_ipp._tcp.local.",
                 "_printer._tcp.local.", "_smb._tcp.local.", "_ssh._tcp.local.",
                 "_googlecast._tcp.local.", "_airplay._tcp.local.",
                 "_raop._tcp.local.", "_device-info._tcp.local.",
                 "_workstation._tcp.local.", "_nfs._tcp.local."]
        browsers = [ServiceBrowser(zc, t, Listener()) for t in types]
        time.sleep(seconds)
        for b in browsers:
            b.cancel()
        zc.close()
        found.extend(hits)
    except Exception as e:                                     # noqa: BLE001
        errors.append(f"mdns: {e}")
    return {"devices": found, "count": len(found), "errors": errors}


# --------------------------------------------------------------------- lookups

def dns_lookup(name: str, rtype: str = "A", resolver: str = "") -> dict:
    """Query one record type. Uses dnspython when available, getaddrinfo otherwise."""
    name = (name or "").strip()
    rtype = (rtype or "A").upper()
    if not name:
        raise ValueError("no name given")
    if rtype == "PTR" or re.fullmatch(r"[\d.:a-fA-F]+", name):
        try:
            ipaddress.ip_address(name)
            host = rdns(name)
            return {"name": name, "type": "PTR", "resolver": resolver or "system",
                    "answers": [host] if host else [], "ms": 0}
        except ValueError:
            pass
    t0 = time.perf_counter()
    try:
        import dns.resolver                                    # optional dependency
    except ImportError:
        if rtype in ("A", "AAAA", "ANY"):
            fam = socket.AF_INET6 if rtype == "AAAA" else socket.AF_INET
            try:
                ans = sorted({a[4][0] for a in socket.getaddrinfo(name, None, fam)})
            except OSError as e:
                raise ValueError(f"{name}: {e.strerror or e}")
            return {"name": name, "type": rtype, "resolver": "system", "answers": ans,
                    "ms": round((time.perf_counter() - t0) * 1000, 1),
                    "note": "install dnspython for MX/TXT/NS/SOA"}
        raise ValueError(f"{rtype} lookups need dnspython (pip install dnspython)")

    r = dns.resolver.Resolver()
    if resolver:
        r.nameservers = [resolve(resolver)]
    r.lifetime = 6
    try:
        ans = r.resolve(name, rtype)
    except Exception as e:                                     # noqa: BLE001
        raise ValueError(f"{name} {rtype}: {type(e).__name__}")
    return {"name": name, "type": rtype,
            "resolver": (r.nameservers[0] if r.nameservers else "system"),
            "answers": [x.to_text() for x in ans],
            "ttl": ans.rrset.ttl if ans.rrset is not None else None,
            "ms": round((time.perf_counter() - t0) * 1000, 1)}


def banner(host: str, port: int, use_tls: bool = False, timeout: float = 4.0) -> dict:
    """Grab whatever a service says first, and the certificate if it speaks TLS."""
    ip = resolve(host)
    port = int(port)
    out = {"host": host, "ip": ip, "port": port, "tls": bool(use_tls),
           "banner": "", "cert": None, "error": ""}
    sock = socket.socket(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((ip, port))
    except OSError as e:
        sock.close()
        out["error"] = f"connect failed: {e.strerror or e}"
        return out

    try:
        if use_tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            server_name = host if not re.fullmatch(r"[\d.]+", host) else None
            sock = ctx.wrap_socket(sock, server_hostname=server_name)
            out["tls_version"] = sock.version()
            out["cipher"] = (sock.cipher() or ("", "", 0))[0]
            der = sock.getpeercert(binary_form=True)
            out["cert"] = _cert_info(der)
            if port in (443, 8443, 9443):
                sock.sendall(f"HEAD / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
        elif port in (80, 8080, 8000, 8888):
            sock.sendall(f"HEAD / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
        try:
            data = sock.recv(2048)
        except (socket.timeout, OSError):
            data = b""
        out["banner"] = data.decode("utf-8", "replace").strip()
    except ssl.SSLError as e:
        out["error"] = f"tls: {e}"
    except OSError as e:
        out["error"] = str(e)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return out


def _cert_info(der: bytes) -> dict:
    if not der:
        return {}
    try:
        from cryptography import x509                          # optional dependency
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        return {"note": "install cryptography for certificate details",
                "size": len(der)}
    try:
        c = x509.load_der_x509_certificate(der)
        try:
            sans = [str(g.value) for g in
                    c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value]
        except x509.ExtensionNotFound:
            sans = []
        not_after = getattr(c, "not_valid_after_utc", None) or c.not_valid_after
        not_before = getattr(c, "not_valid_before_utc", None) or c.not_valid_before
        days = (not_after.timestamp() - time.time()) / 86400
        return {
            "subject": c.subject.rfc4514_string(),
            "issuer": c.issuer.rfc4514_string(),
            "serial": format(c.serial_number, "x"),
            "not_before": not_before.isoformat(timespec="seconds"),
            "not_after": not_after.isoformat(timespec="seconds"),
            "days_left": round(days, 1),
            "expired": days < 0,
            "sans": sans[:40],
            "sig": c.signature_hash_algorithm.name if c.signature_hash_algorithm else "",
            "fingerprint": c.fingerprint(hashes.SHA256()).hex(),
        }
    except Exception as e:                                     # noqa: BLE001
        return {"error": f"cannot parse certificate: {e}"}


def http_check(url: str, max_redirects: int = 8, timeout: float = 8.0) -> dict:
    """Follow a URL hop by hop, timing each phase of the first request."""
    import http.client
    from urllib.parse import urlparse, urljoin

    if not re.match(r"^https?://", url or "", re.I):
        url = "http://" + (url or "").strip()
    chain, hops = [], 0
    t_all = time.perf_counter()
    while hops <= max_redirects:
        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            raise ValueError("only http and https")
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "https" else 80)
        t0 = time.perf_counter()
        ip = resolve(host)
        t_dns = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        if u.scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.connect()
            t_conn = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            conn.request("GET", u.path or "/" + ("?" + u.query if u.query else ""),
                         headers={"User-Agent": "webterm/1.0", "Accept": "*/*",
                                  "Connection": "close"})
            resp = conn.getresponse()
            t_ttfb = (time.perf_counter() - t0) * 1000
            body = resp.read(4096)
            headers = {k: v for k, v in resp.getheaders()}
            chain.append({"url": url, "ip": ip, "status": resp.status,
                          "reason": resp.reason, "dns_ms": round(t_dns, 1),
                          "connect_ms": round(t_conn, 1), "ttfb_ms": round(t_ttfb, 1),
                          "bytes": len(body), "server": headers.get("Server", ""),
                          "type": headers.get("Content-Type", ""),
                          "headers": headers})
            loc = headers.get("Location", "")
            conn.close()
        except Exception as e:                                 # noqa: BLE001
            chain.append({"url": url, "ip": ip, "status": 0, "reason": str(e),
                          "dns_ms": round(t_dns, 1), "connect_ms": None,
                          "ttfb_ms": None, "bytes": 0, "server": "", "type": "",
                          "headers": {}})
            break
        if resp.status in (301, 302, 303, 307, 308) and loc:
            url = urljoin(url, loc)
            hops += 1
            continue
        break
    return {"chain": chain, "hops": len(chain) - 1,
            "total_ms": round((time.perf_counter() - t_all) * 1000, 1),
            "final": chain[-1]["url"] if chain else url,
            "status": chain[-1]["status"] if chain else 0}


def whois_lookup(target: str) -> dict:
    """RDAP first (structured, no binary needed), whois port 43 as a fallback."""
    target = (target or "").strip().lower()
    if not target:
        raise ValueError("no target given")
    kind = "ip" if re.fullmatch(r"[\d.]+|[\da-f:]+", target) else "domain"
    url = f"https://rdap.org/{'ip' if kind == 'ip' else 'domain'}/{target}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "webterm",
                                                   "Accept": "application/rdap+json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read(200000).decode("utf-8", "replace"))
        out = {"target": target, "source": "rdap", "name": d.get("name", ""),
               "handle": d.get("handle", ""), "type": d.get("type", ""),
               "country": d.get("country", ""),
               "start": d.get("startAddress", ""), "end": d.get("endAddress", ""),
               "status": d.get("status", []), "events": [], "contacts": []}
        for e in d.get("events", []):
            out["events"].append(f"{e.get('eventAction')}: {e.get('eventDate','')[:10]}")
        for ent in d.get("entities", [])[:6]:
            roles = ",".join(ent.get("roles", []))
            label = ent.get("handle", "")
            for v in (ent.get("vcardArray") or [None, []])[1]:
                if v and v[0] in ("fn", "email"):
                    label = f"{label} {v[3]}".strip()
            out["contacts"].append(f"{roles}: {label}".strip(": "))
        if kind == "domain":
            out["nameservers"] = [n.get("ldhName", "") for n in d.get("nameservers", [])]
        return out
    except Exception as e:                                     # noqa: BLE001
        rdap_err = str(e)

    if shutil.which("whois"):
        ok, out = _run(["whois", target], timeout=15)
        if ok:
            return {"target": target, "source": "whois:43", "raw": out[:8000]}
    try:                                                        # last resort: raw port 43
        s = socket.create_connection(("whois.iana.org", 43), timeout=6)
        s.sendall(target.encode() + b"\r\n")
        buf = b""
        while len(buf) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        return {"target": target, "source": "whois.iana.org:43",
                "raw": buf.decode("utf-8", "replace")[:8000]}
    except OSError as e:
        return {"target": target, "error": f"rdap: {rdap_err} / whois: {e}"}


# ------------------------------------------------------------------- bandwidth

_BW_PREV = {}


def bandwidth() -> dict:
    """Per-interface rx/tx rates, measured against the previous call."""
    now = time.time()
    rows = []
    try:
        with open("/proc/net/dev") as f:
            lines = f.read().splitlines()[2:]
    except OSError:
        return {"interfaces": [], "now": now}
    for line in lines:
        name, _, rest = line.partition(":")
        name = name.strip()
        f = rest.split()
        if len(f) < 16:
            continue
        rx, tx = int(f[0]), int(f[8])
        rxp, txp = int(f[1]), int(f[9])
        prev = _BW_PREV.get(name)
        dt = now - prev[0] if prev else 0
        rate_rx = (rx - prev[1]) / dt if prev and dt > 0.2 else 0.0
        rate_tx = (tx - prev[2]) / dt if prev and dt > 0.2 else 0.0
        _BW_PREV[name] = (now, rx, tx)
        rows.append({"name": name, "rx": rx, "tx": tx, "rx_pkts": rxp, "tx_pkts": txp,
                     "rx_rate": max(0.0, rate_rx), "tx_rate": max(0.0, rate_tx),
                     "errs": int(f[2]) + int(f[10]), "drop": int(f[3]) + int(f[11])})
    rows.sort(key=lambda r: r["rx"] + r["tx"], reverse=True)
    return {"interfaces": rows, "now": now}


# ---------------------------------------------------------------- service views
#
# One function per protocol, each returning a plain dict the UI can render.
# They never raise for network conditions — the error travels inside the result.

def _tls_wrap(sock, host):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sni = None if re.fullmatch(r"[\d.]+", host) else host
    return ctx.wrap_socket(sock, server_hostname=sni)


def ftp_list(host: str, port: int = 21, user: str = "anonymous",
             password: str = "anonymous@", path: str = "", tls: bool = False) -> dict:
    """Log in and list one directory. MLSD when the server has it, LIST otherwise."""
    from ftplib import FTP, FTP_TLS, error_perm, all_errors

    ip = resolve(host)
    out = {"host": host, "ip": ip, "port": int(port), "user": user, "path": path or "/",
           "welcome": "", "features": [], "entries": [], "error": "", "tls": bool(tls)}
    ftp = FTP_TLS() if tls else FTP()
    try:
        ftp.connect(ip, int(port), timeout=10)
        out["welcome"] = (ftp.getwelcome() or "").strip()
        if tls:
            ftp.auth()
            ftp.prot_p()
        ftp.login(user or "anonymous", password or "anonymous@")
        try:
            out["features"] = [l.strip() for l in ftp.sendcmd("FEAT").splitlines()[1:-1]]
        except all_errors:
            pass
        if path:
            ftp.cwd(path)
        out["path"] = ftp.pwd()

        entries = []
        try:                                        # MLSD gives typed, parseable output
            for name, facts in ftp.mlsd():
                if name in (".", ".."):
                    continue
                entries.append({"name": name, "dir": facts.get("type") == "dir",
                                "size": int(facts.get("size", 0) or 0),
                                "modify": facts.get("modify", ""),
                                "perm": facts.get("perm", "")})
        except all_errors:
            lines = []
            ftp.retrlines("LIST", lines.append)
            for line in lines:
                p = line.split(maxsplit=8)
                if len(p) < 9 or line.startswith("total"):
                    continue
                name = p[8]
                if name in (".", ".."):
                    continue
                entries.append({"name": name, "dir": line[0] == "d",
                                "size": int(p[4]) if p[4].isdigit() else 0,
                                "modify": " ".join(p[5:8]), "perm": p[0]})
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        out["entries"] = entries
    except error_perm as e:
        out["error"] = f"server refused: {e}"
    except all_errors as e:
        out["error"] = f"{type(e).__name__}: {e}"
    except OSError as e:
        out["error"] = str(e)
    finally:
        try:
            ftp.quit()
        except Exception:                                      # noqa: BLE001
            try:
                ftp.close()
            except Exception:                                  # noqa: BLE001
                pass
    return out


def ftp_fetch(host: str, port: int, user: str, password: str, path: str,
              limit: int = 25 * 1024 * 1024) -> bytes:
    """Download one file over FTP, refusing anything beyond `limit`."""
    from ftplib import FTP, all_errors

    ftp = FTP()
    chunks, total = [], 0
    try:
        ftp.connect(resolve(host), int(port), timeout=10)
        ftp.login(user or "anonymous", password or "anonymous@")

        def sink(data):
            nonlocal total
            total += len(data)
            if total > limit:
                raise ValueError("file too large")
            chunks.append(data)

        ftp.retrbinary(f"RETR {path}", sink)
    except all_errors as e:
        raise ValueError(str(e))
    finally:
        try:
            ftp.close()
        except Exception:                                      # noqa: BLE001
            pass
    return b"".join(chunks)


def _ssh_kexinit(ip: str, port: int, timeout: float = 6.0) -> dict:
    """Read the server's banner and KEXINIT without completing a handshake."""
    out = {"banner": "", "kex": [], "hostkey": [], "cipher": [], "mac": [], "compress": []}
    s = socket.create_connection((ip, port), timeout=timeout)
    try:
        s.settimeout(timeout)
        buf = b""
        while b"\r\n" not in buf and len(buf) < 1024:
            chunk = s.recv(256)
            if not chunk:
                break
            buf += chunk
        out["banner"] = buf.split(b"\r\n")[0].decode("utf-8", "replace")
        s.sendall(b"SSH-2.0-webterm_probe\r\n")

        hdr = s.recv(4)
        if len(hdr) < 4:
            return out
        plen = struct.unpack("!I", hdr)[0]
        if not (8 <= plen <= 65536):
            return out
        body = b""
        while len(body) < plen:
            chunk = s.recv(plen - len(body))
            if not chunk:
                break
            body += chunk
        pad = body[0]
        payload = body[1:len(body) - pad]
        if not payload or payload[0] != 20:              # 20 = SSH_MSG_KEXINIT
            return out
        pos = 17                                         # msg byte + 16-byte cookie
        names = []
        for _ in range(10):
            if pos + 4 > len(payload):
                break
            ln = struct.unpack("!I", payload[pos:pos + 4])[0]
            pos += 4
            names.append(payload[pos:pos + ln].decode("utf-8", "replace").split(","))
            pos += ln
        keys = ["kex", "hostkey", "cipher_cs", "cipher_sc", "mac_cs", "mac_sc",
                "compress_cs", "compress_sc"]
        got = dict(zip(keys, names))
        out["kex"] = got.get("kex", [])
        out["hostkey"] = got.get("hostkey", [])
        out["cipher"] = got.get("cipher_sc", [])
        out["mac"] = got.get("mac_sc", [])
        out["compress"] = got.get("compress_sc", [])
    except OSError:
        pass
    finally:
        s.close()
    return out


def ssh_probe(host: str, port: int = 22, user: str = "root") -> dict:
    """Server banner, offered algorithms, host key fingerprint, auth methods."""
    import base64
    import hashlib

    ip = resolve(host)
    out = {"host": host, "ip": ip, "port": int(port), "error": ""}
    out.update(_ssh_kexinit(ip, int(port)))
    if not out["banner"]:
        out["error"] = "no SSH banner — is this really an SSH server?"
        return out
    try:
        import paramiko                                        # optional dependency
    except ImportError:
        out["note"] = "install paramiko for host key and auth methods"
        return out
    try:
        t = paramiko.Transport((ip, int(port)))
        t.banner_timeout = 8
        t.start_client(timeout=8)
        key = t.get_remote_server_key()
        raw = key.asbytes()
        out["key_type"] = key.get_name()
        out["key_bits"] = key.get_bits()
        out["fingerprint_sha256"] = "SHA256:" + base64.b64encode(
            hashlib.sha256(raw).digest()).decode().rstrip("=")
        out["fingerprint_md5"] = "MD5:" + ":".join(
            f"{b:02x}" for b in hashlib.md5(raw).digest())
        try:
            t.auth_none(user or "root")
            out["auth"] = ["none (server accepted an unauthenticated login!)"]
        except paramiko.BadAuthenticationType as e:
            out["auth"] = list(e.allowed_types)
        except paramiko.AuthenticationException:
            out["auth"] = ["password/publickey (server did not say)"]
        t.close()
    except Exception as e:                                     # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def smtp_probe(host: str, port: int = 25, starttls: bool = True) -> dict:
    """Greeting, EHLO capabilities, STARTTLS and the certificate behind it."""
    import smtplib

    ip = resolve(host)
    port = int(port)
    out = {"host": host, "ip": ip, "port": port, "banner": "", "features": [],
           "starttls": False, "esmtp": False, "auth": [], "size_limit": None,
           "cert": None, "error": ""}
    try:
        if port in (465, 8465):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            srv = smtplib.SMTP_SSL(ip, port, timeout=10, context=ctx)
            out["starttls"] = True
            out["cert"] = _cert_info(srv.sock.getpeercert(binary_form=True))
        else:
            srv = smtplib.SMTP(ip, port, timeout=10)
        out["banner"] = (srv.sock.recv(0) if False else "") or getattr(srv, "_host", "")
        code, msg = srv.ehlo("webterm.local")
        out["esmtp"] = code == 250
        if code != 250:
            code, msg = srv.helo("webterm.local")
        out["banner"] = msg.decode("utf-8", "replace").splitlines()[0] if isinstance(msg, bytes) else str(msg)
        out["features"] = sorted(srv.esmtp_features.keys())
        out["auth"] = (srv.esmtp_features.get("auth", "") or "").split()
        if srv.esmtp_features.get("size"):
            out["size_limit"] = int(srv.esmtp_features["size"])
        if starttls and srv.has_extn("starttls") and not out["starttls"]:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            srv.starttls(context=ctx)
            out["starttls"] = True
            out["cert"] = _cert_info(srv.sock.getpeercert(binary_form=True))
            srv.ehlo("webterm.local")
            out["features"] = sorted(srv.esmtp_features.keys())
            out["auth"] = (srv.esmtp_features.get("auth", "") or "").split()
        try:
            srv.quit()
        except Exception:                                      # noqa: BLE001
            srv.close()
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ------------------------------------------------------------------ NFS / RPC

def _rpc_call(ip: str, port: int, prog: int, vers: int, proc: int,
              args: bytes = b"", timeout: float = 6.0) -> bytes:
    """One ONC-RPC call over TCP. Returns the reply body after the accept header."""
    xid = int.from_bytes(os.urandom(4), "big")
    body = struct.pack("!IIIIII", xid, 0, 2, prog, vers, proc)
    body += struct.pack("!IIII", 0, 0, 0, 0)          # null cred + null verf
    body += args
    frag = struct.pack("!I", 0x80000000 | len(body)) + body

    s = socket.create_connection((ip, port), timeout=timeout)
    try:
        s.settimeout(timeout)
        s.sendall(frag)
        data = b""
        while True:                                   # reassemble record fragments
            head = b""
            while len(head) < 4:
                c = s.recv(4 - len(head))
                if not c:
                    raise OSError("connection closed")
                head += c
            mark = struct.unpack("!I", head)[0]
            ln, last = mark & 0x7FFFFFFF, mark & 0x80000000
            part = b""
            while len(part) < ln:
                c = s.recv(min(65536, ln - len(part)))
                if not c:
                    raise OSError("short read")
                part += c
            data += part
            if last:
                break
    finally:
        s.close()

    if len(data) < 24:
        raise OSError("short RPC reply")
    r_xid, mtype, stat = struct.unpack("!III", data[:12])
    if r_xid != xid or mtype != 1:
        raise OSError("RPC reply mismatch")
    if stat != 0:
        raise OSError(f"RPC denied (stat {stat})")
    vflavor, vlen = struct.unpack("!II", data[12:20])
    pos = 20 + vlen
    accept = struct.unpack("!I", data[pos:pos + 4])[0]
    if accept != 0:
        raise OSError(f"RPC not accepted (accept_stat {accept})")
    return data[pos + 4:]


def _xdr_string(buf: bytes, pos: int):
    ln = struct.unpack("!I", buf[pos:pos + 4])[0]
    pos += 4
    val = buf[pos:pos + ln].decode("utf-8", "replace")
    pos += ln + ((4 - ln % 4) % 4)
    return val, pos


RPC_NAMES = {100000: "portmapper", 100003: "nfs", 100005: "mountd", 100021: "nlockmgr",
             100024: "status", 100227: "nfs_acl", 391002: "sgi_fam"}


def nfs_exports(host: str, port: int = 111) -> dict:
    """Ask portmap what it runs, then ask mountd for its export list."""
    ip = resolve(host)
    out = {"host": host, "ip": ip, "programs": [], "exports": [], "error": "",
           "source": "rpc"}
    mount_port = 0
    try:
        body = _rpc_call(ip, int(port), 100000, 2, 4)          # PMAPPROC_DUMP
        pos, seen = 0, set()
        while pos + 4 <= len(body):
            more = struct.unpack("!I", body[pos:pos + 4])[0]
            pos += 4
            if not more or pos + 16 > len(body):
                break
            prog, vers, prot, pport = struct.unpack("!IIII", body[pos:pos + 16])
            pos += 16
            key = (prog, vers, prot, pport)
            if key in seen:
                continue
            seen.add(key)
            out["programs"].append({
                "program": prog, "name": RPC_NAMES.get(prog, str(prog)),
                "version": vers, "proto": "tcp" if prot == 6 else "udp",
                "port": pport})
            if prog == 100005 and prot == 6 and pport:
                mount_port = pport
    except OSError as e:
        out["error"] = f"portmap: {e}"

    for mp, mv in [(mount_port, 3), (mount_port, 1), (int(port), 3)]:
        if not mp:
            continue
        try:
            body = _rpc_call(ip, mp, 100005, mv, 5)            # MOUNTPROC_EXPORT
            pos = 0
            while pos + 4 <= len(body):
                more = struct.unpack("!I", body[pos:pos + 4])[0]
                pos += 4
                if not more:
                    break
                path, pos = _xdr_string(body, pos)
                groups = []
                while pos + 4 <= len(body):
                    g_more = struct.unpack("!I", body[pos:pos + 4])[0]
                    pos += 4
                    if not g_more:
                        break
                    g, pos = _xdr_string(body, pos)
                    groups.append(g)
                out["exports"].append({"path": path, "clients": groups or ["*"]})
            if out["exports"]:
                out["error"] = ""
                return out
        except OSError as e:
            out["error"] = f"mountd: {e}"

    if not out["exports"] and shutil.which("showmount"):
        ok, txt = _run(["showmount", "-e", ip], timeout=12)
        if ok:
            out["source"] = "showmount"
            out["error"] = ""
            for line in txt.splitlines()[1:]:
                p = line.split(maxsplit=1)
                if p:
                    out["exports"].append({"path": p[0],
                                           "clients": (p[1] if len(p) > 1 else "*").split(",")})
    if out["programs"] and not out["exports"] and not mount_port:
        out["error"] = ("portmap answered but no mountd is registered — "
                        "this host is not exporting NFS shares")
    if not out["programs"] and not out["exports"] and not out["error"]:
        out["error"] = "no RPC services answered on port 111"
    return out
