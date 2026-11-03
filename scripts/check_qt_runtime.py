#!/usr/bin/env python
"""Refuse to package a Qt that is known to break GilzaBots.

The Qt a user runs is decided at build time, not at install time: install.ps1
and install.sh download the release this pipeline produces and never resolve a
Python package themselves. The floor declared in pyproject.toml therefore
protects users only if the build confirms it — `pip install .` upgrades a
too-old Qt, but nothing checked what PyInstaller was actually about to copy.

Three checks, the cheap ones first:

1. The Qt floors declared in pyproject.toml (the file the build installs from)
   against the installed distributions.
2. All four Qt packages from the same Qt minor release. PyQt6-WebEngine does
   not constrain PyQt6-Qt6, so PyQt6 6.12 installed next to WebEngine 6.11 and
   QtWebEngineWidgets failed to load on Windows.
3. The failure itself, on Windows: a child process imports QtWebEngine and
   *then* onnxruntime — the order the application loads them in. Qt 6.10.2
   ships MSVCP140 14.26; Windows binds a DLL name once per process, so the Qt
   copy served onnxruntime too, and the first agent message died in
   MSVCP140.dll. A version number can only stand in for that failure; this
   reproduces it. It is skipped elsewhere, where there is no MSVC runtime to
   conflict and a headless runner may lack the libraries a QtWebEngine import
   needs.

    python scripts/check_qt_runtime.py

Exit codes: 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Callable

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent

QT_PACKAGES = ("pyqt6", "pyqt6-qt6", "pyqt6-webengine", "pyqt6-webengine-qt6")

# The application's own load order: the window (Qt) exists long before the first
# agent message pulls in ChromaDB's embedding function, and with it onnxruntime.
IMPORT_ORDER_PROBE = "import PyQt6.QtWebEngineWidgets\nimport onnxruntime\n"


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def qt_floors(pyproject: Path = ROOT / "pyproject.toml") -> dict[str, Version]:
    """The minimum version pyproject.toml declares for each Qt package.

    Qt belongs to the ``gui`` extra, not to the base install: the base install is
    the command-line interface and never imports it. The build installs
    ``.[gui]``, so that is the list this gate has to read.
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    floors: dict[str, Version] = {}
    for line in data["project"]["optional-dependencies"]["gui"]:
        req = Requirement(line)
        name = _normalize(req.name)
        if name in QT_PACKAGES:
            mins = [Version(s.version) for s in req.specifier if s.operator in (">=", "==")]
            if mins:
                floors[name] = max(mins)
    return floors


def version_problems(
    floors: dict[str, Version],
    installed: Callable[[str], str] = metadata.version,
) -> list[str]:
    problems = []
    for name in QT_PACKAGES:
        floor = floors.get(name)
        if floor is None:
            problems.append(f"pyproject.toml declares no floor for {name}")
            continue
        try:
            found = Version(installed(name))
        except metadata.PackageNotFoundError:
            problems.append(f"{name} is not installed (needs >={floor})")
            continue
        if found < floor:
            problems.append(f"{name} {found} is installed, below the declared floor {floor}")
    return problems


def mixed_release_problem(installed: Callable[[str], str] = metadata.version) -> str | None:
    """Report Qt packages that do not all come from the same Qt minor release.

    PyQt6-WebEngine pins its own Qt binaries but not PyQt6-Qt6, so a new PyQt6
    minor installs next to the previous WebEngine. On Windows that pair failed
    to load QtWebEngineWidgets ("The specified procedure could not be found").
    """
    versions = {name: Version(installed(name)) for name in QT_PACKAGES}
    if len({v.release[:2] for v in versions.values()}) == 1:
        return None
    found = ", ".join(f"{name} {v}" for name, v in versions.items())
    return (
        f"the Qt packages come from different Qt releases ({found}). PyQt6-WebEngine "
        "is built against the Qt of its own release, so all four must share one "
        "major.minor; pyproject.toml declares a common ceiling for that."
    )


def import_order_problem(
    python: str = sys.executable,
    platform: str = sys.platform,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str | None:
    """Import Qt and then onnxruntime in a child, and report how it ended."""
    if platform != "win32":
        return None
    result = runner(
        [python, "-c", IMPORT_ORDER_PROBE],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode == 0:
        return None
    code = result.returncode & 0xFFFFFFFF
    if code >= 0xC0000000:
        how = f"crashed natively (exit 0x{code:08X}), which is the C++ runtime conflict"
    else:
        how = f"failed (exit {result.returncode})"
    tail = (result.stderr or "").strip().splitlines()[-3:]
    detail = f": {' | '.join(tail)}" if tail else ""
    if 'File "<string>", line 1,' in (result.stderr or ""):
        # The first line of the probe is the Qt import: onnxruntime never ran.
        return "importing QtWebEngine " + how + detail + ". The application window could not start."
    return (
        "importing onnxruntime after QtWebEngine " + how + detail +
        ". The application loads them in this order, so its first agent message would die the same way."
    )


def main() -> int:
    problems = version_problems(qt_floors())
    if not problems:
        mixed = mixed_release_problem()
        if mixed:
            problems.append(mixed)
    if not problems:
        order = import_order_problem()
        if order:
            problems.append(order)

    if not problems:
        installed = ", ".join(f"{name} {metadata.version(name)}" for name in QT_PACKAGES)
        print(f"Qt runtime check passed: {installed}")
        return 0

    on_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for problem in problems:
        print(f"::error::{problem}" if on_actions else f"ERROR: {problem}", file=sys.stderr)
    print(
        "Refusing to package this Qt. See tests/test_dependency_floors.py for the "
        "verified Qt range and what broke outside it.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
