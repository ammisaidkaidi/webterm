"""Packet capture: the third FAB, the capture controls, and the hex dump."""

import socket
import subprocess
import time


def wait_label(pg, want, timeout=8000):
    """Wait for the start/stop button to reach a state, rather than sleeping blind."""
    pg.wait_for_function(
        "want => document.getElementById('cap-run').textContent.trim() === want",
        arg=want, timeout=timeout)


def smtp_chat_nc(port=25):
    """Traffic from a client that is not this test process.

    The app column is only proved by two different names on the same
    conversation, and a socket opened here would just say python3 at both
    ends.
    """
    subprocess.run(["nc", "-C", "-w", "2", "127.0.0.1", str(port)],
                   input=b"EHLO nc-probe\r\nQUIT\r\n", capture_output=True, timeout=10)


def smtp_chat(port=25):
    """Make some traffic on a port the capture is watching."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.recv(200)
        s.sendall(b"EHLO capture-probe\r\n")
        s.recv(400)
        s.sendall(b"QUIT\r\n")
        s.recv(100)


def test_packets(pg, P, services, no_capture):
    time.sleep(1.0)

    # the new FAB sits above the process one
    P("1 third FAB present", pg.is_visible("#fab-cap"))
    box_cap = pg.eval_on_selector("#fab-cap", "e=>e.getBoundingClientRect().top")
    box_ps = pg.eval_on_selector("#fab-ps", "e=>e.getBoundingClientRect().top")
    P("2 sits above the process FAB", box_cap < box_ps, f"{box_cap} < {box_ps}")

    pg.click("#fab-cap")
    time.sleep(1.2)
    P("3 opens the packets tab", pg.is_visible("#cappane")
      and pg.eval_on_selector("#tab-cap", "e=>e.classList.contains('on')"))
    P("4 settings: port, address, start", pg.is_visible("#cap-port")
      and pg.is_visible("#cap-addr") and pg.text_content("#cap-run").strip() == "start")

    # Not capturing yet. The buffer of an earlier capture survives a page
    # reload on purpose, so the status may read "stopped - N packets" rather
    # than "not capturing"; what matters is that nothing is running.
    status = pg.text_content("#ps-status")
    P("5 reports that nothing is capturing", not status.startswith("capturing")
      and pg.text_content("#cap-run").strip() == "start", status)

    # a port with something behind it
    pg.fill("#cap-port", "25")
    pg.select_option("#cap-addr", "127.0.0.1")
    pg.click("#cap-run")
    wait_label(pg, "stop")
    time.sleep(1.0)
    P("6 capture starts", pg.text_content("#cap-run").strip() == "stop",
      pg.text_content("#cap-run"))
    P("7 FAB marks it recording",
      pg.eval_on_selector("#fab-cap", "e=>e.classList.contains('rec')"))
    P("8 names the listener on that port", "25" in pg.text_content("#cap-listen")
      and "nothing is listening" not in pg.text_content("#cap-listen"),
      pg.text_content("#cap-listen"))

    smtp_chat()
    time.sleep(2.5)

    rows = pg.eval_on_selector_all(".crow", "e=>e.map(r=>r.textContent)")
    P("9 packets appear", len(rows) >= 4, f"{len(rows)} rows")
    P("10 both directions", any("▼" in r for r in rows) and any("▲" in r for r in rows),
      rows[:2])
    P("11 ports shown", all(":25" in r or ":25 " in r for r in rows[:3]), rows[:1])
    P("12 live counter", "capturing" in pg.text_content("#ps-status")
      and "packets" in pg.text_content("#ps-status"), pg.text_content("#ps-status"))

    # payload: click the packet carrying the EHLO we sent
    target = None
    for i, row in enumerate(rows):
        if "▲" in row or "▼" in row:
            target = i
    pg.eval_on_selector_all(
        ".crow", "e=>e.filter(r=>r.textContent.includes('B'))[0].click()")
    time.sleep(0.6)
    P("13 clicking a packet shows the dump", pg.is_visible("#cap-dump"),
      pg.text_content("#cap-dump")[:40])
    dump = pg.text_content("#cap-dump")
    P("14 dump is hex plus ascii", "00000" in dump or "(no payload)" in dump,
      dump.splitlines()[:3])

    # the SMTP banner has to be readable somewhere in the capture
    found = pg.evaluate("""() => {
      const rows = document.querySelectorAll('.crow');
      for (const r of rows) {
        r.click();
        const t = document.getElementById('cap-dump').textContent;
        if (t.includes('ESMTP') || t.includes('EHLO')) return t;
      }
      return '';
    }""")
    P("15 payload decodes to readable text", "ESMTP" in found or "EHLO" in found,
      [ln for ln in found.splitlines() if "ESMTP" in ln or "EHLO" in ln][:1])

    # filtering reuses the drawer's filter box
    pg.fill("#ps-filter", "udp")
    time.sleep(0.6)
    P("16 filter narrows the list",
      pg.eval_on_selector_all(".crow", "e=>e.length") < len(rows),
      pg.eval_on_selector_all(".crow", "e=>e.length"))
    pg.fill("#ps-filter", "")
    time.sleep(0.5)

    pg.click("#cap-run")
    wait_label(pg, "start")
    time.sleep(1.2)
    P("17 capture stops", pg.text_content("#cap-run").strip() == "start"
      and "stopped" in pg.text_content("#ps-status"), pg.text_content("#ps-status"))
    P("18 no sniffer left running",
      subprocess.run(["pgrep", "-f", "sniffer.py --port 25"],
                     capture_output=True).returncode != 0)

    pg.click("#cap-clear")
    time.sleep(0.4)
    P("19 clear empties the list", pg.eval_on_selector_all(".crow", "e=>e.length") == 0)

    # a port nobody is listening on is reported as such, not as a silent empty list
    pg.fill("#cap-port", "9")
    pg.click("#cap-run")
    wait_label(pg, "stop")
    time.sleep(1.0)
    P("20 honest about a dead port", "nothing is listening" in pg.text_content("#cap-listen"),
      pg.text_content("#cap-listen"))
    pg.click("#cap-run")
    wait_label(pg, "start")

    # out-of-range ports are refused rather than sent to the server
    pg.fill("#cap-port", "99225")
    pg.click("#cap-run")
    time.sleep(1.0)
    P("21 rejects a port above 65535", pg.text_content("#cap-run").strip() == "start",
      pg.text_content("#cap-run"))


def test_packet_columns(pg, P, services, no_capture):
    """The app column, and sorting by clicking a header."""
    time.sleep(1.0)
    pg.click("#fab-cap")
    time.sleep(1.0)
    pg.fill("#cap-port", "25")
    pg.click("#cap-run")
    wait_label(pg, "stop")
    time.sleep(0.8)

    smtp_chat_nc()
    smtp_chat_nc()
    time.sleep(2.5)

    heads = pg.eval_on_selector_all("#cap-head span", "e=>e.map(s=>s.textContent.trim())")
    P("1 a header per column", len(heads) == 7, heads)
    P("2 there is an app column", any(h.startswith("APP") for h in heads), heads)

    apps = pg.eval_on_selector_all(".crow .capp", "e=>e.map(s=>s.textContent.trim())")
    P("3 app column is filled in", sum(a not in ("", "—") for a in apps) >= 4,
      sorted(set(apps)))
    joined = " ".join(apps)
    P("4 names the server process", "python3" in joined, sorted(set(apps)))
    P("5 names the client process too", "nc" in joined, sorted(set(apps)))
    P("5b shows both ends of the conversation",
      any("nc" in a and "python3" in a for a in apps), sorted(set(apps)))
    P("6 tooltip names both ends",
      "received it" in (pg.get_attribute(".crow .capp", "title") or ""),
      pg.get_attribute(".crow .capp", "title"))

    # default order is arrival order
    first_ts = pg.eval_on_selector_all(".crow .cnum", "e=>e.map(s=>s.textContent)")[0]
    P("7 time column sorted ascending by default",
      pg.eval_on_selector("#cap-head span[data-k='ts']", "e=>e.classList.contains('sorted')"),
      first_ts)

    def lens():
        return [int(t.replace("B", "")) for t in
                pg.eval_on_selector_all(".crow span:last-child", "e=>e.map(s=>s.textContent)")]

    pg.click("#cap-head span[data-k='len']")
    time.sleep(0.5)
    asc = lens()
    P("8 sort by length ascending", asc == sorted(asc), asc[:6])

    pg.click("#cap-head span[data-k='len']")
    time.sleep(0.5)
    desc = lens()
    P("9 clicking again reverses", desc == sorted(desc, reverse=True), desc[:6])
    P("10 the active column is marked",
      pg.eval_on_selector("#cap-head span[data-k='len']", "e=>e.classList.contains('sorted')")
      and "↓" in pg.text_content("#cap-head span[data-k='len']"),
      pg.text_content("#cap-head span[data-k='len']"))

    pg.click("#cap-head span[data-k='app']")
    time.sleep(0.5)
    grouped = [a for a in pg.eval_on_selector_all(".crow .capp", "e=>e.map(s=>s.textContent.trim())")]
    P("11 sort by app groups the processes", grouped == sorted(grouped), grouped[:6])
    P("12 length column no longer marked",
      not pg.eval_on_selector("#cap-head span[data-k='len']", "e=>e.classList.contains('sorted')"))

    pg.click("#cap-head span[data-k='ts']")
    time.sleep(0.4)
    P("13 back to arrival order",
      pg.eval_on_selector("#cap-head span[data-k='ts']", "e=>e.classList.contains('sorted')"))

    pg.click("#cap-run")
    wait_label(pg, "start")
