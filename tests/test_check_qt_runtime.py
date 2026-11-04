"""Tests for the build gate that refuses to package a Qt known to break GilzaBots.

install.ps1 and install.sh download what the release build produced, so the Qt a
user runs is chosen by the build. These tests hold the gate to the two failures
it exists for, and hold both build scripts to actually running it.
"""

import importlib.util
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("check_qt_runtime", ROOT / "scripts" / "check_qt_runtime.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def test_the_gate_reads_the_same_floor_the_repository_declares():
    from test_dependency_floors import VERIFIED

    assert gate.qt_floors() == VERIFIED


def test_a_qt_below_the_floor_is_refused():
    floors = gate.qt_floors()
    installed = {
        "pyqt6": "6.10.2",
        "pyqt6-qt6": "6.10.2",
        "pyqt6-webengine": "6.11.0",
        "pyqt6-webengine-qt6": "6.11.2",
    }
    problems = gate.version_problems(floors, installed=installed.__getitem__)
    assert len(problems) == 2
    assert any("pyqt6-qt6 6.10.2" in p and "6.11.2" in p for p in problems)


def test_a_missing_qt_package_is_refused():
    def installed(name):
        if name == "pyqt6-webengine-qt6":
            raise metadata.PackageNotFoundError(name)
        return "6.11.2"

    problems = gate.version_problems(gate.qt_floors(), installed=installed)
    assert problems == ["pyqt6-webengine-qt6 is not installed (needs >=6.11.2)"]


def test_the_runtime_conflict_is_reported_as_what_it_is():
    """Qt 6.10.2 then onnxruntime died with an access violation, exit 0xC0000005."""
    crashed = lambda *a, **k: subprocess.CompletedProcess(a[0], 3221225477, "", "")
    problem = gate.import_order_problem(python="python", platform="win32", runner=crashed)
    assert "crashed natively (exit 0xC0000005)" in problem
    assert "first agent message" in problem

    fine = lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", "")
    assert gate.import_order_problem(python="python", platform="win32", runner=fine) is None


def test_the_probe_imports_in_the_application_s_order():
    """Reversed, the same Qt 6.10.2 imports cleanly — the order is the defect."""
    seen = {}

    def runner(cmd, **kwargs):
        seen["code"] = cmd[-1]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    gate.import_order_problem(python="python", platform="win32", runner=runner)
    assert seen["code"].index("PyQt6.QtWebEngineWidgets") < seen["code"].index("onnxruntime")


def test_the_import_probe_does_not_run_where_the_conflict_cannot_happen():
    def runner(*a, **k):
        raise AssertionError("the probe must not run off Windows")

    assert gate.import_order_problem(python="python", platform="linux", runner=runner) is None


@pytest.mark.skipif(sys.platform != "win32", reason="the runtime conflict is Windows-only")
def test_this_environment_passes_the_real_probe():
    assert gate.import_order_problem() is None


@pytest.mark.parametrize("script", ["build_exe.ps1", "build_exe.sh"])
def test_every_build_script_runs_the_gate_before_packaging(script):
    text = (ROOT / script).read_text(encoding="utf-8")
    # The build installs the desktop extra: the base install has no Qt to gate.
    install = text.index('pip install ".[gui]"')
    check = text.index("scripts/check_qt_runtime.py")
    package = text.index("pyinstaller --name")
    assert install < check < package, (
        f"{script} must check the installed Qt after installing it and before packaging it"
    )
    if script.endswith(".ps1"):
        # PowerShell's ErrorActionPreference does not stop on a native exit code.
        after = text[check:package]
        assert "$LASTEXITCODE" in after, "build_exe.ps1 must stop when the gate fails"


def test_qt_packages_from_different_releases_are_refused():
    """The set pip resolved for v0.2.24, the day PyQt6 6.12 came out without WebEngine 6.12."""
    mixed = {
        "pyqt6": "6.12.0",
        "pyqt6-qt6": "6.12.0",
        "pyqt6-webengine": "6.11.0",
        "pyqt6-webengine-qt6": "6.11.2",
    }
    problem = gate.mixed_release_problem(installed=mixed.__getitem__)
    assert "different Qt releases" in problem
    assert "pyqt6-qt6 6.12.0" in problem and "pyqt6-webengine-qt6 6.11.2" in problem


def test_qt_packages_from_one_release_pass_whatever_their_patch_level():
    same = {
        "pyqt6": "6.11.0",
        "pyqt6-qt6": "6.11.2",
        "pyqt6-webengine": "6.11.0",
        "pyqt6-webengine-qt6": "6.11.2",
    }
    assert gate.mixed_release_problem(installed=same.__getitem__) is None


def test_a_failing_qt_import_is_not_blamed_on_onnxruntime():
    """The v0.2.24 build log: the probe's first line, the Qt import, is what failed."""
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "<string>", line 1, in <module>\n'
        "ImportError: DLL load failed while importing QtWebEngineWidgets: "
        "The specified procedure could not be found.\n"
    )
    failed = lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", stderr)
    problem = gate.import_order_problem(python="python", platform="win32", runner=failed)
    assert problem.startswith("importing QtWebEngine failed (exit 1)")
    assert "onnxruntime" not in problem
    assert "procedure could not be found" in problem
