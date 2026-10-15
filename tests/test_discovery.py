"""Network discovery tab: all thirteen tools."""

import time
def run(pg, tool, fill=None, wait=4.0):
    pg.click(f".dtool:text-is('{tool}')"); time.sleep(.4)
    for name,val in (fill or {}).items():
        pg.fill(f".dform [name='{name}']", val)
    if pg.eval_on_selector_all(".dform input, .dform select","e=>e.length"):
        pg.click(".dform button")
    deadline=time.time()+wait
    while time.time()<deadline:
        t=pg.text_content("#dout")
        if "running…" not in t and t.strip() and t.strip()!="…": return t
        time.sleep(.3)
    return pg.text_content("#dout")

def test_discovery(pg, P):
    dialogs=pg.dialogs
    time.sleep(1.2)
    pg.click("#fab-ps"); time.sleep(1.0)

    P("1 third tab exists", pg.is_visible("#tab-disc"))
    pg.click("#tab-disc"); time.sleep(2.5)
    P("2 tab active + list hidden", pg.eval_on_selector("#tab-disc","e=>e.classList.contains('on')") and pg.is_hidden("#ps-list"))
    chips=[c.strip() for c in pg.eval_on_selector_all("#dtools .dtool","e=>e.map(x=>x.textContent)")]
    P("3 thirteen tools", len(chips)==13, chips)  # #dtools only: the service
      # header reuses the .dtool class for its own buttons
    t=pg.text_content("#dout")
    P("4 my network auto-runs", "INTERFACES" in t and "ROUTES" in t and "eth0" in t)
    P("5 shows primary ip + gateway", "169.254.0.21" in t and "169.254.0.22" in t)

    pg.click(".dtool:text-is('ping')"); time.sleep(1.2)
    P("5b selecting a tool does not auto-probe", len(dialogs)==0 and "press run" in pg.text_content("#dout"),
      pg.text_content("#dout")[:40])
    P("6 public IP", "35.197" in run(pg,"public IP",wait=10) or "AS" in pg.text_content("#dout"))
    P("7 neighbours", "MAC" in run(pg,"neighbours") )
    t=run(pg,"ping",{"host":"127.0.0.1","count":"3"},wait=10)
    P("8 ping local", "0%" in t and "ROUND TRIP" in t, t.split("\n")[0][:50])
    t=run(pg,"scan ports",{"host":"127.0.0.1","ports":"20-25,7681"},wait=12)
    P("9 port scan finds 22 + 7681", "7681" in t and "ssh" in t, t[:60])
    t=run(pg,"DNS",{"name":"github.com"},wait=10)
    P("10 dns A record", "via" in t and t.count(".")>3, t[:60])
    t=run(pg,"whois / ASN",{"target":"8.8.8.8"},wait=12)
    P("11 whois", "GOGL" in t or "handle" in t, t[:60])
    nd0=len(dialogs)
    t=run(pg,"HTTP check",{"url":"http://github.com"},wait=15)
    P("12a first public target asks once", len(dialogs)==nd0+1 and "github.com" in dialogs[-1],
      f"{len(dialogs)-nd0} new: " + (dialogs[-1][:46] if dialogs else "none"))
    P("12 http check + redirect chain", "200" in t and "301" in t and "HEADERS" in t, t[:50])
    t=run(pg,"banner / TLS",{"host":"github.com","port":"443"},wait=15)
    P("13 TLS cert parsed", "CN=github.com" in t and "expires" in t, t[:50])
    t=run(pg,"bandwidth",wait=6)
    P("14 bandwidth live", "eth0" in t and "/s" in t)
    before=pg.text_content("#dout"); time.sleep(3.2)
    P("15 bandwidth refreshes itself", pg.text_content("#dout")!=before or "live" in pg.text_content("#dout"))
    t=run(pg,"find hosts",wait=25)
    P("16 host sweep uses my subnet", "169.254" in t, t.split("\n")[0][:60])
    nd=len(dialogs)
    t=run(pg,"traceroute",{"host":"8.8.8.8","max_hops":"6"},wait=25)
    P("17 later public targets dont re-ask", len(dialogs)==nd, f"{len(dialogs)} dialogs total")
    P("18 traceroute hops", "169.254.0.22" in t or "reached" in t, t.split("\n")[0][:60])
    t=run(pg,"devices",wait=12)
    P("19 ssdp/mdns runs", "device" in t.lower() or "answered" in t, t[:60])

    pg.click("#tab-ps"); time.sleep(3.0)
    P("20a discovery pane hidden", pg.is_hidden("#dpane"))
    P("20b process list back", pg.eval_on_selector_all(".prow","e=>e.length")>20,
      f'{pg.eval_on_selector_all(".prow","e=>e.length")} rows, list display='+pg.eval_on_selector("#ps-list","e=>getComputedStyle(e).display"))
