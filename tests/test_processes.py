"""Process viewer: listing, columns, sorting, filtering, kill, auto-refresh."""

import time
import subprocess

def test_processes(pg, P):
    time.sleep(.8)

    # two FABs, process one stacked above the file one
    P("1 both FABs present", pg.is_visible("#fab") and pg.is_visible("#fab-ps"))
    a=pg.query_selector("#fab").bounding_box(); c=pg.query_selector("#fab-ps").bounding_box()
    P("2 process FAB sits on top", c["y"]+c["height"] < a["y"] and abs(c["x"]-a["x"])<2, f"ps.y={c['y']:.0f} files.y={a['y']:.0f}")

    pg.click("#fab-ps"); time.sleep(1.4)
    P("3 panel opens", pg.evaluate("document.body.classList.contains('procs')"))
    rows=pg.eval_on_selector_all(".prow","e=>e.length")
    P("4 lists processes", rows>20, f"{rows} rows")
    hdr=[t.strip() for t in pg.eval_on_selector_all("#ps-head span","e=>e.map(x=>x.textContent)")]
    P("5 columns", hdr==["PID","USER","CPU%","MEM","S","COMMAND"], hdr)
    st=pg.text_content("#ps-status")
    P("6 status bar stats", "load" in st and "mem" in st and "cpu" in st, st[:90])

    # Ace theme palette applied
    bg=pg.evaluate("getComputedStyle(document.querySelector('.psdrawer')).backgroundColor")
    light=pg.evaluate("document.body.classList.contains('light-ace')")
    P("7 Ace palette", (bg=="rgb(255, 255, 255)" and light) or (bg=="rgb(40, 44, 52)" and not light), f"bg={bg} light-ace={light}")

    pid1=pg.eval_on_selector_all(".prow","e=>e.map(r=>r.textContent)")[0]
    P("8 mem human-readable", "K" in pid1 or "M" in pid1 or "B" in pid1, pid1[:60])

    # filter
    pg.fill("#ps-filter","python3"); time.sleep(.5)
    n=pg.eval_on_selector_all(".prow","e=>e.length")
    allcmd=pg.eval_on_selector_all(".prow .pcmd","e=>e.map(x=>x.textContent)")
    P("9 filter narrows", 0 < n < rows and all("python3" in c for c in allcmd), f"{n} rows")
    pg.fill("#ps-filter",""); time.sleep(.4)

    # sorting by PID
    pg.click("#ps-head span[data-k='pid']"); time.sleep(.4)
    pids=[int(x) for x in pg.eval_on_selector_all(".prow .ppid","e=>e.map(x=>x.textContent)")]
    P("10 sort by pid desc", pids==sorted(pids,reverse=True), pids[:5])
    pg.click("#ps-head span[data-k='pid']"); time.sleep(.4)
    pids=[int(x) for x in pg.eval_on_selector_all(".prow .ppid","e=>e.map(x=>x.textContent)")]
    P("11 click again asc", pids==sorted(pids), pids[:5])

    # kill a real process
    sp=subprocess.Popen(["sleep","400"]); time.sleep(1.2)
    pg.fill("#ps-filter","sleep 400"); time.sleep(2.4)
    found=pg.eval_on_selector_all(".prow .ppid","e=>e.map(x=>x.textContent)")
    P("12 new process appears", str(sp.pid) in found, found[:4])
    pg.click(f".prow:has-text('{sp.pid}')"); time.sleep(.3)
    P("13 selection shown", "selected" in pg.text_content("#ps-status"), pg.text_content("#ps-status")[-40:])
    pg.click("#ps-term"); time.sleep(1.2)
    P("14 term kills it", sp.poll() is not None, f"exit={sp.returncode}")

    # auto refresh is on by default
    P("15 auto-refresh on", pg.eval_on_selector("#ps-auto","e=>e.classList.contains('on')"))
    pg.click("#ps-auto"); time.sleep(.3)
    P("16 auto toggles off", not pg.eval_on_selector("#ps-auto","e=>e.classList.contains('on')"))

    pg.fill("#ps-filter",""); time.sleep(.5)

    # esc closes, ctrl+shift+P opens
    pg.keyboard.press("Escape"); time.sleep(.4)
    P("17 Esc closes", not pg.evaluate("document.body.classList.contains('procs')"))
    pg.keyboard.press("Control+Shift+P"); time.sleep(.8)
    P("18 Ctrl+Shift+P opens", pg.evaluate("document.body.classList.contains('procs')"))
    pg.click("#scrim", position={"x":20,"y":300}, force=True); time.sleep(.5)
    P("19 scrim closes", not pg.evaluate("document.body.classList.contains('procs')"))
    # file browser still independent
    pg.click("#fab"); time.sleep(.8)
    P("20 file browser unaffected", pg.evaluate("document.body.classList.contains('files')"))
