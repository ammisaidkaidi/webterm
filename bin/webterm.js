#!/usr/bin/env node
// Launcher for the npm distribution of webterm.
//
// webterm itself is a Python application. This wrapper exists so it can be
// started with `npx @ammisaidkaidi/webterm`. It does three things:
//
//   1. locate a Python 3.9+ interpreter,
//   2. create a private virtualenv next to the installed package and install
//      requirements.txt into it, once, on first run,
//   3. exec server.py with every argument passed straight through.
//
// The virtualenv lives inside the package directory so that uninstalling the
// npm package removes it too. If that directory is read-only (a global install
// owned by root, for example), the environment variable WEBTERM_VENV can point
// somewhere writable instead.

"use strict";

const { spawnSync, spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const pkgRoot = path.resolve(__dirname, "..");
const venvDir = process.env.WEBTERM_VENV || path.join(pkgRoot, ".venv");
const venvPython = path.join(venvDir, "bin", "python");
const stamp = path.join(venvDir, ".requirements-installed");
const requirements = path.join(pkgRoot, "requirements.txt");

function fail(message, hint) {
  process.stderr.write("webterm: " + message + "\n");
  if (hint) process.stderr.write("         " + hint + "\n");
  process.exit(1);
}

// 1. Find an interpreter. PYTHON wins, then the usual names.
function findPython() {
  const candidates = [];
  if (process.env.PYTHON) candidates.push(process.env.PYTHON);
  candidates.push("python3", "python");

  for (const candidate of candidates) {
    const probe = spawnSync(
      candidate,
      ["-c", "import sys; print('%d.%d' % sys.version_info[:2]) if sys.version_info >= (3, 9) else sys.exit(1)"],
      { encoding: "utf8" }
    );
    if (probe.status === 0) return { exe: candidate, version: (probe.stdout || "").trim() };
  }
  return null;
}

// 2. Build the virtualenv once, then install requirements once.
function ensureEnvironment(python) {
  if (!fs.existsSync(venvPython)) {
    process.stderr.write("webterm: first run, creating a virtualenv in " + venvDir + "\n");
    const made = spawnSync(python, ["-m", "venv", venvDir], { stdio: "inherit" });
    if (made.status !== 0) {
      fail(
        "could not create a virtualenv in " + venvDir,
        "On Debian and Ubuntu this usually means the python3-venv package is missing, " +
          "or the install directory is not writable. Set WEBTERM_VENV to a writable path."
      );
    }
  }

  if (!fs.existsSync(stamp)) {
    process.stderr.write("webterm: installing Python dependencies, this happens once\n");
    const installed = spawnSync(
      venvPython,
      ["-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", requirements],
      { stdio: "inherit" }
    );
    if (installed.status !== 0) {
      fail("dependency installation failed", "Try again, or install " + requirements + " by hand.");
    }
    fs.writeFileSync(stamp, new Date().toISOString() + "\n");
  }
}

// 3. Hand over to the Python server.
function main() {
  const python = findPython();
  if (!python) {
    fail(
      "no Python 3.9 or newer interpreter was found",
      "webterm is a Python application; this npm package only launches it. " +
        "Install Python 3, or point the PYTHON environment variable at an interpreter."
    );
  }

  ensureEnvironment(python.exe);

  const server = path.join(pkgRoot, "server.py");
  if (!fs.existsSync(server)) fail("server.py is missing from " + pkgRoot);

  const child = spawn(venvPython, [server].concat(process.argv.slice(2)), { stdio: "inherit" });
  child.on("exit", (code, signal) => {
    if (signal) process.kill(process.pid, signal);
    else process.exit(code === null ? 1 : code);
  });
  for (const sig of ["SIGINT", "SIGTERM"]) {
    process.on(sig, () => child.kill(sig));
  }
}

main();
