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
import tempfile
import termios
import time
import uuid
import zipfile
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
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

    def cwd(self) -> str:
        """Where the shell currently is, so the file browser can follow it."""
        try:
            return os.readlink(f"/proc/{self.pid}/cwd")       # Linux
        except OSError:
            return os.path.expanduser("~")

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


SESSIONS: dict = {}          # sid -> Session, so /api/ls can follow the shell's cwd


def check_token(token: str) -> None:
    if TOKEN and token != TOKEN:
        raise HTTPException(status_code=401, detail="bad token")


def resolve(path: str, sid: str) -> str:
    """Absolute path for a request; empty path means 'wherever the shell is'."""
    if not path:
        s = SESSIONS.get(sid)
        return s.cwd() if s else os.path.expanduser("~")
    return os.path.realpath(os.path.expanduser(path))


@app.get("/api/ls")
async def api_ls(path: str = "", sid: str = "", token: str = "", hidden: int = 0):
    """List a directory. No sandbox: this is a shell, the files are already reachable."""
    check_token(token)
    target = resolve(path, sid)
    if not os.path.isdir(target):
        raise HTTPException(status_code=404, detail="not a directory")

    entries, truncated = [], False
    try:
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
                    size, mtime = st.st_size, st.st_mtime
                    mode = st.st_mode
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
    except PermissionError:
        raise HTTPException(status_code=403, detail="permission denied")

    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {
        "path": target,
        "parent": None if target == "/" else os.path.dirname(target) or "/",
        "home": os.path.expanduser("~"),
        "entries": entries,
        "truncated": truncated,
        "now": time.time(),
    }


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


@app.get("/api/stat")
async def api_stat(path: str, token: str = "", deep: int = 0):
    """Properties for one entry. deep=1 also walks a folder for its true size."""
    check_token(token)
    p = os.path.realpath(os.path.expanduser(path))
    try:
        st = os.lstat(p)
    except OSError:
        raise HTTPException(status_code=404, detail="no such path")

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
    except OSError as e:
        raise HTTPException(status_code=403, detail=e.strerror or "rename failed")
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
        raise HTTPException(status_code=403, detail=e.strerror or "mkdir failed")
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
        raise HTTPException(status_code=403, detail=e.strerror or "delete failed")
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
    p.add_argument("--host", default=os.environ.get("WEBTERM_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 7681)))
    p.add_argument("--shell", default=SHELL)
    args = p.parse_args()
    if args.shell:
        SHELL = args.shell
    print(f"  webterm  ->  http://{args.host}:{args.port}   shell={pick_shell()}")
    if not TOKEN:
        print("  no token set: anyone who can reach this port gets a shell.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", ws_ping_interval=20)
