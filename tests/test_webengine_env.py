"""Tests for the graphics mode handed to the embedded QtWebEngine window."""

from gilzabots.webengine_env import (
    CHROMIUM_FLAGS_VAR,
    DISABLE_GPU_FLAG,
    DISABLE_GPU_VAR,
    GPU_AUTO,
    GPU_DEFAULT,
    GPU_OFF,
    QUICK_BACKEND_SOFTWARE,
    QUICK_BACKEND_VAR,
    apply_webengine_environment,
    normalize_gpu_mode,
    pristine_environment,
    resolve_gpu_mode,
)


def test_hardware_acceleration_is_off_by_default():
    assert GPU_DEFAULT == GPU_OFF
    from gilzabots.ui_settings import _DEFAULTS
    assert _DEFAULTS["webengine_gpu"] == GPU_DEFAULT


def test_unknown_values_fall_back_to_the_default_mode():
    assert normalize_gpu_mode(None) == GPU_DEFAULT
    assert normalize_gpu_mode("") == GPU_DEFAULT
    assert normalize_gpu_mode("nonsense") == GPU_DEFAULT
    assert normalize_gpu_mode("OFF") == GPU_OFF
    assert normalize_gpu_mode("AUTO") == GPU_AUTO
    # A hand-edited or older settings file that wrote a "disable gpu" boolean.
    assert normalize_gpu_mode(True) == GPU_OFF
    assert normalize_gpu_mode(False) == GPU_AUTO
    assert normalize_gpu_mode("false") == GPU_AUTO


def test_the_environment_overrides_the_saved_setting_in_both_directions():
    settings = {"webengine_gpu": GPU_AUTO}
    assert resolve_gpu_mode({DISABLE_GPU_VAR: "1"}, settings) == (GPU_OFF, "env")

    settings = {"webengine_gpu": GPU_OFF}
    assert resolve_gpu_mode({DISABLE_GPU_VAR: "0"}, settings) == (GPU_AUTO, "env")
    assert resolve_gpu_mode({}, settings) == (GPU_OFF, "settings")
    assert resolve_gpu_mode({}, {}) == (GPU_OFF, "default")
    assert resolve_gpu_mode({}, None) == (GPU_OFF, "default")


def test_turning_the_gpu_off_adds_the_flag_on_any_platform():
    env = {}
    report = apply_webengine_environment(env, "win32", {"webengine_gpu": GPU_OFF})
    assert env[CHROMIUM_FLAGS_VAR] == DISABLE_GPU_FLAG
    assert report["gpu_mode"] == GPU_OFF
    assert report["source"] == "settings"
    assert report["reason"]


def test_using_the_gpu_leaves_windows_untouched():
    env = {}
    report = apply_webengine_environment(env, "win32", {"webengine_gpu": GPU_AUTO})
    assert CHROMIUM_FLAGS_VAR not in env
    assert report["changed"] is False


def test_user_supplied_flags_are_never_dropped():
    env = {CHROMIUM_FLAGS_VAR: "--enable-logging --v=1"}
    apply_webengine_environment(env, "win32", {"webengine_gpu": GPU_OFF})
    assert env[CHROMIUM_FLAGS_VAR].split() == ["--enable-logging", "--v=1", DISABLE_GPU_FLAG]

    # Applying it twice must not accumulate duplicates.
    apply_webengine_environment(env, "win32", {"webengine_gpu": GPU_OFF})
    assert env[CHROMIUM_FLAGS_VAR].split().count(DISABLE_GPU_FLAG) == 1


def test_nothing_chosen_runs_without_the_gpu_on_any_platform():
    for platform in ("win32", "linux", "darwin"):
        env = {}
        report = apply_webengine_environment(env, platform, {})
        assert report["gpu_mode"] == GPU_OFF
        assert report["source"] == "default"
        assert env[CHROMIUM_FLAGS_VAR] == DISABLE_GPU_FLAG
        assert env[QUICK_BACKEND_VAR] == QUICK_BACKEND_SOFTWARE


def test_linux_keeps_its_historic_auto_behaviour_and_its_escape_hatch():
    """With acceleration turned on, Linux still avoids the Mesa/Zink path unless
    the user set the Chromium flags by hand."""
    env = {}
    apply_webengine_environment(env, "linux", {"webengine_gpu": GPU_AUTO})
    assert env[CHROMIUM_FLAGS_VAR] == DISABLE_GPU_FLAG

    chosen = {CHROMIUM_FLAGS_VAR: "--use-gl=desktop"}
    apply_webengine_environment(chosen, "linux", {"webengine_gpu": GPU_AUTO})
    assert chosen[CHROMIUM_FLAGS_VAR] == "--use-gl=desktop"


def test_turning_the_gpu_off_also_takes_qt_compositing_off_the_driver():
    """``--disable-gpu`` alone left the Intel D3D11 driver loaded in the process.

    Chromium stops using the GPU, but Qt still composites the web view through
    Direct3D; measured on the reporting machine, all four Intel driver DLLs stayed
    loaded until the Qt Quick scene graph was switched to software as well.
    """
    env = {}
    report = apply_webengine_environment(env, "win32", {"webengine_gpu": GPU_OFF})
    assert env[QUICK_BACKEND_VAR] == QUICK_BACKEND_SOFTWARE
    assert report["qt_quick_backend"] == QUICK_BACKEND_SOFTWARE


def test_using_the_gpu_leaves_qt_compositing_alone():
    env = {}
    apply_webengine_environment(env, "win32", {"webengine_gpu": GPU_AUTO})
    assert QUICK_BACKEND_VAR not in env

    # The Linux default predates this and concerns only Chromium's GPU process.
    linux = {}
    apply_webengine_environment(linux, "linux", {"webengine_gpu": GPU_AUTO})
    assert QUICK_BACKEND_VAR not in linux


def test_a_qt_quick_backend_the_user_chose_is_kept():
    env = {QUICK_BACKEND_VAR: "d3d12"}
    report = apply_webengine_environment(env, "win32", {"webengine_gpu": GPU_OFF})
    assert env[QUICK_BACKEND_VAR] == "d3d12"
    assert env[CHROMIUM_FLAGS_VAR] == DISABLE_GPU_FLAG
    assert report["qt_quick_backend"] == "d3d12"


def test_a_restart_does_not_inherit_this_run_s_rendering_decision():
    """The bug this closes: "applies at the next launch" did not apply.

    The restarted app is a child of the old process, so it inherited the flags
    the old one had written. Turning acceleration back on in Settings produced a
    run that recorded ``webengine_gpu: auto`` while still carrying the inherited
    ``--disable-gpu`` — the app reporting one thing and doing another.
    """
    env = {"PATH": "/usr/bin"}
    apply_webengine_environment(env, "win32", {"webengine_gpu": GPU_OFF})
    assert env[CHROMIUM_FLAGS_VAR] == DISABLE_GPU_FLAG
    # pywebview appends its own flags after this module has run.
    env[CHROMIUM_FLAGS_VAR] += " --use-fake-ui-for-media-stream"

    fresh = pristine_environment(env)
    assert CHROMIUM_FLAGS_VAR not in fresh
    assert QUICK_BACKEND_VAR not in fresh
    assert fresh["PATH"] == "/usr/bin"

    # And the child, deciding for itself, now really does use the GPU.
    report = apply_webengine_environment(fresh, "win32", {"webengine_gpu": GPU_AUTO})
    assert report["chromium_flags"] == ""
    assert report["qt_quick_backend"] == ""


def test_flags_the_user_set_survive_a_restart():
    """Sanitising must not eat a variable the user exported themselves."""
    env = {CHROMIUM_FLAGS_VAR: "--enable-logging", QUICK_BACKEND_VAR: "d3d12"}
    apply_webengine_environment(env, "win32", {"webengine_gpu": GPU_OFF})

    fresh = pristine_environment(env)
    assert fresh[CHROMIUM_FLAGS_VAR] == "--enable-logging"
    assert fresh[QUICK_BACKEND_VAR] == "d3d12"
