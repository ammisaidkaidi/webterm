"""Back / forward / up navigation in the file explorer."""

import time
def cwd(pg):           # read the prompt's last line
    t=pg.eval_on_selector(".xterm-rows","e=>e.innerText").rstrip().splitlines()[-1]
    return t.split(":")[-1].replace("$","").strip()

def test_navigation(pg, P):
    time.sleep(1.5)

    P("1 three nav buttons in header", all(pg.is_visible(f"#{i}") for i in ["nav-back","nav-up","nav-fwd"]))
    P("2 nav sits left of the title",
      pg.eval_on_selector("#nav","e=>e.getBoundingClientRect().right") < pg.eval_on_selector("#status","e=>e.getBoundingClientRect().left"))
    P("3 back/fwd start disabled",
      pg.eval_on_selector("#nav-back","e=>e.disabled") and pg.eval_on_selector("#nav-fwd","e=>e.disabled"))

    pg.click(".xterm-screen")
    pg.keyboard.type("cd /tmp/edt\n"); time.sleep(2.0)
    pg.keyboard.type("cd /tmp/edt/deep/deeper\n");time.sleep(2.0)
    P("4 history recorded, back enabled", not pg.eval_on_selector("#nav-back","e=>e.disabled"))
    P("5 back tooltip names target", "/tmp/edt" in pg.get_attribute("#nav-back","title"), pg.get_attribute("#nav-back","title"))

    pg.click("#nav-back"); time.sleep(2.2)
    P("6 back goes to previous dir", cwd(pg)=="/tmp/edt", cwd(pg))
    P("7 forward now enabled", not pg.eval_on_selector("#nav-fwd","e=>e.disabled"))
    pg.click("#nav-back"); time.sleep(2.2)
    P("8 back again -> start dir", cwd(pg).endswith("webterm"), cwd(pg))
    P("9 back disabled at start of history", pg.eval_on_selector("#nav-back","e=>e.disabled"))

    pg.click("#nav-fwd"); time.sleep(2.2)
    P("10 forward retraces", cwd(pg)=="/tmp/edt", cwd(pg))
    pg.click("#nav-fwd"); time.sleep(2.2)
    P("11 forward to deepest", cwd(pg)=="/tmp/edt/deep/deeper", cwd(pg))
    P("12 forward disabled at the end", pg.eval_on_selector("#nav-fwd","e=>e.disabled"))

    pg.click("#nav-up"); time.sleep(2.2)
    P("13 up = parent dir", cwd(pg)=="/tmp/edt/deep", cwd(pg))
    P("14 up tooltip names parent", "/tmp/edt" in pg.get_attribute("#nav-up","title"), pg.get_attribute("#nav-up","title"))

    # a manual cd must drop the forward trail
    pg.click(".xterm-screen"); pg.keyboard.type("cd /\n"); time.sleep(2.2)
    P("15 manual cd clears forward", pg.eval_on_selector("#nav-fwd","e=>e.disabled"), cwd(pg))
    P("16 up disabled at /", pg.eval_on_selector("#nav-up","e=>e.disabled"), cwd(pg))

    # typed text must not be mangled by a nav click
    pg.keyboard.type("echo keepme"); time.sleep(.5)
    pg.click("#nav-back"); time.sleep(2.2)
    txt=pg.eval_on_selector(".xterm-rows","e=>e.innerText")
    P("17 half-typed line discarded", "keepmecd" not in txt and "keepme cd" not in txt, repr([l for l in txt.splitlines() if "keepme" in l][-1:]))
    P("18 nav still worked after typing", cwd(pg)=="/tmp/edt/deep", cwd(pg))
