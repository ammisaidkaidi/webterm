#!/bin/sh
# Every check, in the order of how fast it fails. CI runs this same script.
#
#   sh dev/check.sh           syntax, lint and the inline script
#   sh dev/check.sh --tests   the above, then the browser suite
#
# The browser suite needs Playwright and a free port 7681, and skips the
# service tests unless dev/fixtures.py is running.
set -e
cd "$(dirname "$0")/.."

echo "==> python syntax"
python3 -c "import ast,sys; [ast.parse(open(f).read(), f) for f in sys.argv[1:]]" \
  server.py netscan.py dev/fixtures.py tests/*.py

echo "==> inline javascript"
# The whole UI lives in one inline <script> in index.html, so nothing else
# ever parses it. node --check is the only thing standing between a typo and
# a blank page. It does not catch temporal-dead-zone errors; the browser
# suite does, by failing on any page error.
python3 - <<'PY' > /tmp/webterm-inline.js
import re
html = open("static/index.html").read()
scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
print(max(scripts, key=len))
PY
node --check /tmp/webterm-inline.js

echo "==> hardcoded client binaries"
# Shipping a command for a binary nobody has is how `telnet: command not
# found` reached a user. Client commands come from /api/svc/tools.
if grep -nE '"(telnet|ftp|mount\.nfs|showmount|lftp|ncftp|smbclient) ' static/index.html \
     | grep -v 'tools\.' ; then
  echo "   ^ a client command that does not check /api/svc/tools first"
  exit 1
fi

echo "==> existence checks on paths the server may not own"
# os.path.isfile and isdir both return False on EPERM, which turns "you are
# not allowed to read this" into "this does not exist".
# Mark a deliberate use with "# eperm-ok: <why>" on the same line.
if grep -nE 'os\.path\.(isfile|isdir)\(' server.py netscan.py | grep -v 'eperm-ok'; then
  echo "   ^ os.path.isfile/isdir return False on EPERM, turning 'not allowed'"
  echo "     into 'does not exist'. Use os.stat and catch PermissionError, or"
  echo "     mark the line with '# eperm-ok: <why>' if a false negative is safe."
  exit 1
fi

if [ "$1" = "--tests" ]; then
  echo "==> ruff"
  ruff check server.py netscan.py dev tests
  echo "==> browser suite"
  python3 -m pytest tests -v
fi

echo "all checks passed"
