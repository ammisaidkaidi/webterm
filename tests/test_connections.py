"""Connections tab and the parent-directory button."""

import time
import subprocess

def test_connections(pg, P):
    time.sleep(1.0)

    # ---- terminal: parent-folder button ----
    pg.click(".xterm-screen"); pg.keyboard.type("cd /tmp/edt\n"); time.sleep(.9)
    P("1 cd .. button exists", pg.is_visible("button.k[title*='Parent folder']"))
    pg.click("button.k[title*='Parent folder']"); time.sleep(1.0)
    after=pg.eval_on_selector(".xterm-rows","e=>e.innerText")
    P("2 button runs cd ..", "cd .." in after and after.rstrip().endswith("/tmp$"), repr(after.strip().splitlines()[-1][-28:]))
    # confirm the shell really moved, via the explorer's sync
    pg.click("#fab"); time.sleep(.8); pg.click("#f-sync"); time.sleep(.8)
    P("3 shell cwd is parent", pg.text_content("#f-crumbs").strip()=="/tmp", pg.text_content("#f-crumbs"))
    pg.keyboard.press("Escape"); time.sleep(.4)

    # ---- process panel tabs ----
    pg.click("#fab-ps"); time.sleep(1.3)
    P("4 two tabs", pg.is_visible("#tab-ps") and pg.is_visible("#tab-net"))
    P("5 processes tab active", pg.eval_on_selector("#tab-ps","e=>e.classList.contains('on')"))

    pg.click("#tab-net"); time.sleep(1.6)
    P("6 switch to connections", pg.eval_on_selector("#tab-net","e=>e.classList.contains('on')")
      and not pg.eval_on_selector("#tab-ps","e=>e.classList.contains('on')"))
    hdr=[t.strip() for t in pg.eval_on_selector_all("#net-head span","e=>e.map(x=>x.textContent)")]
    P("7 connection columns", hdr==["PROTO","LOCAL","REMOTE","STATE","SERVICE","PID / PROCESS"], hdr)
    rows=pg.eval_on_selector_all(".prow","e=>e.map(r=>r.innerText)")
    P("8 lists sockets", len(rows)>5, f"{len(rows)} sockets")
    P("9 shows our own listener", any("7681" in r and "python3" in r for r in rows),
      [r.replace("\n"," ")[:46] for r in rows if "7681" in r][:1])
    P("10 tcp and udp present", any(r.startswith("tcp") for r in rows) and any(r.startswith("udp") for r in rows))
    st=pg.text_content("#ps-status")
    P("11 status: sockets+listening", "sockets" in st and "listening" in st, st[:70])

    # service naming: start an http server on 80? use 8080 (http-alt)
    srv=subprocess.Popen(["python3","-m","http.server","8080","--bind","127.0.0.1"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5); pg.click("#ps-reload"); time.sleep(1.5)
    rows=pg.eval_on_selector_all(".prow","e=>e.map(r=>r.innerText)")
    P("12 new listener + service name", any("8080" in r and "http-alt" in r for r in rows),
      [r.replace("\n"," ")[:56] for r in rows if "8080" in r][:1])

    # filter by port
    pg.fill("#ps-filter","8080"); time.sleep(.6)
    n=pg.eval_on_selector_all(".prow","e=>e.length")
    P("13 filter by port", 0 < n < len(rows), f"{n} rows")

    # select a socket -> kill its owning process
    pg.click(".prow:has-text('8080')"); time.sleep(.4)
    P("14 selecting socket picks pid", "selected pid" in pg.text_content("#ps-status"), pg.text_content("#ps-status")[-28:])
    pg.click("#ps-term"); time.sleep(1.5)
    P("15 term from connections tab", srv.poll() is not None, f"exit={srv.returncode}")
    srv.kill()

    # sorting on connections
    pg.fill("#ps-filter",""); time.sleep(.6)
    pg.click("#net-head span[data-k='lport']"); time.sleep(.5)
    ports=[int(r.split("\n")[1].split(":")[-1]) for r in pg.eval_on_selector_all(".prow","e=>e.map(r=>r.innerText)")]
    P("16a sort desc on first click", ports==sorted(ports,reverse=True), ports[:5])
    pg.click("#net-head span[data-k='lport']"); time.sleep(.5)
    ports=[int(r.split("\n")[1].split(":")[-1]) for r in pg.eval_on_selector_all(".prow","e=>e.map(r=>r.innerText)")]
    P("16b click again -> asc", ports==sorted(ports), ports[:5])
    pg.click("#net-head span[data-k='proto']"); time.sleep(.5)
    protos=[r.split("\n")[0] for r in pg.eval_on_selector_all(".prow","e=>e.map(r=>r.innerText)")]
    P("16c sort by proto", protos==sorted(protos), protos[:4])

    pg.click("#tab-ps"); time.sleep(1.2)
    P("17 back to processes", pg.eval_on_selector_all(".prow","e=>e.length")>20 and pg.is_visible("#ps-head"))
