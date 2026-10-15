"""Per-service views: FTP, SSH, SMTP, telnet, NFS and HTTP."""

import time

def test_services(pg, P, ctx, services):
    time.sleep(1.2)
    pg.click("#fab-ps"); time.sleep(.8); pg.click("#tab-disc"); time.sleep(2.0)

    pg.click(".dtool:text-is('scan ports')"); time.sleep(.4)
    pg.fill(".dform [name='host']","127.0.0.1"); pg.fill(".dform [name='ports']","21,22,23,25,111,7681")
    pg.click(".dform button")
    for _ in range(30):
        time.sleep(.5)
        if "open" in pg.text_content("#dout") and "running" not in pg.text_content("#dout"): break
    labels={b.get_attribute("data-port"): b.inner_text() for b in pg.query_selector_all("[data-kind]")}
    P("1 a button per open port", len(labels)>=5, labels)
    P("2 labels match the service", labels.get("21")=="ftp" and labels.get("22")=="ssh"
      and labels.get("23")=="telnet" and labels.get("25")=="smtp" and labels.get("111")=="nfs"
      and labels.get("7681")=="open", labels)

    # ---- FTP ----
    pg.click("[data-port='21']"); time.sleep(2.5)
    t=pg.text_content("#svc-body")
    P("3 ftp view lists files", "readme.txt" in t and "subdir" in t, t.split("\n")[0][:46])
    P("4 ftp title", pg.text_content("#svc-title").startswith("ftp · 127.0.0.1:21"))
    pg.click(".fitem[data-dir]"); time.sleep(2.0)
    P("5 ftp dir navigation", "/subdir" in pg.text_content("#svc-body"), pg.text_content("#svc-body")[:40])
    P("6 ftp download button present", bool(pg.query_selector("[data-get]")),
      pg.text_content("#svc-body")[:40])
    pg.click("#svc-back"); time.sleep(.5)
    P("7 back closes the view", pg.is_hidden("#svcview"))

    # ---- SSH ----
    pg.click("[data-port='22']"); time.sleep(4.0)
    t=pg.text_content("#svc-body")
    P("8 ssh banner", "OpenSSH" in t, [l for l in t.split("\n") if "SSH-2.0" in l][:1])
    P("9 ssh host key fingerprint", "SHA256:" in t and ("ed25519" in t or "rsa" in t))
    P("10 ssh auth methods", "ACCEPTED AUTH" in t)
    P("11 ssh algorithms listed", "CIPHERS" in t and "KEY EXCHANGE" in t)
    pg.click("#svc-back"); time.sleep(.4)

    # ---- SMTP ----
    pg.click("[data-port='25']"); time.sleep(3.5)
    t=pg.text_content("#svc-body")
    P("12 smtp capabilities", "SIZE" in t.upper() and "AUTH" in t.upper(), t[:50])
    P("13 smtp size limit parsed", "34.2 MB" in t or "MB" in t)
    P("14 smtp live console connected", "connected to 127.0.0.1:25" in t or "220" in t)
    pg.fill("#svc-input","EHLO probe"); pg.press("#svc-input","Enter"); time.sleep(1.5)
    P("15 smtp console round trip", "250" in pg.text_content("#svc-console"),
      pg.text_content("#svc-console").replace("\n"," ")[:56])
    pg.click("#svc-back"); time.sleep(.4)

    # ---- telnet ----
    pg.click("[data-port='23']"); time.sleep(2.5)
    P("16 telnet banner", "FakeRouter" in pg.text_content("#svc-console"),
      pg.text_content("#svc-console").replace("\n"," ")[:50])
    pg.fill("#svc-input","hello"); pg.press("#svc-input","Enter"); time.sleep(1.2)
    P("17 telnet echo", "you said: hello" in pg.text_content("#svc-console"))
    pg.click("#svc-back"); time.sleep(.4)

    # ---- NFS ----
    pg.click("[data-port='111']"); time.sleep(3.0)
    t=pg.text_content("#svc-body")
    P("18 nfs rpc programs", "portmapper" in t, t[:50])
    P("19 nfs honest about no mountd", "mountd" in t.lower())
    pg.click("#svc-back"); time.sleep(.4)

    # ---- http opens a tab ----
    with ctx.expect_page() as pop:
        pg.click("[data-port='7681']")
    np=pop.value; np.wait_for_load_state();
    P("20 http opens a new tab", "7681" in np.url, np.url)
    np.close()

    # ---- open in terminal ----
    pg.click("[data-port='22']"); time.sleep(3.0)
    pg.click("#svc-term"); time.sleep(2.0)
    txt=pg.eval_on_selector(".xterm-rows","e=>e.innerText")
    P("21 'open in terminal' sends ssh", "ssh -p 22 127.0.0.1" in txt,
      [l for l in txt.split("\n") if "ssh" in l][-1:])
    P("22 drawer closed for the terminal", not pg.eval_on_selector("body","e=>e.classList.contains('procs')"))
