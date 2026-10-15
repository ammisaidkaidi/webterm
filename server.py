#!/usr/bin/env python3
"""
webterm - a minimal web terminal.

Serves a single page that gives you a real interactive shell in the browser.
No login, no accounts, no database. One file, one page.

    python3 server.py [--host 0.0.0.0] [--port 7681] [--shell /bin/bash]

WARNING: this gives anyone who can reach the port a shell as the user running
this process. Bind to 127.0.0.1 unless you know what you are doing, or set
WEBTERM_TOKEN=secret and open  http://host:port/?token=secret
"""

import argparse
import asyncio
import fcntl
import grp
import json
import os
import pty
import pwd
import shutil
import signal
import stat as statmod
import struct
import subprocess
import sys
import tempfile
import termios
import time
import uuid
import zipfile
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

import netscan            # network discovery helpers (local module)
from starlette.background import BackgroundTask

BASE = Path(__file__).parent
STATIC = BASE / "static"
TOKEN = os.environ.get("WEBTERM_TOKEN", "")
SHELL = os.environ.get("WEBTERM_SHELL", "")

app = FastAPI(title="webterm")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


def pick_shell() -> str:
    for candidate in (SHELL, os.environ.get("SHELL"), "/bin/bash", "/bin/sh"):
        if candidate and os.path.exists(candidate):
            return candidate
    return "/bin/sh"


def set_winsize(fd: int, rows: int, cols: int) -> None:
    rows = max(1, min(int(rows), 500))
    cols = max(1, min(int(cols), 500))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class Session:
    """One forked shell attached to a pseudo-terminal."""

    def __init__(self, rows: int = 24, cols: int = 80):
        self.sid = uuid.uuid4().hex[:12]
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child
            shell = pick_shell()
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env["COLORTERM"] = "truecolor"
            env.setdefault("LANG", "C.UTF-8")
            env.pop("WEBTERM_TOKEN", None)
            try:
                os.execvpe(shell, [shell, "-i"], env)
            except Exception:
                os._exit(1)
        # parent
        os.set_blocking(self.fd, False)
        set_winsize(self.fd, rows, cols)

    def write(self, data: str) -> None:
        os.write(self.fd, data.encode("utf-8", "ignore"))

    def leaf(self) -> int:
        """Deepest descendant of the shell: follows `su`, `sudo -i`, nested shells."""
        pid = self.pid
        for _ in range(16):
            try:
                with open(f"/proc/{pid}/task/{pid}/children") as f:
                    kids = f.read().split()
            except OSError:
                break
            if not kids:
                break
            pid = int(kids[-1])
        return pid

    def cwd(self) -> str:
        """Where the shell currently is, so the file browser can follow it."""
        pid = self.leaf()
        for target in (pid, self.pid):
            try:
                return os.readlink(f"/proc/{target}/cwd")          # Linux
            except PermissionError:
                # the leaf belongs to root (sudo -i); ask sudo for the link
                ok, out = run_sudo(["readlink", f"/proc/{target}/cwd"])
                if ok and out.startswith("/"):
                    return out
            except OSError:
                pass
        return os.path.expanduser("~")

    def user(self) -> str:
        """Who the shell is running as — follows su / sudo -i into child shells."""
        try:
            with open(f"/proc/{self.leaf()}/status") as f:
                for line in f:
                    if line.startswith("Uid:"):
                        uid = int(line.split()[1])          # real uid
                        return pwd.getpwuid(uid).pw_name
        except (OSError, KeyError, ValueError, IndexError):
            pass
        try:
            return pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            return str(os.getuid())

    def resize(self, rows: int, cols: int) -> None:
        set_winsize(self.fd, rows, cols)

    def close(self) -> None:
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(self.pid), sig)
            except Exception:
                pass
        try:
            os.close(self.fd)
        except Exception:
            pass
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except Exception:
            pass


EDIT_MAX = 4 * 1024 * 1024   # editor refuses anything larger
SESSIONS: dict = {}          # sid -> Session, so /api/ls can follow the shell's cwd
_SUDO = None                 # None = not probed yet


def sudo_available() -> bool:
    """True when `sudo -n` works without a password and the operator hasn't opted out.

    The terminal this app serves can already run `sudo -n` itself, so this grants the
    HTTP API nothing the shell does not already have. Set WEBTERM_SUDO=0 to disable.
    """
    global _SUDO
    if os.environ.get("WEBTERM_SUDO", "1") == "0":
        return False
    if _SUDO is None:
        try:
            _SUDO = bool(shutil.which("sudo")) and subprocess.run(
                ["sudo", "-n", "true"], capture_output=True, timeout=5
            ).returncode == 0
        except Exception:
            _SUDO = False
    return _SUDO


def run_sudo(args: list) -> tuple:
    """Run one command via sudo -n. Returns (ok, message)."""
    if not sudo_available():
        return False, "sudo not available"
    try:
        p = subprocess.run(["sudo", "-n"] + args, capture_output=True, timeout=30, text=True)
    except Exception as e:
        return False, str(e)
    return p.returncode == 0, (p.stderr or p.stdout or "").strip()


def sudo_helper(op: str, path: str, hidden: int = 0, deep: int = 0):
    """Re-run one read-only operation as root via `sudo -n python3 server.py --helper`.

    Same code, same output shape — the only difference is the uid it runs under.
    Returns the parsed dict, or None when sudo is unavailable or the call fails.
    """
    if not sudo_available():
        return None
    args = ["sudo", "-n", sys.executable, os.path.abspath(__file__),
            "--helper", op, "--path", path, "--hidden", str(hidden), "--deep", str(deep)]
    try:
        r = subprocess.run(args, capture_output=True, timeout=60, text=True)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def owner_hint(path: str) -> str:
    """Explain *why* an operation was refused: who owns it vs who we are."""
    try:
        st = os.lstat(path)
        u, _ = _owner(st.st_uid, st.st_gid)
    except OSError:
        return ""
    me = pwd.getpwuid(os.geteuid()).pw_name
    if u == me:
        return ""
    return f" (owned by {u}, server runs as {me}; use the 'run in terminal' button)"


def check_token(token: str) -> None:
    if TOKEN and token != TOKEN:
        raise HTTPException(status_code=401, detail="bad token")


def resolve(path: str, sid: str) -> str:
    """Absolute path for a request; empty path means 'wherever the shell is'."""
    if not path:
        s = SESSIONS.get(sid)
        return s.cwd() if s else os.path.expanduser("~")
    return os.path.realpath(os.path.expanduser(path))


def do_ls(target: str, hidden: int = 0) -> dict:
    """List a directory. Raises OSError (PermissionError included) for the caller."""
    entries, truncated = [], False
    with os.scandir(target) as it:
        for e in it:
            if not hidden and e.name.startswith("."):
                continue
            if len(entries) >= 2000:
                truncated = True
                break
            try:
                is_dir = e.is_dir()
                st = e.stat(follow_symlinks=False)
                size, mtime, mode = st.st_size, st.st_mtime, st.st_mode
            except OSError:
                is_dir, size, mtime, mode = False, 0, 0, 0
            entries.append({
                "name": e.name,
                "dir": is_dir,
                "link": e.is_symlink(),
                "exe": bool(mode & 0o111) and not is_dir,
                "size": size,
                "mtime": mtime,
            })
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {
        "path": target,
        "parent": None if target == "/" else os.path.dirname(target) or "/",
        "home": os.path.expanduser("~"),
        "entries": entries,
        "truncated": truncated,
        "now": time.time(),
    }


@app.get("/api/ls")
async def api_ls(path: str = "", sid: str = "", token: str = "", hidden: int = 0):
    """List a directory. No sandbox: this is a shell, the files are already reachable."""
    check_token(token)
    target = resolve(path, sid)
    try:
        if not os.path.isdir(target):
            raise HTTPException(status_code=404, detail="not a directory")
        return do_ls(target, hidden)
    except PermissionError:
        out = sudo_helper("ls", target, hidden=hidden)       # retry as root
        if out is not None:
            out["sudo"] = True
            return out
        raise HTTPException(status_code=403, detail="permission denied" + owner_hint(target))


_CPU_PREV: dict = {}          # pid -> (total ticks, wall clock) for %CPU between polls


def _proc_list() -> dict:
    """Snapshot of /proc. %CPU is measured against the previous call, like top."""
    hz = os.sysconf("SC_CLK_TCK")
    page = os.sysconf("SC_PAGE_SIZE")
    now = time.time()
    procs, seen = [], set()

    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        pid_i = int(pid)
        seen.add(pid_i)
        try:
            with open(f"/proc/{pid}/stat") as f:
                raw = f.read()
            # comm can contain spaces and parens: split around the last ')'
            close = raw.rindex(")")
            comm = raw[raw.index("(") + 1:close]
            rest = raw[close + 2:].split()
            state = rest[0]
            ppid = int(rest[1])
            utime, stime = int(rest[11]), int(rest[12])
            threads = int(rest[17])
            starttime = int(rest[19])
            rss_pages = int(rest[21])   # field 24 = rss; field 25 is rsslim
            st = os.stat(f"/proc/{pid}")
            user, _ = _owner(st.st_uid, st.st_gid)
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            except OSError:
                cmd = ""
        except (OSError, ValueError, IndexError):
            continue

        ticks = utime + stime
        prev = _CPU_PREV.get(pid_i)
        _CPU_PREV[pid_i] = (ticks, now)
        if prev and now > prev[1]:
            cpu = max(0.0, (ticks - prev[0]) / hz / (now - prev[1]) * 100.0)
        else:
            cpu = 0.0

        procs.append({
            "pid": pid_i,
            "ppid": ppid,
            "user": user,
            "state": state,
            "cpu": round(cpu, 1),
            "rss": rss_pages * page,
            "threads": threads,
            "started": starttime / hz,
            "name": comm,
            "cmd": cmd or f"[{comm}]",
        })

    for dead in set(_CPU_PREV) - seen:
        _CPU_PREV.pop(dead, None)

    mem = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.strip().split()[0]) * 1024
    except OSError:
        pass

    try:
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
    except OSError:
        uptime = 0.0

    procs.sort(key=lambda x: -x["cpu"])
    return {
        "procs": procs,
        "count": len(procs),
        "load": list(os.getloadavg()),
        "ncpu": os.cpu_count() or 1,
        "uptime": uptime,
        "mem_total": mem.get("MemTotal", 0),
        "mem_avail": mem.get("MemAvailable", 0),
        "me": pwd.getpwuid(os.geteuid()).pw_name,
        "now": now,
    }


TCP_STATES = {
    "01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1",
    "05": "FIN_WAIT2", "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT",
    "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING", "0C": "NEW_SYN_RECV",
}

WELL_KNOWN = {
    20: "ftp-data", 21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 53: "dns",
    67: "dhcp", 69: "tftp", 80: "http", 110: "pop3", 123: "ntp", 143: "imap",
    389: "ldap", 443: "https", 445: "smb", 465: "smtps", 514: "syslog",
    587: "smtp", 631: "ipp", 993: "imaps", 995: "pop3s", 1080: "socks",
    1194: "openvpn", 1433: "mssql", 3306: "mysql", 3389: "rdp", 5432: "postgres",
    5672: "amqp", 6379: "redis", 8080: "http-alt", 8443: "https-alt",
    9000: "http-dev", 9090: "http-dev", 11211: "memcached", 27017: "mongodb",
}


def _hexaddr(h: str):
    """'0100007F:1F90' -> ('127.0.0.1', 8080). Handles IPv4 and IPv6."""
    addr, _, port = h.partition(":")
    port = int(port, 16)
    if len(addr) == 8:                                   # IPv4, little-endian
        b = bytes.fromhex(addr)[::-1]
        return ".".join(str(x) for x in b), port
    # IPv6: four little-endian 32-bit words
    words = [addr[i:i + 8] for i in range(0, 32, 8)]
    raw = b"".join(bytes.fromhex(w)[::-1] for w in words)
    import socket as _s
    try:
        ip = _s.inet_ntop(_s.AF_INET6, raw)
    except OSError:
        ip = addr
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    return ip, port


def _socket_owners() -> dict:
    """inode -> (pid, process name), for whatever /proc lets us read."""
    owners = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            names = os.listdir(fd_dir)
        except OSError:
            continue
        pname = ""
        for fd in names:
            try:
                link = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if link.startswith("socket:["):
                if not pname:
                    try:
                        with open(f"/proc/{pid}/comm") as f:
                            pname = f.read().strip()
                    except OSError:
                        pname = "?"
                owners[link[8:-1]] = (int(pid), pname)
    return owners


def do_net() -> dict:
    """Open sockets from /proc/net, joined to the processes that own them."""
    owners = _socket_owners()
    conns = []
    sources = [
        ("tcp", "/proc/net/tcp"), ("tcp6", "/proc/net/tcp6"),
        ("udp", "/proc/net/udp"), ("udp6", "/proc/net/udp6"),
    ]
    for proto, path in sources:
        try:
            with open(path) as f:
                lines = f.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            try:
                lip, lport = _hexaddr(parts[1])
                rip, rport = _hexaddr(parts[2])
                state = TCP_STATES.get(parts[3], parts[3]) if proto.startswith("tcp") else "—"
                uid = int(parts[7])
                inode = parts[9]
            except (ValueError, IndexError):
                continue
            pid, pname = owners.get(inode, (0, ""))
            user, _ = _owner(uid, uid)
            svc = WELL_KNOWN.get(lport) or WELL_KNOWN.get(rport) or ""
            conns.append({
                "proto": proto,
                "local": lip, "lport": lport,
                "remote": rip, "rport": rport,
                "state": state,
                "user": user,
                "pid": pid,
                "pname": pname,
                "service": svc,
                "listen": state == "LISTEN" or (proto.startswith("udp") and rport == 0),
            })
    conns.sort(key=lambda c: (not c["listen"], c["proto"], c["lport"]))
    return {"conns": conns, "count": len(conns), "now": time.time(),
            "root": os.geteuid() == 0}


NOLOGIN = ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false", "")


def check_target(target: str, confirm: int) -> None:
    """Public targets need one explicit confirmation from the browser."""
    host = (target or "").split("/")[0].strip()
    try:
        probe = netscan.resolve(host)
    except ValueError:
        probe = host
    if not netscan.is_private(probe) and not confirm:
        raise HTTPException(428, f"{host} is outside your private network — confirm first")


@app.get("/api/net/self")
async def api_net_self(token: str = ""):
    check_token(token)
    return netscan.interfaces()


@app.get("/api/net/public")
async def api_net_public(token: str = ""):
    check_token(token)
    return netscan.public_ip()


@app.get("/api/net/arp")
async def api_net_arp(token: str = ""):
    check_token(token)
    return netscan.arp_table()


@app.get("/api/net/bw")
async def api_net_bw(token: str = ""):
    check_token(token)
    return netscan.bandwidth()


@app.get("/api/net/discover")
async def api_net_discover(seconds: float = 3.0, token: str = ""):
    check_token(token)
    return await asyncio.to_thread(netscan.discover, min(max(seconds, 1.0), 10.0))


@app.post("/api/net/ping")
async def api_net_ping(req: Request):
    body = await req.json()
    check_token(body.get("token", ""))
    check_target(body.get("host", ""), body.get("confirm", 0))
    try:
        return await asyncio.to_thread(netscan.ping, body.get("host", ""),
                                       int(body.get("count", 4)))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/net/trace")
async def api_net_trace(req: Request):
    body = await req.json()
    check_token(body.get("token", ""))
    host = body.get("host", "")
    hops = int(body.get("max_hops", 20))
    check_target(host, body.get("confirm", 0))
    try:
        netscan.resolve(host)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if os.geteuid() != 0:                     # raw sockets: re-run this file as root
        out = sudo_helper("trace", host, deep=hops)
        if out is not None:
            out["sudo"] = True
            return out
    return await asyncio.to_thread(netscan.traceroute, host, hops)


@app.post("/api/net/hosts")
async def api_net_hosts(req: Request):
    body = await req.json()
    check_token(body.get("token", ""))
    cidr = body.get("cidr", "")
    check_target(cidr, body.get("confirm", 0))
    try:
        return await netscan.scan_hosts(cidr)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/net/ports")
async def api_net_ports(req: Request):
    body = await req.json()
    check_token(body.get("token", ""))
    check_target(body.get("host", ""), body.get("confirm", 0))
    try:
        return await netscan.scan_ports(body.get("host", ""), body.get("ports", ""),
                                        float(body.get("timeout", 1.0)))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/net/dns")
async def api_net_dns(req: Request):
    body = await req.json()
    check_token(body.get("token", ""))
    try:
        return await asyncio.to_thread(netscan.dns_lookup, body.get("name", ""),
                                       body.get("type", "A"), body.get("resolver", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/net/banner")
async def api_net_banner(req: Request):
    body = await req.json()
    check_token(body.get("token", ""))
    check_target(body.get("host", ""), body.get("confirm", 0))
    try:
        return await asyncio.to_thread(netscan.banner, body.get("host", ""),
                                       int(body.get("port", 443)),
                                       bool(body.get("tls", False)))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/net/http")
async def api_net_http(req: Request):
    body = await req.json()
    check_token(body.get("token", ""))
    url = body.get("url", "")
    from urllib.parse import urlparse
    host = urlparse(url if "://" in url else "http://" + url).hostname or url
    check_target(host, body.get("confirm", 0))
    try:
        return await asyncio.to_thread(netscan.http_check, url)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/net/whois")
async def api_net_whois(req: Request):
    body = await req.json()
    check_token(body.get("token", ""))
    try:
        return await asyncio.to_thread(netscan.whois_lookup, body.get("target", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


SVC_PORTS = {                       # port -> which service view the UI should open
    21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 111: "nfs", 2049: "nfs",
    465: "smtp", 587: "smtp", 2525: "smtp", 80: "http", 8080: "http", 8000: "http",
    8008: "http", 8888: "http", 3000: "http", 5000: "http", 9090: "http",
    443: "https", 8443: "https", 9443: "https",
}


@app.get("/api/svc/tools")
async def api_svc_tools(token: str = ""):
    """Which command-line clients exist here, so the UI only offers real ones."""
    check_token(token)
    names = ["telnet", "nc", "ncat", "socat", "curl", "ftp", "lftp", "ncftp",
             "sftp", "ssh", "openssl", "mount.nfs", "showmount", "smbclient"]
    return {"tools": {n: bool(shutil.which(n)) for n in names}}


@app.post("/api/svc/ftp")
async def api_svc_ftp(req: Request):
    body = await req.json()
    check_token(body.get("token", ""))
    check_target(body.get("host", ""), body.get("confirm", 0))
    try:
        return await asyncio.to_thread(
            netscan.ftp_list, body.get("host", ""), int(body.get("port", 21)),
            body.get("user", "anonymous"), body.get("password", "anonymous@"),
            body.get("path", ""), bool(body.get("tls", False)))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/svc/ftp/get")
async def api_svc_ftp_get(host: str, path: str, port: int = 21,
                          user: str = "anonymous", password: str = "anonymous@",
                          confirm: int = 0, token: str = ""):
    check_token(token)
    check_target(host, confirm)
    try:
        data = await asyncio.to_thread(netscan.ftp_fetch, host, port, user, password, path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    name = os.path.basename(path) or "download"
    return Response(content=data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/svc/ssh")
async def api_svc_ssh(req: Request):
    body = await req.json()
    check_token(body.get("token", ""))
    check_target(body.get("host", ""), body.get("confirm", 0))
    try:
        return await asyncio.to_thread(netscan.ssh_probe, body.get("host", ""),
                                       int(body.get("port", 22)),
                                       body.get("user", "root"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/svc/smtp")
async def api_svc_smtp(req: Request):
    body = await req.json()
    check_token(body.get("token", ""))
    check_target(body.get("host", ""), body.get("confirm", 0))
    try:
        return await asyncio.to_thread(netscan.smtp_probe, body.get("host", ""),
                                       int(body.get("port", 25)),
                                       bool(body.get("starttls", True)))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/svc/nfs")
async def api_svc_nfs(req: Request):
    body = await req.json()
    check_token(body.get("token", ""))
    check_target(body.get("host", ""), body.get("confirm", 0))
    try:
        return await asyncio.to_thread(netscan.nfs_exports, body.get("host", ""),
                                       int(body.get("port", 111)))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.websocket("/ws/raw")
async def ws_raw(ws: WebSocket, host: str = "", port: int = 23,
                 token: str = "", confirm: int = 0):
    """A bare TCP pipe for telnet and any other line protocol."""
    if TOKEN and token != TOKEN:
        await ws.close(code=4401)
        return
    try:
        ip = netscan.resolve(host)
    except ValueError as e:
        await ws.accept()
        await ws.send_text(f"\r\n[{e}]\r\n")
        await ws.close()
        return
    if not netscan.is_private(ip) and not confirm:
        await ws.close(code=4403)
        return

    await ws.accept()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, int(port)), timeout=8)
    except (OSError, asyncio.TimeoutError) as e:
        await ws.send_text(f"\r\n[cannot connect to {ip}:{port} — {e}]\r\n")
        await ws.close()
        return
    await ws.send_text(f"[connected to {ip}:{port}]\r\n")

    async def pump():                       # socket -> browser
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                await ws.send_text(data.decode("utf-8", "replace"))
        except asyncio.CancelledError:      # the browser went away; say nothing
            raise
        except Exception:                                      # noqa: BLE001
            pass
        else:
            try:                            # only the remote end hanging up gets a notice
                await ws.send_text("\r\n[remote closed]\r\n")
                await ws.close()
            except Exception:                                  # noqa: BLE001
                pass

    task = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive_text()
            writer.write(msg.encode("utf-8", "ignore"))
            await writer.drain()
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        task.cancel()
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


@app.get("/api/users")
async def api_users(token: str = ""):
    """Accounts on this machine, split into ones you can log into and service ones."""
    check_token(token)
    out = []
    for e in pwd.getpwall():
        login = e.pw_shell not in NOLOGIN
        if e.pw_uid == 0:
            kind = "root"
        elif e.pw_uid >= 1000 and e.pw_uid < 65534 and login:
            kind = "login"
        else:
            kind = "system"
        out.append({"name": e.pw_name, "uid": e.pw_uid, "home": e.pw_dir,
                    "shell": e.pw_shell, "kind": kind, "login": login})
    order = {"root": 0, "login": 1, "system": 2}
    out.sort(key=lambda u: (order[u["kind"]], u["uid"]))
    try:
        me = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        me = str(os.getuid())
    return {"users": out, "server_user": me, "sudo": sudo_available()}


@app.get("/api/cwd")
async def api_cwd(sid: str = "", token: str = ""):
    """Where the shell is right now. Cheap enough to poll: one readlink."""
    check_token(token)
    sess = SESSIONS.get(sid)
    if not sess:
        return {"cwd": "", "alive": False}
    return {"cwd": sess.cwd(), "user": sess.user(), "alive": True}


@app.get("/api/net")
async def api_net(token: str = ""):
    """Open connections. Mapping sockets to pids needs root to be complete."""
    check_token(token)
    if os.geteuid() != 0 and sudo_available():
        out = sudo_helper("net", "")          # root sees every socket's owning pid
        if out is not None:
            out["sudo"] = True
            return out
    return do_net()


@app.get("/api/ps")
async def api_ps(token: str = ""):
    check_token(token)
    return _proc_list()


@app.post("/api/kill")
async def api_kill(body: dict = Body(...)):
    """Signal a process. Falls back to sudo for processes owned by someone else."""
    check_token(body.get("token", ""))
    try:
        pid = int(body.get("pid"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad pid")
    name = str(body.get("signal", "TERM")).upper().lstrip("SIG")
    sig = getattr(signal, "SIG" + name, None)
    if sig is None or pid <= 1:
        raise HTTPException(status_code=400, detail="bad signal or pid")
    if pid == os.getpid():
        raise HTTPException(status_code=400, detail="refusing to signal webterm itself")
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        raise HTTPException(status_code=404, detail="no such process")
    except PermissionError as e:
        ok, msg = run_sudo(["kill", "-" + name, str(pid)])
        if not ok:
            raise HTTPException(status_code=403, detail=(e.strerror or "not permitted") +
                                " — process owned by another user")
        return {"ok": True, "sudo": True}
    return {"ok": True}


@app.get("/api/download")
async def api_download(path: str, token: str = ""):
    """A file streams as-is; a folder is zipped first."""
    check_token(token)
    p = os.path.realpath(os.path.expanduser(path))

    if os.path.isfile(p):
        return FileResponse(p, filename=os.path.basename(p), media_type="application/octet-stream")

    if not os.path.isdir(p):
        raise HTTPException(status_code=404, detail="no such path")

    tmp = tempfile.NamedTemporaryFile(prefix="webterm-", suffix=".zip", delete=False)
    tmp.close()
    base = os.path.basename(p.rstrip("/")) or "root"
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for root, dirs, files in os.walk(p, followlinks=False):
                dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
                rel_root = os.path.relpath(root, p)
                if rel_root == ".":
                    rel_root = ""
                if not dirs and not files and rel_root:
                    z.writestr(os.path.join(base, rel_root) + "/", "")
                for f in files:
                    full = os.path.join(root, f)
                    if os.path.islink(full) or not os.path.isfile(full):
                        continue
                    try:
                        z.write(full, os.path.join(base, rel_root, f))
                    except (OSError, PermissionError):
                        continue        # unreadable files are skipped, not fatal
    except Exception:
        os.unlink(tmp.name)
        raise HTTPException(status_code=500, detail="zip failed")

    return FileResponse(
        tmp.name,
        filename=base + ".zip",
        media_type="application/zip",
        background=BackgroundTask(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name)),
    )


def _owner(uid: int, gid: int):
    try:
        u = pwd.getpwuid(uid).pw_name
    except KeyError:
        u = str(uid)
    try:
        g = grp.getgrgid(gid).gr_name
    except KeyError:
        g = str(gid)
    return u, g


def do_stat(p: str, deep: int = 0) -> dict:
    """Properties for one entry. deep=1 also walks a folder for its true size."""
    st = os.lstat(p)
    is_link = statmod.S_ISLNK(st.st_mode)
    target = os.readlink(p) if is_link else None
    if is_link:
        try:
            st = os.stat(p)
        except OSError:
            pass
    is_dir = statmod.S_ISDIR(st.st_mode)
    user, group = _owner(st.st_uid, st.st_gid)

    size, files, folders = st.st_size, 0, 0
    if is_dir and deep:
        size = 0
        for root, dirs, fs in os.walk(p, followlinks=False):
            folders += len(dirs)
            for f in fs:
                files += 1
                try:
                    size += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
    elif is_dir:
        try:
            with os.scandir(p) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        folders += 1
                    else:
                        files += 1
        except OSError:
            pass

    return {
        "path": p,
        "name": os.path.basename(p) or "/",
        "dir": is_dir,
        "link": is_link,
        "target": target,
        "hidden": os.path.basename(p).startswith("."),
        "size": size,
        "files": files,
        "folders": folders,
        "mode": st.st_mode & 0o7777,
        "modestr": statmod.filemode(st.st_mode),
        "user": user,
        "group": group,
        "uid": st.st_uid,
        "gid": st.st_gid,
        "mtime": st.st_mtime,
        "atime": st.st_atime,
        "ctime": st.st_ctime,
        "readonly": not os.access(p, os.W_OK),
        "deep": bool(deep),
    }


@app.get("/api/stat")
async def api_stat(path: str, token: str = "", deep: int = 0):
    check_token(token)
    p = os.path.realpath(os.path.expanduser(path))
    try:
        return do_stat(p, deep)
    except PermissionError:
        out = sudo_helper("stat", p, deep=deep)
        if out is not None:
            out["sudo"] = True
            return out
        raise HTTPException(status_code=403, detail="permission denied" + owner_hint(p))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no such path")


def do_read(p: str) -> dict:
    """Text content for the editor. Raises OSError, or ValueError for refusals."""
    st = os.stat(p)
    if not statmod.S_ISREG(st.st_mode):
        raise ValueError("not a file")
    if st.st_size > EDIT_MAX:
        raise ValueError(f"file is {st.st_size} bytes, limit {EDIT_MAX}")
    raw = open(p, "rb").read()
    if b"\0" in raw[:8192]:
        raise ValueError("binary file")
    try:
        text, encoding = raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text, encoding = raw.decode("latin-1"), "latin-1"
    return {
        "path": p,
        "name": os.path.basename(p),
        "text": text,
        "encoding": encoding,
        "mtime": st.st_mtime,
        "size": st.st_size,
        "crlf": raw.count(b"\r\n") > 0,
        "readonly": not os.access(p, os.W_OK),
    }


@app.get("/api/read")
async def api_read(path: str, token: str = ""):
    check_token(token)
    p = os.path.realpath(os.path.expanduser(path))
    try:
        return do_read(p)
    except PermissionError:
        out = sudo_helper("read", p)
        if out is not None:
            out["sudo"] = True
            out["readonly"] = False          # sudo can write it back too
            return out
        raise HTTPException(status_code=403, detail="permission denied" + owner_hint(p))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no such file")
    except ValueError as e:
        code = 415 if "binary" in str(e) else (413 if "limit" in str(e) else 400)
        raise HTTPException(status_code=code, detail=str(e))


@app.post("/api/write")
async def api_write(body: dict = Body(...)):
    """Save. Pass the mtime you loaded to detect edits made underneath you."""
    check_token(body.get("token", ""))
    p = os.path.realpath(os.path.expanduser(body.get("path", "")))
    text = body.get("text", "")
    if os.path.isdir(p):
        raise HTTPException(status_code=400, detail="path is a directory")

    base = body.get("mtime")
    if base and os.path.exists(p):
        now = os.stat(p).st_mtime
        if abs(now - float(base)) > 0.001 and not body.get("force"):
            raise HTTPException(status_code=409, detail="file changed on disk since you opened it")

    if body.get("crlf"):
        text = text.replace("\n", "\r\n")
    data = text.encode(body.get("encoding") or "utf-8", "replace")

    # write to a temp file in the same dir, then rename: a crash can't truncate the original
    d = os.path.dirname(p) or "."
    try:
        fd, tmp = tempfile.mkstemp(prefix=".webterm-", dir=d)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if os.path.exists(p):
            shutil.copymode(p, tmp)
        os.replace(tmp, p)
    except OSError as e:
        ok = False
        if os.path.exists(p):
            fd2, tmp2 = tempfile.mkstemp(prefix="webterm-", dir="/tmp")
            with os.fdopen(fd2, "wb") as f:
                f.write(data)
            os.chmod(tmp2, 0o644)
            ok, _ = run_sudo(["cp", "--", tmp2, p])     # cp onto existing keeps owner+mode
            os.unlink(tmp2)
        try:
            os.unlink(tmp)
        except Exception:
            pass
        if not ok:
            raise HTTPException(status_code=403,
                                detail=(e.strerror or "cannot write") + owner_hint(p))
    st = os.stat(p)
    return {"ok": True, "path": p, "mtime": st.st_mtime, "size": st.st_size}


@app.post("/api/touch")
async def api_touch(body: dict = Body(...)):
    """Create an empty file (New file in the explorer)."""
    check_token(body.get("token", ""))
    parent = os.path.realpath(os.path.expanduser(body.get("path", "")))
    name = (body.get("name") or "").strip()
    if not name or "/" in name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="bad name")
    dest = os.path.join(parent, name)
    if os.path.lexists(dest):
        raise HTTPException(status_code=409, detail="already exists")
    try:
        open(dest, "x").close()
    except OSError as e:
        ok, msg = run_sudo(["touch", dest])
        if not ok:
            raise HTTPException(status_code=403,
                                detail=(e.strerror or "create failed") + owner_hint(parent))
        return {"ok": True, "path": dest, "sudo": True}
    return {"ok": True, "path": dest}


@app.post("/api/chmod")
async def api_chmod(body: dict = Body(...)):
    check_token(body.get("token", ""))
    p = os.path.realpath(os.path.expanduser(body.get("path", "")))
    try:
        mode = int(str(body.get("mode", "")), 8)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad mode")
    if not 0 <= mode <= 0o7777:
        raise HTTPException(status_code=400, detail="bad mode")
    try:
        os.chmod(p, mode)
    except PermissionError as e:
        ok, msg = run_sudo(["chmod", format(mode, "04o"), p])
        if not ok:
            raise HTTPException(status_code=403,
                                detail=(e.strerror or "chmod failed") + owner_hint(p))
        return {"ok": True, "mode": mode, "sudo": True}
    except OSError as e:
        raise HTTPException(status_code=403, detail=e.strerror or "chmod failed")
    return {"ok": True, "mode": mode}


@app.post("/api/rename")
async def api_rename(body: dict = Body(...)):
    """Rename within the same directory — also how hide/unhide works (leading dot)."""
    check_token(body.get("token", ""))
    p = os.path.realpath(os.path.expanduser(body.get("path", "")))
    name = (body.get("name") or "").strip()
    if not name or "/" in name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="bad name")
    if not os.path.lexists(p):
        raise HTTPException(status_code=404, detail="no such path")
    dest = os.path.join(os.path.dirname(p), name)
    if os.path.lexists(dest):
        raise HTTPException(status_code=409, detail="name already exists")
    try:
        os.rename(p, dest)
    except (PermissionError, OSError) as e:
        ok, msg = run_sudo(["mv", "-T", p, dest])
        if not ok:
            raise HTTPException(status_code=403,
                                detail=(e.strerror or "rename failed") + owner_hint(p))
        return {"ok": True, "path": dest, "sudo": True}
    return {"ok": True, "path": dest}


@app.post("/api/mkdir")
async def api_mkdir(body: dict = Body(...)):
    check_token(body.get("token", ""))
    parent = os.path.realpath(os.path.expanduser(body.get("path", "")))
    name = (body.get("name") or "").strip()
    if not name or "/" in name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="bad name")
    dest = os.path.join(parent, name)
    try:
        os.mkdir(dest)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="already exists")
    except OSError as e:
        ok, msg = run_sudo(["mkdir", dest])
        if not ok:
            raise HTTPException(status_code=403,
                                detail=(e.strerror or "mkdir failed") + owner_hint(parent))
        return {"ok": True, "path": dest, "sudo": True}
    return {"ok": True, "path": dest}


@app.post("/api/delete")
async def api_delete(body: dict = Body(...)):
    check_token(body.get("token", ""))
    p = os.path.realpath(os.path.expanduser(body.get("path", "")))
    if p == "/" or p == os.path.expanduser("~"):
        raise HTTPException(status_code=400, detail="refusing to delete that")
    try:
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.remove(p)
    except OSError as e:
        ok, msg = run_sudo(["rm", "-rf", "--", p])
        if not ok:
            raise HTTPException(status_code=403,
                                detail=(e.strerror or "delete failed") + owner_hint(p))
        return {"ok": True, "sudo": True}
    return {"ok": True}


@app.websocket("/ws")
async def ws_terminal(ws: WebSocket):
    if TOKEN and ws.query_params.get("token") != TOKEN:
        await ws.close(code=4401)
        return

    await ws.accept()
    try:
        rows = int(ws.query_params.get("rows", 24))
        cols = int(ws.query_params.get("cols", 80))
    except ValueError:
        rows, cols = 24, 80

    session = Session(rows, cols)
    SESSIONS[session.sid] = session
    await ws.send_text(json.dumps({"t": "sid", "d": session.sid}))
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def on_readable():
        try:
            data = os.read(session.fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        queue.put_nowait(data)

    loop.add_reader(session.fd, on_readable)

    async def pump_to_browser():
        decoder = __import__("codecs").getincrementaldecoder("utf-8")("replace")
        while True:
            chunk = await queue.get()
            if not chunk:  # shell exited
                await ws.send_text(json.dumps({"t": "exit"}))
                return
            await ws.send_text(json.dumps({"t": "o", "d": decoder.decode(chunk)}))

    pumper = asyncio.create_task(pump_to_browser())
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            kind = msg.get("t")
            if kind == "i":
                session.write(msg.get("d", ""))
            elif kind == "r":
                session.resize(msg.get("rows", 24), msg.get("cols", 80))
            if pumper.done():
                break
    except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
        pass
    finally:
        pumper.cancel()
        SESSIONS.pop(session.sid, None)
        try:
            loop.remove_reader(session.fd)
        except Exception:
            pass
        session.close()
        try:
            await ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn

    p = argparse.ArgumentParser(description="minimal web terminal")
    p.add_argument("--helper", choices=["ls", "stat", "read", "net", "trace"],
                   help="internal: run one read op and print JSON (used for the sudo retry)")
    p.add_argument("--path", default="")
    p.add_argument("--hidden", type=int, default=0)
    p.add_argument("--deep", type=int, default=0)
    p.add_argument("--host", default=os.environ.get("WEBTERM_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 7681)))
    p.add_argument("--shell", default=SHELL)
    args = p.parse_args()

    if args.helper:                      # elevated worker: no server, just JSON on stdout
        try:
            if args.helper == "ls":
                out = do_ls(args.path, args.hidden)
            elif args.helper == "stat":
                out = do_stat(args.path, args.deep)
            elif args.helper == "net":
                out = do_net()
            elif args.helper == "trace":
                out = netscan.traceroute(args.path, args.deep or 20)
            else:
                out = do_read(args.path)
        except Exception as e:
            print(json.dumps({"error": str(e)}), file=sys.stderr)
            raise SystemExit(2)
        print(json.dumps(out))
        raise SystemExit(0)

    if args.shell:
        SHELL = args.shell
    print(f"  webterm  ->  http://{args.host}:{args.port}   shell={pick_shell()}")
    if not TOKEN:
        print("  no token set: anyone who can reach this port gets a shell.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", ws_ping_interval=20)
