"""The Windows scripts must be pure ASCII, with no BOM.

Windows PowerShell 5.1 -- the one every Windows box ships with, and the one the installer
actually runs under -- reads a .ps1 as Windows-1252 unless the file carries a UTF-8 BOM.
A UTF-8 em dash then arrives as three mojibake characters, one of which reads as a quote
and terminates the string it is sitting in. That is a *parse* error, so the script dies
before its first line runs, and the message points at a line that looks fine in any editor.

This shipped once. It is invisible to a PowerShell 7 parse check, because 7 defaults to
UTF-8, so the only reliable guard is to forbid the bytes outright.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UTF8_BOM = b"\xef\xbb\xbf"


def _windows_scripts() -> list[Path]:
    found = sorted(REPO_ROOT.glob("scripts/*.ps1")) + sorted(REPO_ROOT.glob("*.bat"))
    assert found, "no Windows scripts found -- has the layout changed?"
    return found


@pytest.mark.parametrize("script", _windows_scripts(), ids=lambda p: p.name)
def test_windows_script_is_ascii_only(script: Path) -> None:
    raw = script.read_bytes()
    offenders: list[str] = []
    for lineno, line in enumerate(raw.split(b"\n"), start=1):
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:  # pragma: no cover - would also fail the check below
            offenders.append(f"line {lineno}: not valid UTF-8 either")
            continue
        for char in dict.fromkeys(c for c in text if ord(c) > 127):
            name = unicodedata.name(char, "unnamed")
            offenders.append(f"line {lineno}: U+{ord(char):04X} {name} ({char!r})")

    assert not offenders, (
        f"{script.relative_to(REPO_ROOT)} contains non-ASCII characters, which Windows "
        f"PowerShell 5.1 will read as mojibake:\n  " + "\n  ".join(offenders) + "\n"
        "Use plain ASCII: '-' for an em dash, '\"' for smart quotes, '...' for an ellipsis."
    )


@pytest.mark.parametrize("script", _windows_scripts(), ids=lambda p: p.name)
def test_windows_script_has_no_bom(script: Path) -> None:
    assert not script.read_bytes().startswith(UTF8_BOM), (
        f"{script.relative_to(REPO_ROOT)} starts with a UTF-8 BOM. An ASCII-only file does "
        "not need one, and a BOM in a .bat is echoed to the console as garbage on the "
        "first line."
    )


def test_winget_ids_are_spelled_as_their_manifests_spell_them() -> None:
    """`winget install --exact` matches ids case-sensitively.

    'tailscale.tailscale' returns "No package found matching input criteria" while
    'Tailscale.Tailscale' installs. That cost a round trip once.
    """
    setup = (REPO_ROOT / "scripts" / "setup-windows.ps1").read_text(encoding="ascii")
    for expected in ("Git.Git", "Python.Python.3.11", "Tailscale.Tailscale"):
        assert f"-WingetId '{expected}'" in setup, (
            f"expected the winget id {expected!r} in setup-windows.ps1, spelled exactly as "
            "the package manifest spells it"
        )
