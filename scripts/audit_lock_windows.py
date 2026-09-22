"""Would every locked package actually install on Windows/CPython 3.11?

Run this from anywhere with network access after changing dependencies:

    python scripts/audit_lock_windows.py requirements.lock

For each package the lock does not exclude from Windows, it asks PyPI whether that exact
version ships something installable on win_amd64: a win_amd64 wheel, or a pure-python
(-none-any) wheel. A package with neither forces pip to build from source on the user's
machine, which is how `uvloop` -- Linux and macOS wheels only, and a setup.py that raises
"uvloop does not support Windows at the moment" -- got as far as a real desktop before
anyone noticed.

This is not a unit test: it needs the network, so it is not in the pytest run. The offline
half of the guard lives in tests/test_requirements_lock.py, which checks the lock was
generated with --universal and therefore carries the platform markers that keep a
Linux-only package off a Windows box in the first place.
"""

import json
import re
import sys
import urllib.request

LOCK = sys.argv[1]
line_re = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s;]+)(?:\s*;\s*(.*?))?\s*\\?$")

pkgs = []
for line in open(LOCK):
    if line.startswith((" ", "#", "\t")):
        continue
    m = line_re.match(line.rstrip())
    if m:
        pkgs.append((m.group(1), m.group(2), (m.group(3) or "").strip()))

print(f"{len(pkgs)} packages in {LOCK}\n")
problems, excluded, ok = [], [], []
for name, version, marker in pkgs:
    if "sys_platform != 'win32'" in marker or "sys_platform == 'linux'" in marker:
        excluded.append(f"{name}=={version}")
        continue
    if "sys_platform == 'win32'" in marker:
        tag = " (Windows-only)"
    else:
        tag = ""
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        data = json.load(urllib.request.urlopen(url, timeout=25))
    except Exception as e:
        problems.append(f"{name}=={version}: could not query PyPI ({e})")
        continue
    files = [f["filename"] for f in data.get("urls", [])]
    wheels = [f for f in files if f.endswith(".whl")]
    pure = [f for f in wheels if f.endswith("-none-any.whl")]
    win = [f for f in wheels if "win_amd64" in f and ("cp311" in f or "py3" in f or "abi3" in f)]
    if pure:
        ok.append(f"{name}=={version}{tag}: pure-python wheel")
    elif win:
        ok.append(f"{name}=={version}{tag}: win_amd64 wheel")
    else:
        kinds = sorted({f.split("-")[-1] for f in wheels}) or ["sdist only"]
        problems.append(f"{name}=={version}{tag}: NO Windows wheel -- has {kinds}")

print("PROBLEMS ON WINDOWS:")
print("\n".join("  " + p for p in problems) if problems else "  none")
print(f"\nCorrectly excluded from Windows: {', '.join(excluded) or 'none'}")
print(f"\nInstallable on Windows: {len(ok)}/{len(pkgs) - len(excluded)}")
