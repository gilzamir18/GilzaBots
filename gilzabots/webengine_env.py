"""Chromium flags for the embedded QtWebEngine window.

QtWebEngine does not run its GPU service in a child process the way a stand-alone
Chromium does: on Windows the graphics driver is loaded into *this* process, so a
fault in it is a fault in GilzaBots — the window vanishes, the Python interpreter
never gets an exception, and the terminal stays empty (see `crash_report`). The
only defence the application has is to keep the driver out of the process, which
trades smooth compositing for a renderer that cannot take the process down with it.

**Two renderers use the GPU, and turning off one of them is not turning it off.**
``--disable-gpu`` stops *Chromium* from using it: WebGL disappears and the GPU
service logs that it could not create its GLES contexts, which is the expected
signature of the flag working. But Qt composites the web view into the window
through its own scene graph, which on Windows defaults to Direct3D 11 — so the
Intel user-mode driver stayed loaded. Measured on the machine that reported the
crashes (Iris Xe, driver 31.0.101.4502), in an off-screen QWebEngineView that
drew a 2D canvas and asked for WebGL:

=================================================  ==================================================
configuration                                      Intel driver DLLs loaded in the process
=================================================  ==================================================
GPU on                                             igd10um64xe, igd10iumd64, igc64, igdgmm64
``--disable-gpu``                                  igd10um64xe, igd10iumd64, igc64, igdgmm64
``--disable-gpu`` + ``QT_QUICK_BACKEND=software``  igd10um64xe
=================================================  ==================================================

The last row is what `GPU_OFF` now applies: the D3D11 user-mode driver, the shader
compiler (``igc64``) and the GPU memory manager (``igdgmm64``) are no longer
loaded. The one DLL left is pulled in when DXGI enumerates adapters, which
Chromium still does to collect GPU information. All three rows rendered the canvas
correctly. An earlier version of this module applied only the flag, and its claim
that the renderer could no longer take the process down was not true.

That is a real cost, so it is never applied silently: `GPU_OFF` is chosen by the
user, in Settings or through ``GILZABOTS_DISABLE_GPU``, and the choice is recorded
in the crash log next to the run it belongs to, which is what makes "did turning
it off help?" an answerable question.

The Linux default is the one exception, and it predates this module: QtWebEngine's
GPU process can abort at startup when Mesa's Zink/EGL layer cannot acquire a DRM
render node, which is a blank window rather than a slow one. Setting the flags
variable by hand still wins there, as it always did.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Mapping, MutableMapping

CHROMIUM_FLAGS_VAR = "QTWEBENGINE_CHROMIUM_FLAGS"
DISABLE_GPU_VAR = "GILZABOTS_DISABLE_GPU"
DISABLE_GPU_FLAG = "--disable-gpu"
# Qt's own scene graph, which composites the web view into the window. It must
# be set before the first QApplication exists, like the Chromium flags.
QUICK_BACKEND_VAR = "QT_QUICK_BACKEND"
QUICK_BACKEND_SOFTWARE = "software"

GPU_AUTO = "auto"
GPU_OFF = "off"
GPU_MODES = (GPU_AUTO, GPU_OFF)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

# What the variables this module writes held *before* it wrote them, so a process
# this one spawns can be given the environment the user actually has rather than
# the one this run decided on. See `pristine_environment`.
_APPLIED_OVER: dict[str, str | None] = {}


def normalize_gpu_mode(value: Any) -> str:
    """Coerce a stored or user-supplied value into a valid mode.

    Anything unrecognised becomes `GPU_AUTO`: a corrupted settings file must not
    leave the application permanently in software rendering with no explanation.
    """
    text = str(value or "").strip().lower()
    if text in GPU_MODES:
        return text
    if text in _TRUE:  # "disable gpu: true", written by an older or hand-edited file
        return GPU_OFF
    return GPU_AUTO


def resolve_gpu_mode(
    env: Mapping[str, str] | None = None,
    settings: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """Return ``(mode, source)``.

    The environment variable wins over the saved setting in both directions, so
    a user who cannot open Settings because the window keeps dying can still
    start the application, and one who wants the GPU back for a single run can
    ask for it without changing what is saved.
    """
    env = os.environ if env is None else env
    raw = str(env.get(DISABLE_GPU_VAR, "")).strip().lower()
    if raw in _TRUE:
        return GPU_OFF, "env"
    if raw in _FALSE:
        return GPU_AUTO, "env"

    if settings is not None and settings.get("webengine_gpu") is not None:
        return normalize_gpu_mode(settings.get("webengine_gpu")), "settings"
    return GPU_AUTO, "default"


def _append_flag(flags: str, flag: str) -> str:
    if flag in flags.split():
        return flags
    return f"{flags} {flag}".strip()


def apply_webengine_environment(
    env: MutableMapping[str, str] | None = None,
    platform: str | None = None,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Set the rendering environment for this process and report what was decided.

    User choices are never overridden: the Chromium flag is appended to whatever
    flags are already there, and a ``QT_QUICK_BACKEND`` the user set by hand is
    left as it is.
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform

    mode, source = resolve_gpu_mode(env, settings)
    before = env.get(CHROMIUM_FLAGS_VAR)
    quick_before = env.get(QUICK_BACKEND_VAR)
    _APPLIED_OVER.clear()
    _APPLIED_OVER[CHROMIUM_FLAGS_VAR] = before
    _APPLIED_OVER[QUICK_BACKEND_VAR] = quick_before
    flags = before or ""
    reason = ""

    if mode == GPU_OFF:
        flags = _append_flag(flags, DISABLE_GPU_FLAG)
        # Without this, Qt keeps compositing through Direct3D on the very driver
        # the user is trying to get out of the process (see the table above).
        if not quick_before:
            env[QUICK_BACKEND_VAR] = QUICK_BACKEND_SOFTWARE
        reason = f"GPU acceleration turned off ({source})"
    elif platform.startswith("linux") and before is None:
        # Historic default, kept verbatim in effect: QtWebEngine's GPU process
        # can segfault at startup when Mesa's Zink/EGL layer fails to acquire a
        # DRM render node (missing or inaccessible /dev/dri/render*). Users who
        # know GPU acceleration works can set the variable themselves.
        flags = _append_flag(flags, DISABLE_GPU_FLAG)
        reason = "Linux default: avoids the Mesa/Zink render-node path"

    if flags:
        env[CHROMIUM_FLAGS_VAR] = flags

    return {
        "gpu_mode": mode,
        "source": source,
        "reason": reason,
        "chromium_flags": env.get(CHROMIUM_FLAGS_VAR, ""),
        "qt_quick_backend": env.get(QUICK_BACKEND_VAR, ""),
        "changed": (
            env.get(CHROMIUM_FLAGS_VAR, "") != (before or "")
            or env.get(QUICK_BACKEND_VAR, "") != (quick_before or "")
        ),
    }


def pristine_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of `env` with this run's rendering decision taken back out of it.

    A restarted GilzaBots is a *child* of the process it replaces and inherits its
    environment — including the variables this module wrote, which is how a
    setting that only applies "at the next launch" managed to not apply at the
    next launch. Observed on 16/09/2026: a run with acceleration off wrote
    ``--disable-gpu`` into the environment, the user turned acceleration back on
    in Settings and accepted the restart, and the replacement process recorded
    ``webengine_gpu: auto`` in its own crash-log banner while still carrying the
    inherited ``--disable-gpu``. The app reported one thing and did another,
    which is exactly what the log exists to prevent.

    Restoring the values seen *before* this module touched them, rather than
    deleting the variables, is what keeps a user's own
    ``QTWEBENGINE_CHROMIUM_FLAGS`` alive across a restart. It also drops the
    flags pywebview appends afterwards, which is correct: the child appends its
    own, and inherited ones would accumulate with every restart.
    """
    env = os.environ if env is None else env
    fresh = dict(env)
    for name, original in _APPLIED_OVER.items():
        if original is None:
            fresh.pop(name, None)
        else:
            fresh[name] = original
    return fresh
