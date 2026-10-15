"""Terminal-client fallbacks: offer only clients that are installed."""

import time

def test_clients(pg, P, services):
    time.sleep(1.2)
    pg.click("#fab-ps"); time.sleep(.7); pg.click("#tab-disc"); time.sleep(1.8)
    pg.click(".dtool:text-is('scan ports')"); time.sleep(.4)
    pg.fill(".dform [name='host']","127.0.0.1"); pg.fill(".dform [name='ports']","21,22,23,25,111")
    pg.click(".dform button"); time.sleep(3.5)

    # telnet: binary missing here, so the button must fall back to nc
    pg.click("[data-port='23']"); time.sleep(2.0)
    P("1 telnet button enabled via fallback", not pg.eval_on_selector("#svc-term","e=>e.disabled"))
    P("2 command is nc, not telnet", pg.get_attribute("#svc-term","title")=="nc -v 127.0.0.1 23",
      pg.get_attribute("#svc-term","title"))
    pg.click("#svc-term"); time.sleep(2.5)
    txt=pg.eval_on_selector(".xterm-rows","e=>e.innerText")
    P("3 runs and connects (no 'not found')", "command not found" not in txt and "FakeRouter" in txt,
      [l for l in txt.split("\n") if l.strip()][-2:])
    pg.keyboard.press("Control+C"); time.sleep(.8)

    # ftp: binary missing -> curl
    pg.click("#fab-ps"); time.sleep(1.0)
    pg.click("[data-port='21']"); time.sleep(2.5)
    P("4 ftp falls back to curl", "curl" in (pg.get_attribute("#svc-term","title") or ""),
      pg.get_attribute("#svc-term","title"))
    pg.click("#svc-back"); time.sleep(.3)

    # ssh: real binary
    pg.click("[data-port='22']"); time.sleep(3.5)
    P("5 ssh uses ssh", pg.get_attribute("#svc-term","title")=="ssh -p 22 127.0.0.1",
      pg.get_attribute("#svc-term","title"))
    pg.click("#svc-back"); time.sleep(.3)

    # smtp: nc
    pg.click("[data-port='25']"); time.sleep(3.0)
    t6=pg.get_attribute("#svc-term","title") or ""
    P("6 smtp picks a CRLF-capable client", t6.startswith("openssl s_client -starttls smtp")
      or t6.startswith("nc -C"), t6)
    pg.click("#svc-back"); time.sleep(.3)

    # nfs: mount.nfs missing and showmount missing -> disabled with an honest reason
    pg.click("[data-port='111']"); time.sleep(3.0)
    P("7 nfs button disabled", pg.eval_on_selector("#svc-term","e=>e.disabled"))
    P("8 says which clients are missing", "mount.nfs" in (pg.get_attribute("#svc-term","title") or ""),
      pg.get_attribute("#svc-term","title"))
    P("9 label changes", pg.text_content("#svc-term").strip()=="no client installed",
      pg.text_content("#svc-term"))
