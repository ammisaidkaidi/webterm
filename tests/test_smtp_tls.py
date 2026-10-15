"""SMTP over STARTTLS: detection, the openssl command, and a live session."""

import time

def test_smtp_tls(pg, P, services):
    time.sleep(1.2)
    pg.click("#fab-ps"); time.sleep(.7); pg.click("#tab-disc"); time.sleep(1.8)
    pg.click(".dtool:text-is('scan ports')"); time.sleep(.4)
    pg.fill(".dform [name='host']","127.0.0.1"); pg.fill(".dform [name='ports']","23,25")
    pg.click(".dform button"); time.sleep(3.0)

    pg.click("[data-port='25']"); time.sleep(3.5)
    body=pg.text_content("#svc-body")
    P("1 probe sees STARTTLS", "STARTTLS" in body and "yes" in body)
    P("2 probe shows the certificate", "fake.example" in body, [l for l in body.split("\n") if "fake" in l][:1])
    title=pg.get_attribute("#svc-term","title")
    P("3 starttls -> openssl s_client", title=="openssl s_client -starttls smtp -crlf -connect 127.0.0.1:25", title)

    pg.click("#svc-term"); time.sleep(4.0)
    txt=pg.eval_on_selector(".xterm-rows","e=>e.innerText")
    P("4 openssl actually negotiates TLS", "CN = fake.example" in txt or "CN=fake.example" in txt or "Verify return code" in txt,
      [l for l in txt.split("\n") if "CN" in l][:1])
    P("5 no 'command not found'", "command not found" not in txt)
    # the session is live: speak SMTP over the TLS channel
    pg.click(".xterm-screen"); pg.keyboard.type("EHLO probe\n"); time.sleep(2.0)
    txt=pg.eval_on_selector(".xterm-rows","e=>e.innerText")
    P("6 SMTP conversation works inside TLS", "250" in txt.split("EHLO probe")[-1],
      [l for l in txt.split("\n") if l.startswith("250")][:2])
    pg.keyboard.type("QUIT\n"); time.sleep(1.5)

    # plain (no starttls) port keeps using nc -C
    pg.click("#fab-ps"); time.sleep(1.0); pg.click("[data-port='23']"); time.sleep(2.0)
    P("7 telnet still uses nc -v", pg.get_attribute("#svc-term","title")=="nc -v 127.0.0.1 23",
      pg.get_attribute("#svc-term","title"))
