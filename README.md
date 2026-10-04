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
| modifiers | `ctrl` `alt` | sticky: tap to arm (button inverts), then press any key — on the bar *or* your physical keyboard — and it's applied once, then disarms |
| clipboard | `sel` `copy` `paste` `all` | select mode, copy selection, paste, select everything |
| history / cursor | `↑` `↓` `←` `→` | `\x1b[A/B/D/C` — ↑/↓ are bash history; hold to repeat |
| signals | `^C` `^D` `^Z` `^R` | interrupt, EOF, suspend, reverse-i-search |
| line edit | `⇱` `⇲` `⌫` | Ctrl-A, Ctrl-E, backspace (hold to repeat) |
| symbols | `\|` `~` `/` `-` `↵` | awkward characters on phone keyboards |

Hold-to-repeat kicks in after 420 ms at ~14 keys/s. Adding a key is one line in the `KEYS`
array in `static/index.html`: `{ l: "label", d: "bytes to send", t: "tooltip", rep: true }`.

## File browser

A floating button (bottom-right, or `Ctrl+Shift+F`) slides out a file browser. It opens on
**the shell's own current directory** — the server resolves `/proc/<shell pid>/cwd`, so it
follows your `cd`s instead of tracking some separate notion of "where you are".

| action | result |
|---|---|
| click a folder | browse into it |
| click a folder's `cd` chip | runs `cd <path>` in the shell and closes the drawer |
| click a file | types its quoted path at the prompt, focus returns to the terminal |
| click a file's `⤓` chip | downloads it |
| breadcrumb segment | jump to any ancestor directory |
| `sync` | jump back to wherever the shell is now |
| `.*` | toggle dotfiles |
| `Esc` / scrim / `✕` | close |

Folders sort first, executables are tinted, symlinks get their own icon, and sizes are
human-readable. Paths are quoted POSIX-style (`'it'\''s a file.txt'`), so spaces and
apostrophes in filenames can't break the command — and `cd` is prefixed with `\x15`
(kill-line) so it can't concatenate onto a path you already inserted. Filenames render via
`textContent`, never `innerHTML`.

Endpoints: `GET /api/ls?path=&sid=&hidden=` (empty `path` means "follow the shell") and
`GET /api/download?path=`. Both honour `WEBTERM_TOKEN`. They are deliberately **not**
sandboxed to a root directory — this app already hands out a shell, so a path jail would be
security theatre.

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
