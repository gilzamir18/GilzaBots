"""gilzabots.docker_sandbox / sandbox_config: settings, diagnostics and command routing."""
import asyncio
import json
import os
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agenticblocks.blocks.patterns.sandbox import DockerContainerManager
from gilzabots import docker_sandbox, sandbox_config


def _call(tool, *args, **kwargs):
    result = (getattr(tool, "_func", None) or tool)(*args, **kwargs)
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


@pytest.fixture
def project(tmp_path, monkeypatch):
    from gilzabots.tools import set_project_context

    root = tmp_path / "my_project"
    root.mkdir()
    set_project_context(SimpleNamespace(project_path=str(root)))
    # Only the project's own skill folders; the machine's are not part of the test.
    monkeypatch.setattr("gilzabots.skills.skill_search_dirs", lambda project_path="": [
        os.path.join(project_path, "skills"),
    ])
    return root


def test_sandbox_config_defaults_and_persistence():
    cfg = sandbox_config.load_config()
    assert cfg["enabled"] is False
    assert cfg["image"] == "python:3.12-slim"
    assert cfg["network"] == "bridge"
    assert sandbox_config.is_enabled() is False

    sandbox_config.save_config({
        "enabled": True,
        "image": "custom-python:latest",
        "network": "none",
    })

    assert sandbox_config.is_enabled() is True
    assert sandbox_config.get_sandbox_image() == "custom-python:latest"
    assert sandbox_config.get_sandbox_network() == "none"

    # Invalid network falls back to default
    sandbox_config.save_config({"network": "invalid_net"})
    assert sandbox_config.get_sandbox_network() == "bridge"
    # "host" would share the host's network stack, which the sandbox does not offer
    sandbox_config.save_config({"network": "host"})
    assert sandbox_config.get_sandbox_network() == "bridge"


def test_a_partial_save_keeps_the_other_settings():
    sandbox_config.save_config({"enabled": True, "image": "texlive/texlive", "network": "none"})
    sandbox_config.save_config({"enabled": False})
    assert sandbox_config.load_config() == {"enabled": False, "image": "texlive/texlive", "network": "none"}


def test_container_name_deterministic():
    p1 = "/path/to/projectA"
    p2 = "/path/to/projectB"
    name1 = docker_sandbox.container_name_for_project(p1)
    name1_repeat = docker_sandbox.container_name_for_project(p1)
    name2 = docker_sandbox.container_name_for_project(p2)

    assert name1 == name1_repeat
    assert name1.startswith("gilzabots-sandbox-")
    assert name1 != name2


def test_verify_docker_missing_binary(monkeypatch):
    monkeypatch.setattr(docker_sandbox, "find_docker_bin", lambda: None)
    monkeypatch.setattr(docker_sandbox, "_running_in_snap", lambda: False)

    res = docker_sandbox.verify_docker()
    assert res["ready"] is False
    assert res["daemon_running"] is False
    assert "not found on PATH" in res["problem"]


def test_verify_docker_missing_binary_in_snap(monkeypatch):
    monkeypatch.setattr(docker_sandbox, "find_docker_bin", lambda: None)
    monkeypatch.setattr(docker_sandbox, "_running_in_snap", lambda: True)

    res = docker_sandbox.verify_docker()
    assert res["ready"] is False
    assert "inside the Snap environment" in res["problem"]


def test_verify_docker_permission_denied_snap(monkeypatch):
    monkeypatch.setattr(docker_sandbox, "find_docker_bin", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_sandbox, "_running_in_snap", lambda: True)
    monkeypatch.setattr(docker_sandbox, "_snap_docker_plug_connected", lambda: False)
    monkeypatch.setattr(docker_sandbox, "_docker_group_pending", lambda: False)

    def fake_run(cmd, **kwargs):
        if "version" in cmd:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="permission denied while trying to connect to the docker API at unix:///var/run/docker.sock"
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = docker_sandbox.verify_docker()
    assert res["ready"] is False
    assert res["daemon_running"] is False
    assert "Snap confinement" in res["problem"]
    assert res["action_command"] == "sudo snap connect gilzabots:docker"


def test_verify_docker_permission_denied_linux(monkeypatch):
    monkeypatch.setattr(docker_sandbox, "find_docker_bin", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_sandbox, "_running_in_snap", lambda: False)
    monkeypatch.setattr(docker_sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(docker_sandbox, "_docker_group_pending", lambda: False)

    def fake_run(cmd, **kwargs):
        if "version" in cmd:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="permission denied while trying to connect to the docker API at unix:///var/run/docker.sock"
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = docker_sandbox.verify_docker()
    assert res["ready"] is False
    assert "Permission denied" in res["problem"]
    assert res["action_command"] == "sudo usermod -aG docker $USER"


def _denied(monkeypatch, in_snap, plug=None, pending=False):
    monkeypatch.setattr(docker_sandbox, "find_docker_bin", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_sandbox, "_running_in_snap", lambda: in_snap)
    monkeypatch.setattr(docker_sandbox, "_snap_docker_plug_connected", lambda: plug)
    monkeypatch.setattr(docker_sandbox, "_docker_group_pending", lambda: pending)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, "", "permission denied while trying to connect to the docker API at unix:///var/run/docker.sock"))
    return docker_sandbox.verify_docker()


def test_a_group_added_after_login_asks_for_a_new_session_not_usermod_again(monkeypatch):
    res = _denied(monkeypatch, in_snap=False, pending=True)
    assert "Log out and back in" in res["problem"]
    assert "restarting GilzaBots is not enough" in res["problem"]
    assert res["action_command"] == ""


def test_a_connected_snap_plug_moves_on_to_the_docker_group(monkeypatch):
    assert "Log out and back in" in _denied(monkeypatch, in_snap=True, plug=True, pending=True)["problem"]
    res = _denied(monkeypatch, in_snap=True, plug=True, pending=False)
    assert res["action_command"] == "sudo usermod -aG docker $USER"
    # snapctl could not tell: the plug stays the first suspect
    assert _denied(monkeypatch, in_snap=True, plug=None)["action_command"] == "sudo snap connect gilzabots:docker"


def test_only_the_gilzabots_snap_counts_as_snap_confinement(monkeypatch):
    monkeypatch.setenv("SNAP", "/snap/some-ide/7")
    monkeypatch.setenv("SNAP_NAME", "some-ide")
    assert docker_sandbox._running_in_snap() is False
    monkeypatch.setenv("SNAP_NAME", "gilzabots")
    assert docker_sandbox._running_in_snap() is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX groups")
def test_docker_group_pending_compares_the_group_file_with_this_process(monkeypatch):
    import grp
    import pwd

    me = pwd.getpwuid(os.getuid()).pw_name
    entry = SimpleNamespace(gr_gid=4242, gr_mem=[me])
    monkeypatch.setattr(grp, "getgrnam", lambda name: entry)
    monkeypatch.setattr(os, "getgroups", lambda: [1000])
    assert docker_sandbox._docker_group_pending() is True
    monkeypatch.setattr(os, "getgroups", lambda: [1000, 4242])
    assert docker_sandbox._docker_group_pending() is False
    entry.gr_mem = []
    monkeypatch.setattr(os, "getgroups", lambda: [1000])
    assert docker_sandbox._docker_group_pending() is False

def test_verify_docker_daemon_not_running(monkeypatch):
    monkeypatch.setattr(docker_sandbox, "find_docker_bin", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_sandbox, "_running_in_snap", lambda: False)

    def fake_run(cmd, **kwargs):
        if "version" in cmd:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="Is the docker daemon running?"
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = docker_sandbox.verify_docker()
    assert res["ready"] is False
    assert "daemon is not running" in res["problem"]


def test_verify_docker_image_missing_vs_present(monkeypatch):
    monkeypatch.setattr(docker_sandbox, "find_docker_bin", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_sandbox, "_running_in_snap", lambda: False)

    # 1. Daemon ok, image missing
    def fake_run_missing(cmd, **kwargs):
        if "version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout='{"Client":{}}', stderr="")
        if "inspect" in cmd and "image" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Error: No such image")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run_missing)
    res = docker_sandbox.verify_docker()
    assert res["daemon_running"] is True
    assert res["image_found"] is False
    assert res["ready"] is False
    assert "not downloaded yet" in res["problem"]
    assert "docker pull" in res["action_command"]

    # 2. Daemon ok, image present
    def fake_run_present(cmd, **kwargs):
        if "version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout='{"Client":{}}', stderr="")
        if "inspect" in cmd and "image" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run_present)
    res2 = docker_sandbox.verify_docker()
    assert res2["daemon_running"] is True
    assert res2["image_found"] is True
    assert res2["ready"] is True
    assert res2["problem"] == ""

def test_verify_docker_reports_an_error_with_blank_output(monkeypatch):
    monkeypatch.setattr(docker_sandbox, "find_docker_bin", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_sandbox, "_running_in_snap", lambda: False)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "\n"))
    res = docker_sandbox.verify_docker()
    assert res["problem"] == "Docker daemon is unreachable: exit code 1"


@pytest.mark.skipif(os.name == "nt", reason="POSIX uids")
def test_the_container_runs_as_the_user_unless_docker_is_rootless(monkeypatch):
    answers = {"info": subprocess.CompletedProcess([], 0, '["name=seccomp,profile=builtin"]', "")}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: answers["info"])
    monkeypatch.setattr(docker_sandbox, "_rootless_by_bin", {})
    assert docker_sandbox._container_user("/d") == f"{os.getuid()}:{os.getgid()}"

    monkeypatch.setattr(docker_sandbox, "_rootless_by_bin", {})
    answers["info"] = subprocess.CompletedProcess([], 0, '["name=seccomp","name=rootless"]', "")
    assert docker_sandbox._container_user("/d") is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX uids")
def test_a_failed_docker_info_is_not_remembered(monkeypatch):
    answers = [
        subprocess.CompletedProcess([], 1, "", "Cannot connect to the Docker daemon"),
        subprocess.CompletedProcess([], 0, '["name=rootless"]', ""),
    ]
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: answers.pop(0))
    monkeypatch.setattr(docker_sandbox, "_rootless_by_bin", {})
    docker_sandbox._container_user("/d")
    assert docker_sandbox._container_user("/d") is None
    assert docker_sandbox._container_user("/d") is None  # remembered: no third call


def test_the_project_container_mounts_the_project_and_outside_skill_folders(project, tmp_path, monkeypatch):
    outside = tmp_path / "user-skills"
    outside.mkdir()
    (project / "skills").mkdir()
    monkeypatch.setattr("gilzabots.skills.skill_search_dirs", lambda project_path="": [
        os.path.join(project_path, "skills"), str(outside), str(tmp_path / "absent"),
    ])
    monkeypatch.setattr(docker_sandbox, "find_docker_bin", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_sandbox, "_container_user", lambda _bin: "1000:1000")

    with patch.object(DockerContainerManager, "ensure_container", return_value=(True, "n")) as ensure:
        assert docker_sandbox.ensure_project_container() == (True, "n")
    kw = ensure.call_args.kwargs
    assert kw["volume_mount"] == str(project)
    assert [(m.source, m.readonly) for m in kw["mounts"]] == [(str(outside), True)]
    assert kw["mounts"][0].target.startswith("/gilzabots/skills/")
    assert (kw["user"], kw["env"], kw["pull"]) == ("1000:1000", {"HOME": "/tmp"}, "never")


def test_a_missing_image_points_to_the_pull_button(project, monkeypatch):
    monkeypatch.setattr(docker_sandbox, "find_docker_bin", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_sandbox, "_container_user", lambda _bin: None)
    failure = (False, "Failed to create container x: Error response from daemon: No such image: python:3.12-slim")
    with patch.object(DockerContainerManager, "ensure_container", return_value=failure):
        ok, message = docker_sandbox.ensure_project_container()
    assert not ok
    assert "pull it in Settings" in message


def test_container_path_maps_the_project_and_skill_folders(project, tmp_path, monkeypatch):
    outside = tmp_path / "user-skills"
    (outside / "s" / "scripts").mkdir(parents=True)
    monkeypatch.setattr("gilzabots.skills.skill_search_dirs", lambda project_path="": [str(outside)])

    assert docker_sandbox.container_path(str(project / "a" / "b.py")) == "/workspace/a/b.py"
    mapped = docker_sandbox.container_path(str(outside / "s" / "scripts" / "x.py"))
    assert mapped.startswith("/gilzabots/skills/") and mapped.endswith("/s/scripts/x.py")
    with pytest.raises(ValueError, match="outside the project folder"):
        docker_sandbox.container_path(str(tmp_path / "elsewhere.py"))


def test_run_command_is_routed_to_the_sandbox(project):
    from gilzabots.tools import run_command

    sandbox_config.save_config({"enabled": True})
    answer = {"stdout": "hello sandbox\n", "stderr": "", "exit_code": 0, "error": None}
    with patch("gilzabots.docker_sandbox.run_in_sandbox", return_value=answer) as sandbox:
        out = json.loads(json.loads(_call(run_command, "echo hello", timeout_seconds=7))["result"])
    sandbox.assert_called_once()
    assert sandbox.call_args.args == ("echo hello", str(project), 7)
    # The tool's stop request, so interrupting the turn ends the command too.
    assert isinstance(sandbox.call_args.kwargs["stop"], threading.Event)
    assert out == {"stdout": "hello sandbox", "stderr": "", "exit_code": 0}


def test_a_sandbox_timeout_reports_partial_output_and_whether_it_stopped(project):
    from gilzabots.tools import run_command

    sandbox_config.save_config({"enabled": True})
    timed_out = {"stdout": "started\n", "stderr": "", "exit_code": 124, "error": "TimeoutExpired", "stop_error": None}
    with patch("gilzabots.docker_sandbox.run_in_sandbox", return_value=timed_out):
        with pytest.raises(ValueError) as error:
            _call(run_command, "sleep 99", timeout_seconds=1)
    assert "timed out after 1 seconds and was stopped" in str(error.value)
    assert "stdout before the timeout:\nstarted" in str(error.value)

    with patch("gilzabots.docker_sandbox.run_in_sandbox", return_value={**timed_out, "stop_error": "tr: not found"}):
        with pytest.raises(ValueError) as error:
            _call(run_command, "sleep 99", timeout_seconds=1)
    assert "could not be stopped (tr: not found)" in str(error.value)


def test_run_python_script_runs_python3_on_the_container_path(project):
    from gilzabots.tools import run_python_script

    script = project / "scripts" / "test.py"
    script.parent.mkdir()
    script.write_text("print('hi')")
    sandbox_config.save_config({"enabled": True})
    with patch("gilzabots.tools._run_captured", return_value="{}") as run:
        _call(run_python_script, str(script), args="--flag")
    assert run.call_args[0][0] == 'python3 "/workspace/scripts/test.py" --flag'


def test_run_python_script_refuses_a_script_the_container_cannot_see(project, tmp_path):
    from gilzabots.tools import run_python_script

    sandbox_config.save_config({"enabled": True})
    with patch("gilzabots.tools._run_captured") as run:
        with pytest.raises(ValueError, match="outside the project folder"):
            _call(run_python_script, str(tmp_path / "elsewhere.py"))
    run.assert_not_called()


def _started(monkeypatch, docker_bin="/usr/bin/docker"):
    monkeypatch.setattr(docker_sandbox, "find_docker_bin", lambda: docker_bin)
    monkeypatch.setattr(docker_sandbox, "ensure_project_container", lambda project_path="": (True, "n"))


def test_a_terminal_command_runs_a_self_removing_script(project, monkeypatch):
    _started(monkeypatch)
    monkeypatch.setattr(docker_sandbox.shutil, "which", lambda name: "/usr/bin/docker")
    line, script = docker_sandbox.sandbox_terminal_command('npm create "x y"', str(project), tty=True)

    name = docker_sandbox.container_name_for_project(str(project))
    inner = "/workspace/.gilzabots/sandbox/" + os.path.basename(script)
    assert line == f"docker exec -it -w /workspace {name} sh {inner}"
    assert os.path.dirname(script) == str(project / ".gilzabots" / "sandbox")
    with open(script, encoding="utf-8", newline="") as f:
        assert f.read() == 'rm -f -- "$0"\nnpm create "x y"\n'

    line, _ = docker_sandbox.sandbox_terminal_command("ls", str(project), tty=False)
    assert line.startswith("docker exec -w /workspace ")


def test_a_terminal_command_refuses_a_docker_path_it_cannot_type(project, monkeypatch):
    _started(monkeypatch, "/opt/my tools/docker")
    monkeypatch.setattr(docker_sandbox.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="not on PATH"):
        docker_sandbox.sandbox_terminal_command("ls", str(project), tty=True)


def test_an_interactive_command_fails_when_the_container_does_not_start(project, monkeypatch):
    import gilzabots.terminal as T
    from gilzabots.tools import run_interactive_command

    sandbox_config.save_config({"enabled": True})
    monkeypatch.setattr(docker_sandbox, "find_docker_bin", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_sandbox, "ensure_project_container", lambda project_path="": (False, "daemon down"))
    with patch.object(T, "a_interactive_terminal") as terminal:
        with pytest.raises(ValueError, match="daemon down"):
            _call(run_interactive_command, "npm create vite")
    terminal.assert_not_called()


def test_an_interactive_command_opens_the_container_and_cleans_up(project, monkeypatch):
    import gilzabots.terminal as T
    from gilzabots.tools import run_interactive_command

    sandbox_config.save_config({"enabled": True})
    _started(monkeypatch)
    monkeypatch.setattr(docker_sandbox.shutil, "which", lambda name: "/usr/bin/docker")
    seen = {}

    async def terminal(command, term_id):
        seen["command"] = command
        return "cancel"

    monkeypatch.setattr(T, "a_interactive_terminal", terminal)
    assert _call(run_interactive_command, "npm create vite").startswith("FAILURE")
    assert seen["command"].startswith("docker exec -it -w /workspace gilzabots-sandbox-")
    assert os.listdir(project / ".gilzabots" / "sandbox") == []


def test_a_background_command_reaches_the_runner_through_the_container(project, monkeypatch):
    from gilzabots import agent_stdin
    from gilzabots.tools import run_background_command

    sandbox_config.save_config({"enabled": True})
    _started(monkeypatch)
    monkeypatch.setattr(docker_sandbox.shutil, "which", lambda name: "/usr/bin/docker")
    seen = {}

    async def runner(command, cwd):
        seen["command"], seen["cwd"] = command, cwd
        return "SUCCESS"

    agent_stdin.set_background_command_runner(runner)
    try:
        assert _call(run_background_command, "npm run dev") == "SUCCESS"
    finally:
        agent_stdin.set_background_command_runner(None)
    # The CLI's runner has no terminal, so no TTY is requested.
    assert seen["command"].startswith("docker exec -w /workspace gilzabots-sandbox-")
    assert seen["cwd"] == str(project)


def test_a_disabled_sandbox_runs_commands_locally(project):
    from gilzabots.tools import run_command

    with patch("gilzabots.docker_sandbox.run_in_sandbox") as sandbox:
        out = json.loads(json.loads(_call(run_command, "echo hello local"))["result"])
    sandbox.assert_not_called()
    assert out["stdout"] == "hello local"
