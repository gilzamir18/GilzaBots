"""The Qt floor declares what was verified, not what happens to install.

`PyQt6>=6.8.0` was declared for a long time and was false on Windows in two
different ways, both found by installing the older releases while chasing a
graphics crash:

* Qt 6.8.2 embeds Chromium 122. pdf.js 5 calls ``URL.parse`` (Chrome 126), so
  opening a PDF threw and the whole window went grey.
* Qt 6.10.2 ships ``MSVCP140.dll`` 14.26. ``onnxruntime`` needs a newer C++
  runtime and brings none; Qt loads first, its runtime wins for the whole
  process, and the first agent message died importing onnxruntime.

6.11.2 passes both. The ``-Qt6`` packages are the ones that carry the runtime,
so they are held to the verified patch release rather than to PyQt6's own
``>=6.11.0``. Lowering any of these needs the same two checks run again.

The four also share one ceiling. PyQt6-WebEngine pins its own Qt binaries but
not PyQt6-Qt6, so when PyQt6 6.12 was published before PyQt6-WebEngine 6.12,
pip installed 6.12 next to WebEngine 6.11 and the Windows release build failed
to load QtWebEngineWidgets ("The specified procedure could not be found").
"""

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent

VERIFIED = {
    "pyqt6": Version("6.11.0"),
    "pyqt6-qt6": Version("6.11.2"),
    "pyqt6-webengine": Version("6.11.0"),
    "pyqt6-webengine-qt6": Version("6.11.2"),
}


def _floors(lines):
    floors = {}
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        req = Requirement(line)
        name = re.sub(r"[-_.]+", "-", req.name).lower()
        if name in VERIFIED:
            mins = [Version(s.version) for s in req.specifier if s.operator in (">=", "==")]
            floors[name] = max(mins) if mins else None
    return floors


def _ceilings(lines):
    ceilings = {}
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        req = Requirement(line)
        name = re.sub(r"[-_.]+", "-", req.name).lower()
        if name in VERIFIED:
            caps = [Version(s.version) for s in req.specifier if s.operator == "<"]
            ceilings[name] = min(caps) if caps else None
    return ceilings


def _requirements_txt():
    return _floors((ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines())


def _pyproject_gui_extra():
    """The Qt floors now live in the ``gui`` extra, not in the base install.

    The base install is the command-line interface and never imports Qt; the
    desktop application asks for it with ``pip install 'gilzabots[gui]'``. The
    floors themselves are unchanged and still have to match requirements.txt.
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return _floors(data["project"]["optional-dependencies"]["gui"])


def _pyproject_base():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return _floors(data["project"]["dependencies"])


def test_every_qt_package_is_declared_with_a_floor_in_both_files():
    for source, floors in (("requirements.txt", _requirements_txt()), ("pyproject.toml", _pyproject_gui_extra())):
        assert set(floors) == set(VERIFIED), f"{source} must declare all of {sorted(VERIFIED)}"
        assert all(floors.values()), f"{source} declares a Qt package without a floor"


def test_the_floor_is_never_below_the_verified_release():
    for source, floors in (("requirements.txt", _requirements_txt()), ("pyproject.toml", _pyproject_gui_extra())):
        for name, verified in VERIFIED.items():
            assert floors[name] >= verified, (
                f"{source}: {name}>={floors[name]} is below the verified {verified}; "
                "see this module's docstring for what broke below it"
            )


def test_both_files_agree():
    """A pip install reads pyproject.toml, a checkout requirements.txt."""
    assert _requirements_txt() == _pyproject_gui_extra()


def test_all_qt_packages_share_one_ceiling_in_both_files():
    """A shared ceiling is what keeps the four on the same Qt minor."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for source, lines in (
        ("requirements.txt", (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()),
        ("pyproject.toml", data["project"]["optional-dependencies"]["gui"]),
    ):
        ceilings = _ceilings(lines)
        assert set(ceilings) == set(VERIFIED), f"{source} must declare all of {sorted(VERIFIED)}"
        assert len(set(ceilings.values())) == 1 and None not in ceilings.values(), (
            f"{source}: every Qt package needs the same '<' ceiling, got {ceilings}"
        )
        ceiling = next(iter(ceilings.values()))
        assert ceiling.release[2:] in ((), (0,)), f"{source}: the ceiling must be a minor release, got {ceiling}"
        for name, verified in VERIFIED.items():
            assert verified.release[:2] < ceiling.release[:2], (
                f"{source}: {name}'s verified {verified} is not below the ceiling {ceiling}"
            )


def test_the_base_install_carries_no_qt():
    """`pip install gilzabots` must not pull several hundred MB the CLI never loads.

    Every Qt and pywebview import in the package sits inside a function of
    `gilzabots/ide_server.py`, so the base install stays Qt-free by construction;
    this keeps a dependency line from quietly putting it back.
    """
    assert _pyproject_base() == {}, (
        "Qt belongs to the 'gui' extra, not to [project].dependencies"
    )


def _gui_requirements():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for source, lines in (
        ("requirements.txt", (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()),
        ("pyproject.toml", data["project"]["optional-dependencies"]["gui"]),
    ):
        reqs = []
        for line in lines:
            line = line.split("#", 1)[0].strip()
            if line:
                reqs.append(Requirement(line))
        yield source, {re.sub(r"[-_.]+", "-", r.name).lower(): r for r in reqs}


def test_windows_installs_no_qt_and_declares_pythonnet():
    """The Windows window is WebView2 (PROJECT_DESIGN.md 2.16.1): no Qt there.

    pythonnet is what the WebView2 window and the WinForms tray icon import;
    it is declared, not left to arrive through pywebview's own marker.
    """
    windows = {"sys_platform": "win32"}
    for os_name in ("linux", "darwin"):
        assert Requirement("x; sys_platform != 'win32'").marker.evaluate({"sys_platform": os_name})
    for source, reqs in _gui_requirements():
        for name in (*VERIFIED, "qtpy"):
            marker = reqs[name].marker
            assert marker is not None and not marker.evaluate(windows), f"{source}: {name} must skip Windows"
            assert marker.evaluate({"sys_platform": "linux"}) and marker.evaluate({"sys_platform": "darwin"}), (
                f"{source}: {name} must stay on macOS and Linux"
            )
        pythonnet = reqs.get("pythonnet")
        assert pythonnet is not None, f"{source} must declare pythonnet"
        assert pythonnet.marker is not None and pythonnet.marker.evaluate(windows)
        assert not pythonnet.marker.evaluate({"sys_platform": "linux"})
