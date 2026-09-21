"""The lockfile must resolve for every platform, not just the one that generated it.

`uv pip compile` without `--universal` resolves for the machine it runs on and drops the
environment markers. Generated on Linux, that produced a lock which pinned `uvloop`
unconditionally -- a package with Linux and macOS wheels only, whose setup.py raises
"uvloop does not support Windows at the moment" -- and which omitted `colorama`, which
uvicorn needs for coloured output on Windows and nowhere else.

The first of those failed the install on a real Windows desktop. The second would have
been a quieter wrong-looking console afterwards.

These checks are offline. `scripts/audit_lock_windows.py` is the online half: it asks PyPI
whether each locked version actually ships a Windows-installable artifact.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK = REPO_ROOT / "requirements.lock"

# name==version, optionally followed by "; markers", optionally a trailing backslash.
PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9_.\-]+)==(?P<version>[^\s;]+)(?:\s*;\s*(?P<marker>.*?))?\s*\\?$"
)


def _pins() -> dict[str, str]:
    """Every top-level pin in the lock, mapped to its marker string ('' when unmarked)."""
    pins: dict[str, str] = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        if line.startswith((" ", "\t", "#")):
            continue
        match = PIN.match(line.rstrip())
        if match:
            pins[match["name"].lower()] = (match["marker"] or "").strip()
    return pins


def test_lock_was_generated_universally() -> None:
    """uv records its own command in the header, so the flag is checkable."""
    header = "\n".join(LOCK.read_text(encoding="utf-8").splitlines()[:3])
    assert "--universal" in header, (
        "requirements.lock was generated without --universal, so it carries no platform "
        "markers and pins whatever happened to resolve on the generating machine. "
        "Regenerate with `make lock`."
    )


def test_lock_has_pins() -> None:
    pins = _pins()
    assert len(pins) > 20, f"only parsed {len(pins)} pins from the lock -- has its format changed?"


@pytest.mark.parametrize("package", ["uvloop"])
def test_posix_only_packages_are_excluded_from_windows(package: str) -> None:
    """A package with no Windows wheel must carry a marker keeping it off Windows."""
    marker = _pins().get(package)
    if marker is None:
        pytest.skip(f"{package} is no longer in the lock")
    assert "sys_platform != 'win32'" in marker, (
        f"{package} is pinned for Windows but has no Windows wheel, so pip will try to "
        f"build it from source and fail. Its marker is {marker!r}. Regenerate with "
        "`make lock` rather than editing the lock by hand."
    )


def test_windows_only_dependencies_survived_the_resolve() -> None:
    """A universal resolve keeps Windows-only packages that a Linux resolve drops."""
    marker = _pins().get("colorama")
    assert marker is not None, (
        "colorama is missing from the lock, which means the resolve was not universal: "
        "uvicorn needs it for coloured output on Windows, and a Linux-only resolve drops it."
    )
    assert "sys_platform == 'win32'" in marker, (
        f"colorama should be pinned for Windows only; its marker is {marker!r}"
    )
