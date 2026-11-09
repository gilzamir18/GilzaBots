"""Chromium flags for the embedded browser window.

The window is QtWebEngine on macOS and Linux and WebView2 on Windows
(PROJECT_DESIGN.md §2.16.1). Both are Chromium and both take ``--disable-gpu``,
but through different variables and for different reasons; the WebView2 case is
at the end of this docstring.

QtWebEngine
-----------

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

That is a real cost in compositing smoothness, and it is the default anyway
(`GPU_DEFAULT`): a slower window is preferred to one that can vanish with the
application inside it. The user can turn acceleration back on in Settings or,
for a single run, through ``GILZABOTS_DISABLE_GPU=0``; whichever mode a run used
is recorded in the crash log next to it, which is what makes "did the GPU cause
this?" an answerable question.

The Linux default is the one exception, and it predates this module: QtWebEngine's
GPU process can abort at startup when Mesa's Zink/EGL layer cannot acquire a DRM
render node, which is a blank window rather than a slow one. Setting the flags
variable by hand still wins there, as it always did.

WebView2 (Windows)
------------------
WebView2 runs Chromium's GPU service in ``msedgewebview2.exe``, a process of its
own: a driver fault there costs the window its GPU process, which Chromium
restarts, and not the GilzaBots process. The reason acceleration is opt-in under
QtWebEngine does not hold, so on Windows it is on by default
(`WEBVIEW2_GPU_DEFAULT`). It is a separate setting (``webview2_gpu``) because
every existing settings file already holds ``webengine_gpu: "off"`` (saving
writes the defaults), which would otherwise carry the QtWebEngine decision over
to an engine it was never about.

Turning it off appends ``--disable-gpu`` to
``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS``, which the WebView2 loader reads when
the browser process starts. Measured with WebView2 Runtime 157: the browser
process' command line carries pywebview's own arguments followed by these, so
the variable adds to them rather than replacing them.
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

# WebView2 (Windows) reads its extra Chromium arguments from this variable.
WEBVIEW2_ARGUMENTS_VAR = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"

GPU_AUTO = "auto"
GPU_OFF = "off"
GPU_MODES = (GPU_AUTO, GPU_OFF)
# The mode used when nothing was chosen: no saved value, or one that cannot be
# read. Hardware acceleration is opt-in under QtWebEngine (see the module docstring).
GPU_DEFAULT = GPU_OFF
# WebView2 keeps the driver out of the GilzaBots process, so there it is opt-out.
WEBVIEW2_GPU_DEFAULT = GPU_AUTO

# ui_settings keys, one per engine (see the WebView2 section of the docstring).
WEBENGINE_SETTING = "webengine_gpu"
WEBVIEW2_SETTING = "webview2_gpu"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

# What the variables this module writes held *before* it wrote them, so a process
# this one spawns can be given the environment the user actually has rather than
# the one this run decided on. See `pristine_environment`.
_APPLIED_OVER: dict[str, str | None] = {}


def uses_webview2(platform: str | None = None) -> bool:
    """Whether the desktop window on `platform` is WebView2 rather than QtWebEngine."""
    return (sys.platform if platform is None else platform) == "win32"


def engine_name(platform: str | None = None) -> str:
    return "webview2" if uses_webview2(platform) else "qtwebengine"


def gpu_setting_key(platform: str | None = None) -> str:
    """The ui_settings key that holds the graphics mode of this platform's engine."""
    return WEBVIEW2_SETTING if uses_webview2(platform) else WEBENGINE_SETTING


def default_gpu_mode(platform: str | None = None) -> str:
    return WEBVIEW2_GPU_DEFAULT if uses_webview2(platform) else GPU_DEFAULT


def normalize_gpu_mode(value: Any, default: str = GPU_DEFAULT) -> str:
    """Coerce a stored or user-supplied value into a valid mode.

    Anything unrecognised becomes `default` (`GPU_DEFAULT` unless the caller's
    engine has another), the same mode a missing value gets, so a corrupted
    settings file behaves like a fresh one.
    """
    text = str(value or "").strip().lower()
    if text in GPU_MODES:
        return text
    if text in _TRUE:  # "disable gpu: true", written by an older or hand-edited file
        return GPU_OFF
    # "disable gpu: false". `False` needs its own test: `value or ""` turns it into "".
    if value is False or text in _FALSE:
        return GPU_AUTO
    return default


def saved_gpu_mode(settings: Mapping[str, Any] | None, platform: str | None = None) -> str:
    """The mode saved for this platform's engine, or its default when none is."""
    default = default_gpu_mode(platform)
    if not settings:
        return default
    return normalize_gpu_mode(settings.get(gpu_setting_key(platform)), default)


def resolve_gpu_mode(
    env: Mapping[str, str] | None = None,
    settings: Mapping[str, Any] | None = None,
    platform: str | None = None,
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

    key = gpu_setting_key(platform)
    if settings is not None and settings.get(key) is not None:
        return normalize_gpu_mode(settings.get(key), default_gpu_mode(platform)), "settings"
    return default_gpu_mode(platform), "default"


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

    if uses_webview2(platform):
        return _apply_webview2_environment(env, platform, settings)

    mode, source = resolve_gpu_mode(env, settings, platform)
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
        "engine": "qtwebengine",
        "gpu_mode": mode,
        "source": source,
        "reason": reason,
        "flags_var": CHROMIUM_FLAGS_VAR,
        "chromium_flags": env.get(CHROMIUM_FLAGS_VAR, ""),
        "qt_quick_backend": env.get(QUICK_BACKEND_VAR, ""),
        "changed": (
            env.get(CHROMIUM_FLAGS_VAR, "") != (before or "")
            or env.get(QUICK_BACKEND_VAR, "") != (quick_before or "")
        ),
    }


def _apply_webview2_environment(
    env: MutableMapping[str, str],
    platform: str,
    settings: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The Windows half of `apply_webengine_environment` (see the docstring)."""
    mode, source = resolve_gpu_mode(env, settings, platform)
    before = env.get(WEBVIEW2_ARGUMENTS_VAR)
    _APPLIED_OVER.clear()
    _APPLIED_OVER[WEBVIEW2_ARGUMENTS_VAR] = before
    flags = before or ""
    reason = ""

    if mode == GPU_OFF:
        flags = _append_flag(flags, DISABLE_GPU_FLAG)
        reason = f"GPU acceleration turned off ({source})"

    if flags:
        env[WEBVIEW2_ARGUMENTS_VAR] = flags

    return {
        "engine": "webview2",
        "gpu_mode": mode,
        "source": source,
        "reason": reason,
        "flags_var": WEBVIEW2_ARGUMENTS_VAR,
        "chromium_flags": env.get(WEBVIEW2_ARGUMENTS_VAR, ""),
        "qt_quick_backend": "",
        "changed": env.get(WEBVIEW2_ARGUMENTS_VAR, "") != (before or ""),
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
    ``QTWEBENGINE_CHROMIUM_FLAGS`` (or ``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS``)
    alive across a restart. It also drops the
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
