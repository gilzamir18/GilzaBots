import urllib.error

import pytest


@pytest.fixture(autouse=True)
def _no_provider_context_discovery(monkeypatch):
    """Keep "Auto" context-window discovery off the network in every test.

    Resolving an "Auto" catalog entry asks its provider for the model's window
    (opalatex/context_discovery.py) and records the answer in the catalog. A
    test must neither depend on a live provider nor write a real answer into
    whatever catalog it happens to read, so every request fails as if the
    provider were unreachable. Tests of discovery itself pass their own
    transport or patch `_http_json` again.
    """
    from opalatex import context_discovery

    def _offline(url, **_kwargs):
        raise urllib.error.URLError(f"network disabled in tests: {url}")

    monkeypatch.setattr(context_discovery, "_http_json", _offline)
    context_discovery.reset_discovery_state()
    yield
    context_discovery.reset_discovery_state()


@pytest.fixture(autouse=True)
def _english_backend_messages():
    """Start every test with the backend speaking English.

    The language is process-wide (`opalatex.i18n._LANG`), and code under test
    changes it as it does in production: `/api/opalatex/run` and `cli.main`
    adopt the requested language, falling back to the machine's
    (`config.DEFAULT_LANG`). A test that switched it, or ran one of those paths
    on a Portuguese machine, left every later test reading Portuguese, so
    assertions on English text failed only in a full run.
    """
    from opalatex.i18n import set_lang

    set_lang("en")
    yield
    set_lang("en")


@pytest.fixture(autouse=True)
def _isolated_sandbox_config(monkeypatch, tmp_path_factory):
    """Read the Docker sandbox settings from an empty home in every test.

    `opalatex.sandbox_config` reads `<opalatex home>/sandbox.json` on every
    command, so a machine whose real settings enable the sandbox sent every
    `run_command` test into Docker (or failed it where Docker is unreachable).
    """
    from opalatex import sandbox_config

    home = str(tmp_path_factory.mktemp("sandbox-home"))
    monkeypatch.setattr(sandbox_config, "get_opalatex_home", lambda: home)
