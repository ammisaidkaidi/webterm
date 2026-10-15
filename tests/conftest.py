"""Fixtures for the end-to-end suite.

The tests drive a real browser against a running webterm, so they need three
things in place: the server, a page that has finished connecting its
WebSocket, and in some cases a service to connect to or a second user account
to switch to. Anything missing that the test cannot create itself is a skip,
never a failure, so a partial environment still runs the part it can.

Run them with:

    python3 -m pytest tests -v

Set WEBTERM_URL to point at an already-running instance; otherwise a server is
started on port 7681 for the session and stopped afterwards.
"""

import os
import pwd
import socket
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("WEBTERM_PORT", 7681))
URL = os.environ.get("WEBTERM_URL", f"http://127.0.0.1:{PORT}/")
EDT = "/tmp/edt"


def port_open(port, host="127.0.0.1"):
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def wait_for_port(port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(0.3)
    return False


# --------------------------------------------------------------------------
# the server under test
# --------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def server():
    """Reuse a running webterm if there is one, otherwise start our own."""
    if os.environ.get("WEBTERM_URL") or port_open(PORT):
        yield URL
        return
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if not wait_for_port(PORT):
        proc.kill()
        pytest.fail(f"webterm did not start on port {PORT}")
    yield URL
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# --------------------------------------------------------------------------
# browser
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "viewport": {"width": 1250, "height": 860}}


@pytest.fixture
def ctx(page):
    return page.context


@pytest.fixture
def pg(page, server):
    """A page with the terminal already connected.

    Page errors are collected and asserted at teardown: a silent JavaScript
    exception has broken this UI more than once, and node --check cannot see
    the ones that only happen at runtime.
    """
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # Keep every confirm's text, since some tests assert on how many times the
    # UI asked. Accepting is the default, but a test that registers its own
    # handler (to dismiss a confirm, or to type into a prompt) owns the dialog:
    # two handlers both answering raises "dialog is already handled".
    page.dialogs = []

    def on_dialog(dialog):
        page.dialogs.append(dialog.message)
        impl = getattr(page, "_impl_obj", None)
        mine = len(impl.listeners("dialog")) if impl else 1
        if mine == 1:
            dialog.accept()

    page.on("dialog", on_dialog)
    page.goto(server)
    page.wait_for_selector("#status:text('connected')")
    time.sleep(1.0)
    yield page
    assert not errors, "javascript errors on the page: " + " | ".join(errors)


# --------------------------------------------------------------------------
# assertions
# --------------------------------------------------------------------------

class Checks:
    """Collects named checks so one run reports every failure, not just the first.

    These suites walk the UI in sequence, and an early assert would hide
    everything after it. Each check prints as it runs; the fixture fails the
    test at teardown if any of them failed.
    """

    def __init__(self):
        self.failures = []

    def __call__(self, name, ok, detail=""):
        print(f"{name:<44} {'PASS' if ok else 'FAIL'} {str(detail)[:62]}")
        if not ok:
            self.failures.append(f"{name} ({str(detail)[:120]})")
        return ok


@pytest.fixture
def P():
    checks = Checks()
    yield checks
    assert not checks.failures, "failed checks:\n  " + "\n  ".join(checks.failures)


# --------------------------------------------------------------------------
# things the tests connect to
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def services():
    """The fake FTP/telnet/SMTP servers from dev/fixtures.py."""
    missing = [p for p in (21, 23, 25) if not port_open(p)]
    if missing:
        pytest.skip(f"no fixture services on {missing}: run `sudo python3 dev/fixtures.py`")


@pytest.fixture
def no_capture(server):
    """No capture running, before or after.

    The capture is one process per server, not per page, so a leaked one from
    an earlier run would show up as "already capturing" here.
    """
    import urllib.request
    def stop():
        req = urllib.request.Request(server.rstrip("/") + "/api/cap/stop",
                                     data=b"{}", method="POST",
                                     headers={"content-type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except OSError:
            pass
    stop()
    yield
    stop()


@pytest.fixture(scope="session")
def user1():
    """A second login account, for the user switcher."""
    try:
        pwd.getpwnam("user1")
    except KeyError:
        pytest.skip("no user1 account: run `sudo useradd -m -s /bin/bash user1`")


@pytest.fixture
def edt():
    """A directory of files with the properties the editor tests need.

    Rewritten every run, because the tests edit and save these files.
    """
    os.makedirs(f"{EDT}/deep/deeper", exist_ok=True)
    # fresh.md is deliberately absent: the "new file" test creates it.
    if os.path.exists(f"{EDT}/fresh.md"):
        os.remove(f"{EDT}/fresh.md")
    for name, text in (("hello.py", "line one\nline two\n"),
                       ("conf.txt", "a=1\n"), ("ro.txt", "read only\n")):
        path = f"{EDT}/{name}"
        if os.path.exists(path):
            os.chmod(path, 0o644)                # ro.txt is left unwritable below
        with open(path, "w") as f:
            f.write(text)
    with open(f"{EDT}/bin.dat", "wb") as f:
        f.write(bytes(range(256)) * 2)          # NUL bytes: the editor must refuse it
    os.chmod(f"{EDT}/ro.txt", 0o444)            # opens, but cannot be saved
    return EDT


@pytest.fixture
def root_file(edt):
    """A root-owned file, to exercise the sudo fallback."""
    path = f"{EDT}/rootie.txt"
    done = subprocess.run(["sudo", "-n", "touch", path], capture_output=True)
    if done.returncode != 0:
        pytest.skip("passwordless sudo unavailable, cannot create a root-owned file")
    subprocess.run(["sudo", "-n", "chown", "root:root", path], capture_output=True)
    subprocess.run(["sudo", "-n", "chmod", "755", path], capture_output=True)
    return path
