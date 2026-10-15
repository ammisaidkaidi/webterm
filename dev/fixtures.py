"""Fake FTP, telnet and SMTP servers for the end-to-end tests.

The service views need something to connect to. Rather than depend on whatever
happens to be installed, this starts three throwaway servers on the standard
ports, which is why it needs root:

    sudo python3 dev/fixtures.py

FTP  21  pyftpdlib, anonymous, serving a small tree under /tmp/ftproot
telnet 23  a toy "FakeRouter" that echoes whatever you send it
SMTP 25  an ESMTP greeter that advertises STARTTLS and completes the handshake
         with a self-signed certificate generated on first run

Nothing here is a real server. They exist to give the probes in netscan.py a
predictable banner, capability list and directory listing to parse.
"""

import os
import socket
import ssl
import subprocess
import sys
import threading

FTP_PORT = int(os.environ.get("FIXTURE_FTP_PORT", 21))
TELNET_PORT = int(os.environ.get("FIXTURE_TELNET_PORT", 23))
SMTP_PORT = int(os.environ.get("FIXTURE_SMTP_PORT", 25))

FTP_ROOT = "/tmp/ftproot"
CERT = "/tmp/fake.crt"
KEY = "/tmp/fake.key"


def ensure_ftp_root():
    """A couple of files and a subdirectory, so listings have something in them."""
    os.makedirs(f"{FTP_ROOT}/subdir", exist_ok=True)
    with open(f"{FTP_ROOT}/readme.txt", "w") as f:
        f.write("hello from ftp\n")
    with open(f"{FTP_ROOT}/subdir/inner.txt", "w") as f:
        f.write("nested file\n")


def ensure_cert():
    """Self-signed certificate for the SMTP server's STARTTLS."""
    if os.path.exists(CERT) and os.path.exists(KEY):
        return
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", KEY,
         "-out", CERT, "-days", "30", "-nodes",
         "-subj", "/CN=fake.example/O=Webterm Test"],
        check=True, capture_output=True,
    )


def listener(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(5)
    return s


def serve(sock, handler):
    while True:
        conn, _ = sock.accept()
        threading.Thread(target=handler, args=(conn,), daemon=True).start()


def telnet_server():
    def handle(c):
        c.sendall(b"\r\nFakeRouter v1.2 ready\r\nlogin: ")
        while True:
            data = c.recv(256)
            if not data:
                return
            c.sendall(b"you said: " + data.strip() + b"\r\n> ")

    serve(listener(TELNET_PORT), handle)


def smtp_server():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)

    def handle(c):
        c.sendall(b"220 fake.example ESMTP FakeMTA\r\n")
        while True:
            try:
                line = c.recv(1024)
            except OSError:
                return
            if not line:
                return
            verb = line.upper()
            if verb.startswith(b"EHLO") or verb.startswith(b"HELO"):
                c.sendall(b"250-fake.example\r\n250-SIZE 35882577\r\n"
                          b"250-8BITMIME\r\n250-STARTTLS\r\n"
                          b"250-AUTH LOGIN PLAIN\r\n250 HELP\r\n")
            elif verb.startswith(b"STARTTLS"):
                c.sendall(b"220 Ready to start TLS\r\n")
                try:
                    c = ctx.wrap_socket(c, server_side=True)
                except OSError:
                    return
            elif verb.startswith(b"QUIT"):
                c.sendall(b"221 Bye\r\n")
                c.close()
                return
            else:
                c.sendall(b"250 OK\r\n")

    serve(listener(SMTP_PORT), handle)


def ftp_server():
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    auth = DummyAuthorizer()
    auth.add_anonymous(FTP_ROOT)
    handler = FTPHandler
    handler.authorizer = auth
    handler.banner = "FakeFTP 1.0 ready"
    FTPServer(("0.0.0.0", FTP_PORT), handler).serve_forever()


def main():
    if os.geteuid() != 0 and min(FTP_PORT, TELNET_PORT, SMTP_PORT) < 1024:
        sys.exit("ports 21/23/25 are privileged: run with sudo, "
                 "or set FIXTURE_FTP_PORT / FIXTURE_TELNET_PORT / FIXTURE_SMTP_PORT")
    ensure_ftp_root()
    ensure_cert()
    threading.Thread(target=telnet_server, daemon=True).start()
    threading.Thread(target=smtp_server, daemon=True).start()
    print(f"ftp :{FTP_PORT}  telnet :{TELNET_PORT}  smtp :{SMTP_PORT}", flush=True)
    ftp_server()


if __name__ == "__main__":
    main()
