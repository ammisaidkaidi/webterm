"""Service view details: the send line and timestamp formatting."""

import time

def test_service_view(pg, P, services):
    time.sleep(1.2)
    pg.click("#fab-ps"); time.sleep(.7); pg.click("#tab-disc"); time.sleep(1.8)
    pg.click(".dtool:text-is('scan ports')"); time.sleep(.4)
    pg.fill(".dform [name='host']","127.0.0.1"); pg.fill(".dform [name='ports']","21,23")
    pg.click(".dform button"); time.sleep(3)
    pg.click("[data-port='21']"); time.sleep(2.5)
    P("a ftp hides the send line", pg.is_hidden("#svc-line"))
    P("b timestamp formatted", "2026-10-15 09:" in pg.text_content("#svc-body"),
      [l for l in pg.text_content("#svc-body").split("\n") if "readme" in l][:1])
    pg.click("#svc-back"); time.sleep(.4)
    pg.click("[data-port='23']"); time.sleep(2.0)
    P("c telnet shows the send line", pg.is_visible("#svc-line"))
