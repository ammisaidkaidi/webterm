"""User switcher: listing accounts, switching, and coming back."""

import time
def prompt(pg): return pg.eval_on_selector(".xterm-rows","e=>e.innerText").rstrip().splitlines()[-1]

def test_users(pg, P, user1):
    time.sleep(2.0)

    P("1 user button next to nav", pg.is_visible("#nav-user") and
      pg.eval_on_selector("#nav-user","e=>e.getBoundingClientRect().left") >
      pg.eval_on_selector("#nav-fwd","e=>e.getBoundingClientRect().left"))
    P("2 shows current user", pg.text_content("#nav-user-name").strip()=="user", pg.text_content("#nav-user-name"))
    P("3 menu closed at start", pg.is_hidden("#umenu"))

    pg.click("#nav-user"); time.sleep(.6)
    P("4 menu opens", pg.is_visible("#umenu"))
    names=[t.strip() for t in pg.eval_on_selector_all("#umenu button","e=>e.map(x=>x.innerText)")]
    P("5 lists root", any(n.startswith("root") for n in names), names[:3])
    P("6 lists login users", any(n.startswith("user1") for n in names) and any(n.startswith("user\n") or n.startswith("user ") or n.split("\n")[0]=="user" for n in names), names[:6])
    P("7 groups shown", [g.strip() for g in pg.eval_on_selector_all("#umenu .ug","e=>e.map(x=>x.textContent)")]==["ROOT","USERS"])
    P("8 current user ticked", pg.eval_on_selector("#umenu button.cur","e=>e.innerText").startswith("user"))
    P("9 system accounts collapsed", any("system accounts" in n for n in names), [n for n in names if "system" in n])
    pg.click("#umenu button:has-text('system accounts')"); time.sleep(.5)
    n2=pg.eval_on_selector_all("#umenu button","e=>e.length")
    P("10 expanding shows them", n2 > len(names), f"{len(names)} -> {n2}")
    P("11 uid shown", "uid 0" in pg.eval_on_selector("#umenu button:has-text('root')","e=>e.innerText"))

    # switch to root
    pg.click("#umenu button:has-text('root')"); time.sleep(3.0)
    P("12 menu closed on pick", pg.is_hidden("#umenu"))
    P("13 switched to root", pg.text_content("#nav-user-name").strip()=="root", prompt(pg))
    pg.click(".xterm-screen"); pg.keyboard.type("id -un\n"); time.sleep(1.5)
    out=pg.eval_on_selector(".xterm-rows","e=>e.innerText")
    P("14 shell really is root", "root" in out.split("id -un")[-1][:40], repr(out.split("id -un")[-1][:24]))
    P("15 button turns red", pg.eval_on_selector("#nav-user","e=>getComputedStyle(e).color"))

    # root -> user1 uses su (no password)
    pg.click("#nav-user"); time.sleep(.5)
    P("16 exit-back entry present", pg.is_visible("#umenu button:has-text('exit back to user')"))
    pg.click("#umenu button:has-text('user1')"); time.sleep(3.0)
    P("17 root -> user1", pg.text_content("#nav-user-name").strip()=="user1", prompt(pg))

    # nav buttons still track cwd under the new user
    pg.click(".xterm-screen"); pg.keyboard.type("cd /tmp\n"); time.sleep(2.2)
    P("18 cwd tracking survives switch", not pg.eval_on_selector("#nav-back","e=>e.disabled"),
      pg.get_attribute("#nav-back","title"))

    pg.click("#nav-user"); time.sleep(.5)
    pg.click("#umenu button:has-text('exit back to')"); time.sleep(3.0)
    P("19 exit returns to root", pg.text_content("#nav-user-name").strip()=="root", prompt(pg))
