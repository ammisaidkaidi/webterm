"""Notepad editor: open, edit, save, conflicts, read-only and binary files."""

import time
import os

def test_editor(pg, P, edt):
    time.sleep(.8)
    pg.click("#fab"); time.sleep(.9)
    pg.click("#f-crumbs button:nth-child(1)"); time.sleep(.5)
    pg.dblclick(".row:has-text('tmp')"); time.sleep(.6)
    pg.dblclick(".row:has-text('edt')"); time.sleep(.6)

    # 1 dbl-click a file opens the editor with content + syntax mode
    pg.dblclick(".row:has-text('hello.py')"); time.sleep(1.2)
    P("1 dblclick opens editor", pg.is_visible(".editor"))
    val=pg.evaluate("ace.edit('e-area').getValue()")
    P("2 content loaded", val=="line one\nline two\n", repr(val))
    P("3 meta line", "utf-8" in pg.text_content("#e-meta") and "LF" in pg.text_content("#e-meta"), pg.text_content("#e-meta"))
    mode=pg.evaluate("ace.edit('e-area').session.getMode().$id")
    P("4 python mode by ext", mode=="ace/mode/python", mode)

    # 2 edit -> dirty dot -> Ctrl+S saves to disk
    pg.evaluate("ace.edit('e-area').setValue('print(42)\\n', -1)"); time.sleep(.4)
    P("5 dirty indicator", pg.eval_on_selector("#e-dirty","e=>e.classList.contains('on')"))
    pg.keyboard.press("Control+s")
    pg.wait_for_function("document.querySelector('#e-status').textContent.startsWith('saved')", timeout=8000)
    disk=open("/tmp/edt/hello.py").read()
    P("6 Ctrl+S writes disk", disk=="print(42)\n", repr(disk))
    P("7 dirty cleared", not pg.eval_on_selector("#e-dirty","e=>e.classList.contains('on')"), pg.text_content("#e-status"))

    # 3 conflict detection: change file underneath, then save
    pg.evaluate("ace.edit('e-area').setValue('mine\\n', -1)"); time.sleep(.3)
    open("/tmp/edt/hello.py","w").write("theirs\n"); time.sleep(.2)
    pg.once("dialog", lambda d: d.dismiss())          # decline overwrite
    pg.click("#e-save"); time.sleep(1.0)
    P("8 conflict detected", open("/tmp/edt/hello.py").read()=="theirs\n", pg.text_content("#e-status"))
    pg.once("dialog", lambda d: d.accept())           # now force
    pg.click("#e-save")
    pg.wait_for_function("document.querySelector('#e-status').textContent.startsWith('saved')", timeout=8000)
    P("9 force overwrite", open("/tmp/edt/hello.py").read()=="mine\n")

    # 4 close with unsaved changes warns
    pg.evaluate("ace.edit('e-area').setValue('dirty\\n', -1)"); time.sleep(.3)
    pg.once("dialog", lambda d: d.dismiss())
    pg.click("#e-close"); time.sleep(.6)
    P("10 unsaved warn blocks close", pg.is_visible(".editor"))
    pg.once("dialog", lambda d: d.accept())
    pg.keyboard.press("Escape"); time.sleep(.6)
    P("11 Esc closes after confirm", not pg.is_visible(".editor"))
    P("12 drawer still open", pg.evaluate("document.body.classList.contains('files')"))

    # 5 binary refused
    pg.dblclick(".row:has-text('bin.dat')"); time.sleep(1.0)
    P("13 binary refused", not pg.is_visible(".editor") and "binary" in pg.text_content("#status"), pg.text_content("#status"))

    # 6 read-only file
    pg.dblclick(".row:has-text('ro.txt')"); time.sleep(1.0)
    P("14 read-only opens", pg.is_visible(".editor") and pg.evaluate("ace.edit('e-area').getReadOnly()"))
    P("15 save disabled", pg.is_disabled("#e-save"), pg.text_content("#e-status"))
    pg.click("#e-close"); time.sleep(.5)

    # 7 new file -> creates + opens editor
    pg.once("dialog", lambda d: d.accept("fresh.md"))
    pg.click("#f-newfile"); time.sleep(1.3)
    P("16 new file created", os.path.exists("/tmp/edt/fresh.md"))
    P("17 new file opens in editor", pg.is_visible(".editor") and pg.text_content("#e-name")=="fresh.md")
    md=pg.evaluate("ace.edit('e-area').session.getMode().$id")
    P("18 markdown mode", md=="ace/mode/markdown", md)
    pg.evaluate("ace.edit('e-area').setValue('# hi\\n', -1)")
    pg.keyboard.press("Control+s")
    pg.wait_for_function("document.querySelector('#e-status').textContent.startsWith('saved')", timeout=8000)
    P("19 save new file", open("/tmp/edt/fresh.md").read()=="# hi\n")

    # 8 wrap toggle + cursor pos
    pg.click("#e-wrap"); time.sleep(.3)
    P("20 wrap toggle", pg.evaluate("ace.edit('e-area').getOption('wrap')"))
    P("21 cursor readout", "Ln" in pg.text_content("#e-pos"), pg.text_content("#e-pos"))
    pg.click("#e-close"); time.sleep(.4)

    # 9 context menu still has insert path
    pg.click(".row:has-text('fresh.md')", button="right"); time.sleep(.4)
    items=pg.eval_on_selector_all("#f-menu i","e=>e.map(x=>x.textContent)")
    P("22 menu has Edit + Insert path", "Edit" in items and "Insert path" in items, items)
    pg.keyboard.press("Escape")
