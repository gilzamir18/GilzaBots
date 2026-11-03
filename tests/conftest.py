import urllib.error

import pytest


@pytest.fixture(autouse=True)
def _no_provider_context_discovery(monkeypatch):
    """Keep "Auto" context-window discovery off the network in every test.

    Resolving an "Auto" catalog entry asks its provider for the model's window
    (gilzabots/context_discovery.py) and records the answer in the catalog. A
    test must neither depend on a live provider nor write a real answer into
    whatever catalog it happens to read, so every request fails as if the
    provider were unreachable. Tests of discovery itself pass their own
    transport or patch `_http_json` again.
    """
    from gilzabots import context_discovery

    def _offline(url, **_kwargs):
        raise urllib.error.URLError(f"network disabled in tests: {url}")

    monkeypatch.setattr(context_discovery, "_http_json", _offline)
    context_discovery.reset_discovery_state()
    yield
    context_discovery.reset_discovery_state()


@pytest.fixture(autouse=True)
def _english_backend_messages():
    """Start every test with the backend speaking English.

    The language is process-wide (`gilzabots.i18n._LANG`), and code under test
    changes it as it does in production: `/api/gilzabots/run` and `cli.main`
    adopt the requested language, falling back to the machine's
    (`config.DEFAULT_LANG`). A test that switched it, or ran one of those paths
    on a Portuguese machine, left every later test reading Portuguese, so
    assertions on English text failed only in a full run.
    """
    from gilzabots.i18n import set_lang

    set_lang("en")
    yield
    set_lang("en")


@pytest.fixture(autouse=True)
def _isolated_sandbox_config(monkeypatch, tmp_path_factory):
    """Read the Docker sandbox settings from an empty home in every test.

    `gilzabots.sandbox_config` reads `<gilzabots home>/sandbox.json` on every
    command, so a machine whose real settings enable the sandbox sent every
    `run_command` test into Docker (or failed it where Docker is unreachable).
    """
    from gilzabots import sandbox_config

    home = str(tmp_path_factory.mktemp("sandbox-home"))
    monkeypatch.setattr(sandbox_config, "get_gilzabots_home", lambda: home)


@pytest.fixture(autouse=True)
def _isolated_monitors(monkeypatch, tmp_path_factory):
    """Keep monitor state, login services and daemons out of every test.

    Every orchestrator turn takes a chat lease in `<gilzabots home>/monitors.db`
    (`agent_stdin._take_turn_lease`), and registering a monitor starts the
    monitor daemon and installs a login service (`monitors.service`). A test
    must touch neither the real data folder nor the user's systemd/autostart
    configuration, nor leave a daemon running. Spawns are recorded instead of
    made; tests of those paths read `monitors_spawned` or patch further.
    """
    from gilzabots.monitors import service, store

    root = tmp_path_factory.mktemp("monitors-home")
    db_path = str(root / "monitors.db")
    monkeypatch.setattr(store, "default_db_path", lambda: db_path)
    monkeypatch.setattr(service, "monitors_dir", lambda: str(root))
    monkeypatch.setattr(service, "autostart_backend", lambda: None)
    spawned: list = []

    class _FakeProcess:
        pid = 0

        def poll(self):
            return 0

    def _record_spawn(argv, **kwargs):
        spawned.append(list(argv))
        return _FakeProcess()

    monkeypatch.setattr(service, "spawn_detached", _record_spawn)
    monkeypatch.setattr(service, "daemon_running", lambda: False)
    return spawned
