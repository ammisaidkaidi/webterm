# webterm

A minimal web terminal. Open a page, get a real shell. No login, no accounts, no database.

> **⚠️ This is remote code execution by design.** There is no authentication unless you
> enable `WEBTERM_TOKEN`. Anyone who can reach the port gets a shell as the user running
> the process — and therefore that user's files, keys and credentials. Default to
> `--host 127.0.0.1` and read [Security](#security--read-this) before exposing it.

```
webterm/
├── server.py              # FastAPI + PTY-backed shell over WebSocket (~170 lines)
├── static/index.html      # the whole UI: one page, inline CSS
└── static/vendor/         # xterm.js + fit/web-links addons (vendored, no CDN at runtime)
```

## Run

```bash
pip install fastapi "uvicorn[standard]"
python3 server.py                      # http://0.0.0.0:7681
python3 server.py --port 8080 --host 127.0.0.1
python3 server.py --shell /bin/zsh
```

Env vars: `PORT`, `WEBTERM_HOST`, `WEBTERM_SHELL`, `WEBTERM_TOKEN`.

## What you get

- A **true PTY** (`pty.fork`), so it's a real interactive shell: colors, `cd`, history,
  tab-completion, Ctrl-C / Ctrl-D, and full-screen apps like `top`, `htop`, `vim`, `less`.
- Live resize — the terminal's rows/cols are pushed to the PTY with `TIOCSWINSZ`, so
  curses apps redraw correctly when you resize the browser window.
- Auto-reconnect with backoff; a status dot (green = connected).
- `clear` and `restart` buttons. `restart` kills the shell's process group and forks a fresh one.
- **Bottom key bar** for the keys a touch keyboard doesn't have — see below.
- Light/dark follows the OS theme. Mobile-friendly.
- One session per browser tab; each tab is an independent shell, cleaned up on disconnect.

## Bottom key bar

A single scrollable row under the terminal. Buttons never steal focus (`mousedown` is
prevented), so the on-screen keyboard stays open on mobile.

| group | keys | sends |
|---|---|---|
| core | `esc` `tab` | `\x1b`, `\t` — tab is shell completion |
| navigation | `⬆ ..` | runs `cd ..` in the shell (kill-line first, so it can't concatenate) |
| modifiers | `ctrl` `alt` | sticky: tap to arm (button inverts), then press any key — on the bar *or* your physical keyboard — and it's applied once, then disarms |
| clipboard | `sel` `copy` `paste` `all` | select mode, copy selection, paste, select everything |
| history / cursor | `↑` `↓` `←` `→` | `\x1b[A/B/D/C` — ↑/↓ are bash history; hold to repeat |
| signals | `^C` `^D` `^Z` `^R` | interrupt, EOF, suspend, reverse-i-search |
| line edit | `⇱` `⇲` `⌫` | Ctrl-A, Ctrl-E, backspace (hold to repeat) |
| symbols | `\|` `~` `/` `-` `↵` | awkward characters on phone keyboards |

Hold-to-repeat kicks in after 420 ms at ~14 keys/s. Adding a key is one line in the `KEYS`
array in `static/index.html`: `{ l: "label", d: "bytes to send", t: "tooltip", rep: true }`.

## File browser

A floating button (bottom-right, or `Ctrl+Shift+F`) slides out a Windows-Explorer-style
browser: address breadcrumbs, back / forward / up, a details list with sortable **Name ·
Date modified · Type · Size** columns, folders first, a status bar, and a right-click
context menu. It opens on **the shell's own current directory** — the server resolves
`/proc/<shell pid>/cwd`, so it follows your `cd`s.

| action | result |
|---|---|
| single click | select (status bar shows name and size) |
| double click | folder: open · **file: open in the editor** |
| right-click / long-press | context menu |
| `download` | file: streams directly · **folder: zipped on the fly and downloaded** |
| `properties` (or `Alt+Enter`) | properties dialog — see below |
| `new folder` | `mkdir` in the current directory |
| `sync` / `hidden` | follow the shell · show dotfiles (shown dimmed, like Explorer) |
| column headers | sort by name, date, type or size; click again to reverse |
| `←` `→` `↑` | back, forward, up — with `Alt+←` / `Alt+→` / `Backspace` |
| `↑` `↓` `Enter` `F2` `Delete` `F5` | select, open, rename, delete, refresh |
| `Esc` | closes dialog, then menu, then drawer |

Context menu: Open / **Edit**, Insert path, `cd here`, **View with less**, Download, Rename,
Hide / Unhide, Delete, Properties.

### Editor (notepad)

Double-click a file in the explorer and it opens in an editor built on
[Ace](https://ace.c9.io/) (`ace-builds` 1.44.0, vendored under `static/vendor/ace/`, no CDN
and no build step). Syntax highlighting is chosen from the extension — 24 modes shipped,
plus `Makefile`, `Dockerfile` and dotfile special-cases.

- `Ctrl+S` or `save` writes the file; the green dot marks unsaved changes.
- `Ctrl+F` opens Ace's search box; undo/redo, multi-cursor and block selection come free.
- The header shows encoding, line endings and size; the footer shows `Ln`/`Col` and the
  last save time. `wrap` toggles word wrap.
- `new file` creates an empty file and opens it straight away.
- Closing with unsaved changes asks first; `Esc` closes the editor without closing the
  explorer behind it.

Safety properties worth knowing:

- **Binary files are refused** (`NUL` byte in the first 8 KB) rather than corrupted on
  save, and anything over 4 MB is refused too. Use download instead.
- **Read-only files open read-only** — Ace is locked and `save` is disabled, instead of
  failing after you have typed.
- **Concurrent edits are detected.** The editor sends the `mtime` it loaded; if the file
  changed underneath it the server answers `409` and you are asked before overwriting.
- **Saves are atomic**: content goes to a temp file in the same directory, inherits the
  original's mode, then `os.replace()` swaps it in, so a crash mid-save cannot truncate
  your file.
- Encoding and line endings round-trip: a `latin-1` file is written back as `latin-1`, and
  a CRLF file keeps CRLF.

### Properties

Shows name, type, location, size, modified / accessed / changed times, owner : group, and
the mode string. Editable:

- **Permissions** — the full Unix 3×3 grid (Owner / Group / Others × Read / Write /
  Execute) wired two-way to an octal field: tick a box and `644` becomes `744`, type an
  octal and the boxes follow. Applied with `chmod`.
- **Hidden** — ticking it renames the entry to a leading dot (and unticking strips it),
  which is what "hidden" actually means on Unix.
- **Read-only** — clears every write bit, the Windows attribute mapped onto `chmod`.
- **Rename** — the name field.
- **Folder size** — `calculate…` walks the tree for a true recursive size and file count,
  kept out of the normal listing so browsing stays fast.

### Permissions and sudo

The HTTP API runs as the user that started `server.py`, which is *not* whoever you become
with `sudo` inside the terminal — they are different processes. So changing the mode of a
root-owned file used to fail with `Operation not permitted` even for an operator who could
clearly do it by hand.

Now, when a file operation (chmod, rename, mkdir, delete, create, save) fails with
`EPERM`, the server retries it once through `sudo -n`. That is only possible when
passwordless sudo is already configured, and the shell this app serves can run `sudo -n`
itself, so the API gains nothing the terminal did not already have. Set `WEBTERM_SUDO=0`
to turn the fallback off; the error then names the owner and the server's user so the
cause is obvious.

When the fallback is unavailable or sudo needs a password, the UI offers a **run in
terminal** button that types the equivalent command (`sudo chmod 755 '/path'`) at the
prompt, where a password prompt can actually be answered.

Saving a root-owned file keeps its owner and mode: the content is copied over the existing
inode with `cp` rather than replaced.

Endpoints, all honouring `WEBTERM_TOKEN`: `GET /api/ls`, `GET /api/stat` (`deep=1` for
recursive size), `GET /api/download` (zips directories), `GET /api/read`, `POST /api/write`,
`/api/touch`, `/api/chmod`, `/api/rename`, `/api/mkdir`, `/api/delete`. Paths are quoted POSIX-style (`'it'\''s a file.txt'`) before
they reach the shell, filenames render via `textContent`, and `delete` refuses `/` and
`$HOME`. Nothing is jailed to a root directory — this app already hands out a shell, so a
path jail would be security theatre.

## Directory navigation

The header carries three buttons on the left — **back**, **up**, **forward** — that move
the shell between directories the way a file manager does.

They are driven by the shell's *real* working directory (`readlink /proc/<pid>/cwd`,
polled once a second through `GET /api/cwd`), not by guessing at what you typed. So a `cd`
typed by hand, a `pushd`, or a directory change made by a script all land in the history
exactly like a button click would. Typing a new `cd` drops the forward trail, as a browser
does. Hovering a button names the directory it will take you to.

Each button sends `\x15cd <path>` — the kill-line first means a half-typed command is
discarded rather than having `cd` glued onto the end of it. **up** is disabled at `/`, and
the history holds the last 100 directories.

## User switcher

Next to the arrows is a button showing the account the shell is currently running as.
Click it for the list of accounts on the machine, read from `getpwall()` and grouped:

* **ROOT** — uid 0
* **USERS** — uid 1000 and up with a real login shell
* **SYSTEM** — service accounts, collapsed behind a `show N system accounts` line

Picking one types the switch into the terminal: `sudo -i -u <name>` normally, or
`su - <name>` when you are already root. Nothing is run behind your back — the command
appears in the terminal, and if sudo asks for a password you type it there. The menu also
grows an `exit back to <user>` entry once you have switched, which sends `exit` rather
than nesting another shell. The button turns red while you are root.

This grants no privileges of its own: it only sends a command, and your sudoers policy
decides whether it succeeds.

The indicator follows the shell for real. `sudo` keeps its own real uid, so webterm walks
`/proc/<pid>/task/<pid>/children` down to the deepest descendant of the pty and reports
that process's uid — which also means `cd` inside an elevated shell keeps the directory
buttons and the file browser in sync (read through `sudo -n readlink` when the elevated
process is unreadable). A `su` typed by hand updates the button exactly like the menu.

## Process viewer

The upper floating button (or `Ctrl+Shift+P`) opens a two-tab panel styled with the Ace
editor's palette — One Dark in dark mode, TextMate in light — so the editor and the
monitor look like one tool.

### Processes tab

| column | meaning |
|---|---|
| PID | process id; your own processes are tinted with the theme accent |
| USER | owner, resolved through `pwd` |
| CPU% | measured between polls like `top`, not the misleading lifetime average |
| MEM | resident set size |
| S | process state (`R`, `S`, `D`, `Z`, `I`, …) |
| COMMAND | full command line, `[name]` for kernel threads |

Click a header to sort, type in the filter box to match on name, user, command or exact
pid, click a row to select, and use `term` / `kill -9` in the footer (double-click a row
sends `SIGTERM`). `SIGKILL` asks for confirmation first. The footer also shows load
average, cpu count, memory in use and uptime. `auto` polls every 2 seconds and is on when
the panel opens; scroll position and selection survive each refresh.

Signalling a process owned by another user falls back to `sudo -n`, exactly like the file
operations. `webterm` refuses to signal pid 1 or itself.

### Connections tab

Open sockets, parsed from `/proc/net/tcp`, `tcp6`, `udp` and `udp6`: protocol, local and
remote address:port, TCP state, a service name for well-known ports (`http`, `https`,
`ftp`, `ssh`, `dns`, `mysql`, `postgres`, `redis`, …) and the owning **PID / process**.
Listening sockets are tinted with the theme accent; the footer counts them.

Mapping a socket to its process means reading every `/proc/<pid>/fd`, which only root can
do completely, so this endpoint uses the same `sudo -n` helper — output matches
`ss -ltnp`. Without sudo it still lists every socket and says `pids limited (not root)`.

Selecting a row selects its owning process, so `term` and `kill -9` work straight from
this tab: find what is holding port 8080, kill it, without leaving the browser.

Data comes from `/proc` directly — no `psutil` dependency. `GET /api/ps`, `GET /api/net`,
`POST /api/kill` with `{pid, signal}`.

### Network tab

Thirteen small tools behind a row of chips. Selecting a tool never probes anything on its
own — you set the target and press **run**.

| tool | what it does |
|---|---|
| my network | hostname, primary IP, gateway, DNS servers, every NIC with MAC/MTU/state, routing table |
| public IP | WAN address with city, country and ASN/ISP (one outbound call to ipinfo.io, ip-api.com as backup) |
| ping | ICMP via the system binary, elevated with `sudo -n` when the kernel refuses; falls back to timed TCP handshakes where ICMP is blocked, and says which method it used |
| traceroute | raw-socket ICMP walk, one probe per TTL, reverse DNS per hop. Raw sockets need root, so the server re-runs itself with `sudo -n server.py --helper trace` |
| find hosts | subnet sweep: ARP cache + parallel ping + TCP knock on nine common ports, so hosts that drop ICMP still show up. Capped at 1024 addresses (/22) |
| scan ports | asyncio TCP connect scan, up to 800 sockets in flight. Blank spec = 53 common ports; also accepts `22,80,8000-8010` or `all`. Reports open / closed / filtered separately |
| DNS | A, AAAA, MX, TXT, NS, SOA, CNAME, SRV, PTR through dnspython, against any resolver you name |
| neighbours | ARP/NDP table with reverse DNS and MAC-vendor names from a built-in OUI slice |
| banner / TLS | first bytes a service sends, plus certificate subject, issuer, SANs, serial, SHA-256 fingerprint and days until expiry |
| HTTP check | follows the redirect chain, timing DNS, connect and TTFB for every hop, then lists the final response headers |
| bandwidth | live rx/tx rates per interface from `/proc/net/dev`, refreshed every 1.5 s with error and drop counters |
| whois / ASN | RDAP first (structured JSON, no binary needed), falling back to the `whois` binary and then raw port 43 |
| devices | SSDP/UPnP multicast search plus an mDNS browse via python-zeroconf: printers, casts, NAS, anything that ignores ping |

#### Connecting to a service

Every open port in a scan result gets a button that knows what lives behind it. The
protocol is chosen from the port number first, then from the service name, so a web
server on 8081 still offers **open** rather than a raw socket.

| service | button | what happens |
|---|---|---|
| http / https | open | opens `http(s)://host:port/` in a new browser tab |
| ftp | ftp | a small FTP browser: anonymous by default with a login box when the server refuses, directory navigation, file sizes, MLSD timestamps and a **get** button that downloads through the server |
| ssh | ssh | server banner, host key type with SHA-256 and MD5 fingerprints, the auth methods the server accepts, and the offered kex/cipher/MAC algorithms — all without logging in |
| telnet | telnet | a live raw TCP console with a send line, over a `/ws/raw` WebSocket |
| smtp | smtp | greeting, EHLO capabilities, AUTH mechanisms, SIZE limit, STARTTLS and its certificate — plus the same live console underneath |
| nfs | nfs | portmap dump of every registered RPC program, then the mountd export list with a **mount** button per share |
| anything else | connect | the raw TCP console |

Each view also has **open in terminal**, which hands the job to the real shell. The panel
closes and the command is typed for you — useful the moment you need interactive auth,
which the read-only probes deliberately do not do.

`GET /api/svc/tools` reports which clients are actually installed (`shutil.which`), and
the button offers the first one that exists rather than assuming. Plenty of containers
ship without `telnet` or `ftp`:

| service | tried in this order |
|---|---|
| ssh | `ssh -p <port> <host>` |
| ftp | `ftp` → `lftp` → `ncftp` → `curl ftp://…` with the credentials you logged in with |
| telnet | `telnet` → `nc -v` → `ncat -v` → `socat -,raw,echo=0 TCP:…` |
| smtp | `nc` → `ncat -C` → `telnet` → `socat`, or `openssl s_client -crlf` on port 465 |
| nfs | `sudo mount -t nfs <host>:<share> /mnt/<share>`, else `showmount -e` |
| raw | `nc` → `ncat` → `socat` → `telnet` |

When none of them exist the button is disabled and names what is missing, pointing at the
built-in console instead — that one needs nothing installed, since the server opens the
socket.

The SSH probe uses paramiko for the host key and the algorithm lists come from parsing
the server's `SSH_MSG_KEXINIT` directly. NFS speaks ONC-RPC over TCP by hand — portmap
`DUMP` then mountd `EXPORT` — so no `rpcbind` or `showmount` binary is needed, though
`showmount` is used as a fallback when it exists.

**Scanning other people's machines can be a criminal offence.** Any target outside
RFC1918 / loopback / link-local returns HTTP 428 until the browser confirms once; the
confirmation then lasts for that page session. Private targets never prompt.

Everything is pure Python — no `nmap`, no `traceroute`, no `dig`, no `whois` binary
required. `dnspython`, `cryptography` and `zeroconf` are listed in `requirements.txt`;
without them the DNS tool drops to A/AAAA, the certificate panel shows only its size, and
mDNS is skipped while SSDP still runs.

## Selecting and copying

A terminal canvas isn't normally selectable, so there are two paths:

**Desktop** — drag over the text as usual. Releasing the mouse copies the selection
automatically (X11 convention) and the status bar confirms `copied N chars`.
Middle-click pastes.

| shortcut | action |
|---|---|
| drag + release | copy selection |
| `Ctrl+Shift+C` / `Ctrl+Insert` | copy selection |
| `Ctrl+Shift+V` / `Shift+Insert` / middle-click | paste |
| `Ctrl+C` **with** a selection | copy (with no selection it still sends SIGINT) |
| `Ctrl+Shift+A` | select mode + select all |

**Touch** — tap `sel`. The terminal is swapped for a plain `<pre>` holding the same text,
so the OS long-press selection handles work normally; drag the handles, tap `copy`, tap
`sel` again to return. `all` grabs the whole screen plus scrollback in one tap. Wrapped
lines are rejoined into single logical lines, so copied commands paste back as one line.

Pasting uses `term.paste()`, which respects bracketed-paste mode — multi-line pastes land
in the shell safely instead of auto-executing each line. Copy falls back to a hidden
`<textarea>` + `execCommand` when the Clipboard API is unavailable (plain `http://` on a
non-localhost host), so it still works over LAN without TLS.

## Protocol

JSON over a single WebSocket at `/ws?rows=&cols=`:

| direction | message | meaning |
|---|---|---|
| browser → server | `{"t":"i","d":"ls\n"}` | keystrokes into the PTY |
| browser → server | `{"t":"r","rows":40,"cols":120}` | resize |
| server → browser | `{"t":"o","d":"..."}` | PTY output (UTF-8, incremental decoder so split multibyte chars never corrupt) |
| server → browser | `{"t":"exit"}` | shell exited |

`GET /healthz` → `{"ok":true}` for uptime checks.

## Security — read this

This is remote code execution by design. Anyone who can reach the port gets a shell as the
user running `server.py`, which also means access to that user's keys and files.

Safe ways to run it:

- **Localhost only:** `python3 server.py --host 127.0.0.1`, reach it over an SSH tunnel
  (`ssh -L 7681:localhost:7681 you@host`).
- **Shared token:** `WEBTERM_TOKEN=$(openssl rand -hex 16) python3 server.py`, then open
  `http://host:7681/?token=THETOKEN`. The WebSocket rejects anything else with code 4401.
- **Behind a reverse proxy** that adds TLS + auth. nginx needs the upgrade headers:

  ```nginx
  location / {
      proxy_pass http://127.0.0.1:7681;
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
      proxy_read_timeout 1d;
  }
  ```

- **Containerize it** so a compromise is scoped to the container:

  ```bash
  docker run -p 7681:7681 -v "$PWD":/app -w /app python:3.13-slim \
    sh -c "pip install -q fastapi 'uvicorn[standard]' && python server.py"
  ```

For a public deployment, at minimum: TLS, a token, and a non-root user with no secrets in
its home directory.
