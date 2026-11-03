"""The startup splash screen and what keeps it early.

The desktop window opens on a splash page before the agent runtime (litellm,
~4 s) is imported, so the user sees the application at once instead of nothing
for five seconds. Two things make that possible and are guarded here: the
modules on the path to the window do not import litellm, and the splash page
and its two readers (ide_server.load_splash_page, vite.config.js inlineSplash)
agree on the page's contract.
"""

import os
import subprocess
import sys
from pathlib import Path

from gilzabots.ide_server import load_splash_page

ROOT = Path(__file__).resolve().parent.parent
SPLASH_SOURCE = ROOT / "gui_src" / "public" / "splash.html"


def _run(probe: str, cwd: Path = ROOT) -> str:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=cwd, env=env)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_the_path_to_the_window_does_not_import_litellm():
    """Everything imported before the window opens stays light.

    `gilzabots.config` used to import `gilzabots.tools` at module load, and with
    it agenticblocks and litellm, so the window could not appear before ~4 s.
    """
    probe = (
        "import gilzabots.cli, gilzabots.ide_server, gilzabots.ui_settings, "
        "gilzabots.webengine_env, gilzabots.crash_report, sys;"
        "print(sorted({m.split('.')[0] for m in sys.modules} & {'litellm', 'agenticblocks'}))"
    )
    assert _run(probe) == "[]"


def test_config_still_reads_the_working_directory_agents_yaml(tmp_path):
    """Without importing `tools`, the module-load defaults keep their source.

    No project can be set before `tools` is imported, and its
    get_project_path() would answer the working directory: that is where the
    local agents.yaml is still read from.
    """
    (tmp_path / ".gilzabots").mkdir()
    (tmp_path / ".gilzabots" / "agents.yaml").write_text("default: local/model-from-cwd\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    probe = (
        "import os; os.environ['GILZABOTS_HOME'] = %r;"
        "import gilzabots.config as c, sys;"
        "print(c.DEFAULT_MODEL, 'gilzabots.tools' in sys.modules)" % str(home)
    )
    assert _run(probe, cwd=tmp_path) == "local/model-from-cwd False"


def test_splash_page_is_filled_for_the_window(tmp_path):
    source = SPLASH_SOURCE.read_text(encoding="utf-8")
    (tmp_path / "splash.html").write_text(source, encoding="utf-8")
    (tmp_path / "splash-icon.png").write_bytes(b"\x89PNG fake")

    page = load_splash_page(str(tmp_path), "pt-BR")

    # The window shows this page before any server exists: nothing in it may
    # need fetching, and the placeholder in the top comment is not the image.
    assert 'src="data:image/png;base64,iVBORyBmYWtl"' in page
    assert 'src="__SPLASH_ICON__"' not in page
    assert 'data-splash-lang="pt-BR"' in page


def test_splash_language_is_escaped_and_optional(tmp_path):
    (tmp_path / "splash.html").write_text(SPLASH_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "splash-icon.png").write_bytes(b"x")

    assert 'data-splash-lang=""' in load_splash_page(str(tmp_path), "")
    page = load_splash_page(str(tmp_path), 'en"><script>')
    assert 'data-splash-lang="en&quot;&gt;&lt;script&gt;"' in page


def test_a_bundle_without_the_splash_opens_without_one(tmp_path):
    assert load_splash_page(str(tmp_path)) is None
    assert load_splash_page(None) is None


def test_splash_source_keeps_the_contract_its_readers_rely_on():
    source = SPLASH_SOURCE.read_text(encoding="utf-8")
    # ide_server.py: placeholders it fills, and the API _boot calls.
    assert source.count('src="__SPLASH_ICON__"') == 1
    assert 'data-splash-lang=""' in source
    assert "window.gilzabotsSplash" in source
    for phase in ("loading", "starting", "opening"):
        assert f"{phase}:" in source
    # vite.config.js inlineSplash: the pieces it copies into index.html.
    assert '<style id="gilzabots-splash-style">' in source
    assert source.count("<!-- splash:begin -->") == 1
    assert source.count("<!-- splash:end -->") == 1
    assert 'data-status="loading"' in source
    # The page starts in "loading" by itself; the window's background matches it.
    from gilzabots.ide_server import SPLASH_BACKGROUND
    assert f"background: {SPLASH_BACKGROUND}" in source
    assert (ROOT / "gui_src" / "public" / "splash-icon.png").is_file()
