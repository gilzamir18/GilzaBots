"""agenticblocks.blocks.patterns.sandbox: Docker container management and the command block."""
import asyncio
import os
import subprocess
import time
from unittest.mock import patch

import pytest

from agenticblocks.blocks.patterns.sandbox import (
    BindMount,
    CommandExecutorBlock,
    CommandExecutorInput,
    DockerContainerManager,
)

M = "agenticblocks.blocks.patterns.sandbox"


class FakeDocker:
    """Answers `subprocess.run` calls to the Docker CLI from a scripted container state."""

    def __init__(self, state="missing", label="", inspect_error="", run_errors=()):
        self.state = state
        self.label = label
        self.inspect_error = inspect_error
        self.run_errors = list(run_errors)
        self.calls = []

    def __call__(self, cmd, **_kwargs):
        self.calls.append(cmd)
        verb = cmd[1]
        if verb == "inspect":
            if self.inspect_error:
                return subprocess.CompletedProcess(cmd, 1, "", self.inspect_error)
            if self.state == "missing":
                return subprocess.CompletedProcess(cmd, 1, "", f"Error: No such container: {cmd[-1]}")
            running = "true" if self.state == "running" else "false"
            return subprocess.CompletedProcess(cmd, 0, f"{running}|{self.label}\n", "")
        if verb == "rm":
            self.state = "missing"
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if verb == "start":
            self.state = "running"
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if verb == "run":
            if self.run_errors:
                error, self.state, self.label = self.run_errors.pop(0)
                return subprocess.CompletedProcess(cmd, 125, "", error)
            self.state = "running"
            self.label = cmd[cmd.index("--label") + 1].split("=", 1)[1]
            return subprocess.CompletedProcess(cmd, 0, "abc\n", "")
        raise AssertionError(f"unexpected docker call {cmd}")

    def verbs(self):
        return [c[1] for c in self.calls]


@pytest.fixture
def docker():
    def install(fake):
        stack.enter_context(patch(f"{M}.subprocess.run", side_effect=fake))
        return fake

    from contextlib import ExitStack

    with ExitStack() as stack:
        stack.enter_context(patch.object(DockerContainerManager, "find_docker_binary", return_value="docker"))
        yield install


SPEC = dict(image="img:1", network="bridge")


def _digest(volume_mount="/proj", **overrides):
    spec = {
        "image": "img:1", "network": "bridge", "user": None, "env": None,
        "mounts": [BindMount(volume_mount, "/workspace")], "workdir": "/workspace",
    }
    spec.update(overrides)
    return DockerContainerManager.container_spec_digest(**spec)


def test_bind_mount_argument_keeps_native_paths_and_quotes_commas():
    assert BindMount("/home/u/p", "/workspace").to_arg() == "type=bind,source=/home/u/p,target=/workspace"
    assert BindMount("/home/u/a,b", "/s", readonly=True).to_arg() == (
        'type=bind,"source=/home/u/a,b",target=/s,readonly'
    )


def test_find_docker_binary_uses_explicit_paths_as_given(tmp_path):
    missing = str(tmp_path / "docker")
    assert DockerContainerManager.find_docker_binary(missing) is None
    exe = tmp_path / "docker"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert DockerContainerManager.find_docker_binary(str(exe)) == str(exe)
    with patch(f"{M}.shutil.which", return_value="/usr/bin/docker") as which:
        assert DockerContainerManager.find_docker_binary("docker") == "/usr/bin/docker"
        which.assert_called_once_with("docker")


def test_a_missing_container_is_created_with_its_specification(docker):
    fake = docker(FakeDocker())
    ok, name = DockerContainerManager.ensure_container(
        "box", volume_mount="/proj", user="1000:1000", env={"HOME": "/tmp"},
        mounts=[BindMount("/skills", "/s", readonly=True)], pull="never", **SPEC,
    )
    assert (ok, name) == (True, "box")
    run = fake.calls[-1]
    assert run[:4] == ["docker", "run", "-d", "--name"]
    for flag, value in [
        ("--pull", "never"), ("--network", "bridge"), ("--user", "1000:1000"),
        ("-e", "HOME=/tmp"), ("-w", "/workspace"),
    ]:
        assert run[run.index(flag) + 1] == value
    mounts = [run[i + 1] for i, a in enumerate(run) if a == "--mount"]
    assert mounts == ["type=bind,source=/proj,target=/workspace", "type=bind,source=/skills,target=/s,readonly"]
    assert run[-4:] == ["img:1", "tail", "-f", "/dev/null"]


def test_a_running_container_with_the_same_specification_is_reused(docker):
    fake = docker(FakeDocker("running", _digest()))
    assert DockerContainerManager.ensure_container("box", volume_mount="/proj", **SPEC) == (True, "box")
    assert fake.verbs() == ["inspect"]


def test_a_container_with_another_specification_is_replaced(docker):
    fake = docker(FakeDocker("running", _digest(image="img:0")))
    assert DockerContainerManager.ensure_container("box", volume_mount="/proj", **SPEC) == (True, "box")
    assert fake.verbs() == ["inspect", "rm", "run"]
    assert fake.label == _digest()


def test_a_stopped_container_with_the_same_specification_is_started(docker):
    fake = docker(FakeDocker("stopped", _digest()))
    assert DockerContainerManager.ensure_container("box", volume_mount="/proj", **SPEC) == (True, "box")
    assert fake.verbs() == ["inspect", "start"]


def test_an_inspect_failure_is_reported_instead_of_creating(docker):
    fake = docker(FakeDocker(inspect_error="permission denied while trying to connect to the docker API"))
    ok, message = DockerContainerManager.ensure_container("box", volume_mount="/proj", **SPEC)
    assert not ok
    assert "permission denied" in message
    assert fake.verbs() == ["inspect"]


def test_a_container_created_concurrently_is_used(docker):
    conflict = ('Conflict. The container name "/box" is already in use', "running", _digest())
    fake = docker(FakeDocker(run_errors=[conflict]))
    assert DockerContainerManager.ensure_container("box", volume_mount="/proj", **SPEC) == (True, "box")
    assert fake.verbs() == ["inspect", "run", "inspect"]


def test_remove_container_counts_a_missing_container_as_removed():
    def fake(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "Error response from daemon: No such container: box")

    with patch.object(DockerContainerManager, "find_docker_binary", return_value="docker"), \
            patch(f"{M}.subprocess.run", side_effect=fake):
        assert DockerContainerManager.remove_container("box") is True


@pytest.fixture
def local_docker(tmp_path):
    """A `docker` that runs `exec` locally, so the timeout handling meets real processes."""
    exe = tmp_path / "docker"
    exe.write_text(
        "#!/bin/sh\n"
        '[ "$1" = exec ] || exit 99\n'
        "shift\n"
        'while [ $# -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    -e) export "$2"; shift 2 ;;\n'
        "    -w|-u) shift 2 ;;\n"
        "    *) break ;;\n"
        "  esac\n"
        "done\n"
        "shift\n"
        'exec "$@"\n'
    )
    exe.chmod(0o755)
    return str(exe)


def _gone(pid):
    for _ in range(100):
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().split(") ")[1].startswith("Z"):
                    return True
        except FileNotFoundError:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="needs /proc")
def test_a_timeout_stops_the_commands_processes_and_keeps_their_output(local_docker, tmp_path):
    pid_file = tmp_path / "child.pid"
    started = time.monotonic()
    res = DockerContainerManager.exec_in_container(
        "box", f'sleep 60 & echo $! > "{pid_file}"; echo started; wait',
        timeout=1, docker_bin=local_docker,
    )
    assert time.monotonic() - started < 20
    assert res["error"] == "TimeoutExpired"
    assert res["exit_code"] == 124
    assert res["stop_error"] is None
    assert res["stdout"].strip() == "started"
    assert _gone(int(pid_file.read_text().strip()))


def test_exec_reports_output_and_exit_code(local_docker):
    res = DockerContainerManager.exec_in_container("box", "echo out; echo err >&2; exit 3", docker_bin=local_docker)
    assert (res["stdout"], res["stderr"], res["exit_code"]) == ("out\n", "err\n", 3)


def test_container_cwd_maps_host_directories_into_the_mount(tmp_path):
    mount = str(tmp_path)
    assert CommandExecutorBlock.container_cwd(mount, None) == "/workspace"
    assert CommandExecutorBlock.container_cwd(mount, mount) == "/workspace"
    assert CommandExecutorBlock.container_cwd(mount, str(tmp_path / "a" / "b")) == "/workspace/a/b"
    assert CommandExecutorBlock.container_cwd(mount, "a/b") == "/workspace/a/b"
    assert CommandExecutorBlock.container_cwd(None, None) is None
    with pytest.raises(ValueError):
        CommandExecutorBlock.container_cwd(mount, str(tmp_path.parent))
    with pytest.raises(ValueError):
        CommandExecutorBlock.container_cwd(mount, "../x")


def test_each_mounted_directory_gets_its_own_default_container():
    a = CommandExecutorBlock.default_container_name("/proj/a")
    assert a == CommandExecutorBlock.default_container_name("/proj/a")
    assert a != CommandExecutorBlock.default_container_name("/proj/b")


def test_docker_mode_runs_in_the_workspace_of_the_input_directory(tmp_path):
    block = CommandExecutorBlock(execution_mode="docker")
    with patch.object(DockerContainerManager, "ensure_container", return_value=(True, "x")) as ensure, \
            patch.object(DockerContainerManager, "exec_in_container",
                         return_value={"stdout": "ok", "stderr": "", "exit_code": 0, "error": None}) as run:
        out = asyncio.run(block.run(CommandExecutorInput(command="ls", cwd=str(tmp_path))))
    assert out.is_valid and out.stdout == "ok"
    assert ensure.call_args.kwargs["volume_mount"] == str(tmp_path)
    assert ensure.call_args.kwargs["container_name"] == CommandExecutorBlock.default_container_name(str(tmp_path))
    assert run.call_args.kwargs["cwd"] == "/workspace"


def test_local_mode_runs_the_command(tmp_path):
    block = CommandExecutorBlock(execution_mode="local")
    res = asyncio.run(block.run(CommandExecutorInput(command='python3 -c "print(1 + 1)"', cwd=str(tmp_path), timeout=5)))
    assert (res.exit_code, res.stdout.strip(), res.is_valid) == (0, "2", True)


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="needs /proc")
def test_a_stop_request_ends_the_command_before_its_timeout(local_docker, tmp_path):
    import threading

    pid_file = tmp_path / "child.pid"
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    started = time.monotonic()
    res = DockerContainerManager.exec_in_container(
        "box", f'sleep 60 & echo $! > "{pid_file}"; echo started; wait',
        timeout=60, docker_bin=local_docker, stop=stop,
    )
    assert time.monotonic() - started < 20
    assert res["error"] == "Stopped"
    assert res["exit_code"] == 130
    assert res["stop_error"] is None
    assert res["stdout"].strip() == "started"
    assert _gone(int(pid_file.read_text().strip()))
