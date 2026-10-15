"""Properties dialog and chmod on a root-owned file, through the sudo fallback."""

import time
import subprocess
mode=lambda p: subprocess.run(["stat","-c","%a %U",p],capture_output=True,text=True).stdout.strip()

def test_properties(pg, P, edt, root_file):
    time.sleep(.8)
    pg.click("#fab"); time.sleep(.9)
    pg.click("#f-crumbs button:nth-child(1)"); time.sleep(.4)
    pg.dblclick(".row:has-text('tmp')"); time.sleep(.5)
    pg.dblclick(".row:has-text('edt')"); time.sleep(.5)
    pg.click(".row:has-text('rootie.txt')", button="right"); time.sleep(.4)
    pg.click("#f-menu i:text('Properties')"); time.sleep(.9)
    P("1 properties opens", pg.is_visible(".modal"), "mode field="+pg.input_value("#p-oct"))
    P("2 shows root owner", "root" in pg.text_content("#p-body"))
    pg.fill("#p-oct","755"); pg.dispatch_event("#p-oct","input"); time.sleep(.3)
    pg.click("#p-apply"); time.sleep(1.5)
    P("3 chmod root file applies", mode("/tmp/edt/rootie.txt")=="755 root", mode("/tmp/edt/rootie.txt"))
    P("4 dialog closed, no error", not pg.is_visible(".modal"), pg.text_content("#status"))
