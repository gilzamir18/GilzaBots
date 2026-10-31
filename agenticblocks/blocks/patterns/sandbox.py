"""Shell command execution, either locally or inside a Docker container.

``DockerContainerManager`` drives the Docker CLI. It keeps a named container
running with a given specification (image, network, bind mounts, user,
environment), runs commands in it, and stops what a timed-out command left
running there. ``CommandExecutorBlock`` exposes either execution mode as a block.
"""

import asyncio
import hashlib
import json
import os
import posixpath
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Literal, Optional, Tuple

from pydantic import BaseModel, Field

from agenticblocks.core.block import Block

#: Label holding a digest of the specification a container was created with.
SPEC_LABEL = "agenticblocks.sandbox.spec"

#: Environment variable that marks every process started by one ``exec_in_container``
#: call, so the processes it left behind can be found when it times out.
EXEC_ID_ENV = "AGENTICBLOCKS_EXEC_ID"

#: Mount point of ``volume_mount`` inside the container.
WORKSPACE = "/workspace"


def _csv_field(field: str) -> str:
    # ``--mount`` is parsed as one CSV record, so a path with a comma or a quote
    # must be quoted the CSV way.
    if any(ch in field for ch in ',"\n'):
        return '"' + field.replace('"', '""') + '"'
    return field


@dataclass(frozen=True)
class BindMount:
    """A host directory bind-mounted into a container."""

    source: str
    target: str
    readonly: bool = False

    def to_arg(self) -> str:
        """The value of a ``--mount`` option for this bind mount.

        The source stays a native host path: the Docker CLI resolves it on every
        platform, including ``C:\\...`` paths with Docker Desktop on Windows.
        """
        fields = ["type=bind", f"source={os.path.abspath(self.source)}", f"target={self.target}"]
        if self.readonly:
            fields.append("readonly")
        return ",".join(_csv_field(f) for f in fields)

    def spec(self) -> Dict[str, Any]:
        return {
            "source": os.path.normcase(os.path.abspath(self.source)),
            "target": self.target,
            "readonly": self.readonly,
        }


_locks_guard = threading.Lock()
_container_locks: Dict[str, threading.Lock] = {}


def _container_lock(container_name: str) -> threading.Lock:
    with _locks_guard:
        return _container_locks.setdefault(container_name, threading.Lock())


class DockerContainerManager:
    """Manages Docker container lifecycles for sandboxed command and code execution."""

    @staticmethod
    def find_docker_binary(custom_path: str = "docker") -> Optional[str]:
        """Resolve ``custom_path``: a path is used as given, a bare name is looked up on PATH."""
        if not custom_path:
            return shutil.which("docker")
        if os.path.dirname(custom_path):
            if os.path.isfile(custom_path) and os.access(custom_path, os.X_OK):
                return custom_path
            return None
        return shutil.which(custom_path)

    @classmethod
    def check_docker_available(cls, docker_bin: str = "docker") -> Tuple[bool, str]:
        """Check if Docker CLI is found and daemon is reachable."""
        bin_path = cls.find_docker_binary(docker_bin)
        if not bin_path:
            return False, "Docker CLI executable was not found."

        try:
            res = subprocess.run(
                [bin_path, "version", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if res.returncode == 0:
                return True, "Docker is functional and daemon is reachable."
            combined_err = (res.stderr or res.stdout or "").strip()
            return False, combined_err or f"Docker version check failed with exit code {res.returncode}."
        except subprocess.TimeoutExpired:
            return False, "Docker daemon connection check timed out."
        except Exception as e:
            return False, f"Failed to check Docker: {e}"

    @staticmethod
    def container_spec_digest(
        image: str,
        network: str,
        mounts: Iterable[BindMount],
        user: Optional[str],
        env: Optional[Dict[str, str]],
        workdir: Optional[str],
    ) -> str:
        spec = {
            "image": image,
            "network": network or "",
            "mounts": [m.spec() for m in mounts],
            "user": user or "",
            "env": dict(sorted((env or {}).items())),
            "workdir": workdir or "",
        }
        return hashlib.sha256(json.dumps(spec, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    @classmethod
    def ensure_container(
        cls,
        container_name: str,
        image: str,
        volume_mount: Optional[str] = None,
        network: str = "bridge",
        docker_bin: str = "docker",
        *,
        mounts: Iterable[BindMount] = (),
        user: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        workdir: Optional[str] = None,
        pull: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Ensure a named container is running with the given specification.

        ``volume_mount`` is mounted at ``/workspace``, which also becomes the
        working directory unless ``workdir`` says otherwise; ``mounts`` are added
        as given. A container created with a different specification (another
        image, network, mount, user or environment) is removed and created
        again, so a changed setting always takes effect. ``pull`` is passed to
        ``docker run --pull`` (``"never"`` fails at once on a missing image
        instead of downloading it within the creation timeout).

        Returns ``(True, container_name)`` or ``(False, diagnostic)``.
        """
        bin_path = cls.find_docker_binary(docker_bin)
        if not bin_path:
            return False, "Docker executable not found."

        all_mounts = list(mounts)
        if volume_mount:
            all_mounts.insert(0, BindMount(volume_mount, WORKSPACE))
            workdir = workdir or WORKSPACE
        digest = cls.container_spec_digest(image, network, all_mounts, user, env, workdir)

        with _container_lock(container_name):
            for _attempt in range(2):
                state, detail = cls._inspect_state(bin_path, container_name)
                if state == "error":
                    return False, detail
                if state != "missing" and detail != digest:
                    if not cls.remove_container(container_name, docker_bin=bin_path):
                        return False, f"Failed to replace container {container_name}, whose settings changed."
                    state = "missing"
                if state == "running":
                    return True, container_name
                if state == "stopped":
                    try:
                        start = subprocess.run(
                            [bin_path, "start", container_name],
                            capture_output=True,
                            text=True,
                            timeout=15,
                        )
                    except Exception as e:
                        return False, f"Failed to start existing container {container_name}: {e}"
                    if start.returncode == 0:
                        return True, container_name
                    return False, f"Failed to start existing container {container_name}: {start.stderr.strip()}"

                cmd = [bin_path, "run", "-d", "--name", container_name, "--label", f"{SPEC_LABEL}={digest}"]
                if pull:
                    cmd.extend(["--pull", pull])
                if network:
                    cmd.extend(["--network", network])
                if user:
                    cmd.extend(["--user", user])
                for key, value in sorted((env or {}).items()):
                    cmd.extend(["-e", f"{key}={value}"])
                for mount in all_mounts:
                    cmd.extend(["--mount", mount.to_arg()])
                if workdir:
                    cmd.extend(["-w", workdir])
                cmd.extend([image, "tail", "-f", "/dev/null"])

                try:
                    run_res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                except Exception as e:
                    return False, f"Error creating container {container_name}: {e}"
                if run_res.returncode == 0:
                    return True, container_name
                err = (run_res.stderr or run_res.stdout or "").strip()
                # Another process created it between the inspect and the run: look again.
                if "already in use" in err.lower() and _attempt == 0:
                    continue
                return False, f"Failed to create container {container_name}: {err}"
        return False, f"Failed to create container {container_name}: its name is taken by a container being changed concurrently."

    @staticmethod
    def _inspect_state(bin_path: str, container_name: str) -> Tuple[str, str]:
        """Return ``(state, detail)``: ``running``/``stopped`` with the spec label, ``missing``, or ``error``."""
        try:
            inspect = subprocess.run(
                [
                    bin_path, "inspect", "--type", "container", "--format",
                    "{{.State.Running}}|{{index .Config.Labels \"" + SPEC_LABEL + "\"}}",
                    container_name,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as e:
            return "error", f"Failed inspecting container {container_name}: {e}"
        if inspect.returncode != 0:
            err = (inspect.stderr or inspect.stdout or "").strip()
            if "no such" in err.lower():
                return "missing", ""
            return "error", f"Failed inspecting container {container_name}: {err or f'exit code {inspect.returncode}'}"
        running, _, label = inspect.stdout.strip().partition("|")
        return ("running" if running.strip().lower() == "true" else "stopped"), label.strip()

    @classmethod
    def exec_in_container(
        cls,
        container_name: str,
        command: str,
        cwd: Optional[str] = WORKSPACE,
        timeout: int = 120,
        docker_bin: str = "docker",
        env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Run a shell command inside an existing container.

        On a timeout every process the command started inside the container is
        killed (stopping only the ``docker exec`` client would leave them
        running), and the output produced until then is returned with
        ``error == "TimeoutExpired"``. ``stop_error`` is set when those
        processes could not be stopped.
        """
        bin_path = cls.find_docker_binary(docker_bin)
        if not bin_path:
            return {
                "stdout": "",
                "stderr": "Docker binary not found.",
                "exit_code": -1,
                "error": "DockerNotFound",
            }

        exec_id = uuid.uuid4().hex
        cmd = [bin_path, "exec", "-e", f"{EXEC_ID_ENV}={exec_id}"]
        for key, value in sorted((env or {}).items()):
            cmd.extend(["-e", f"{key}={value}"])
        if cwd:
            cmd.extend(["-w", cwd])
        cmd.extend([container_name, "sh", "-c", command])

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "exit_code": -1, "error": str(e)}

        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            stopped, stop_detail = cls.stop_exec(container_name, exec_id, docker_bin=bin_path)
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
            return {
                "stdout": out or "",
                "stderr": err or "",
                "exit_code": 124,
                "error": "TimeoutExpired",
                "stop_error": None if stopped else stop_detail,
            }
        return {
            "stdout": out or "",
            "stderr": err or "",
            "exit_code": proc.returncode,
            "error": None if proc.returncode == 0 else f"Process exited with code {proc.returncode}",
        }

    @classmethod
    def stop_exec(cls, container_name: str, exec_id: str, docker_bin: str = "docker") -> Tuple[bool, str]:
        """Kill, inside the container, every process started by the exec marked ``exec_id``."""
        bin_path = cls.find_docker_binary(docker_bin)
        if not bin_path:
            return False, "Docker executable not found."
        marker = f"{EXEC_ID_ENV}={exec_id}"
        # Two passes: a child forked while the first pass ran is caught by the second.
        script = (
            "command -v tr >/dev/null && command -v grep >/dev/null "
            "|| { echo 'tr and grep are needed to find the processes' >&2; exit 127; }; "
            "for pass in 1 2; do for d in /proc/[0-9]*; do "
            f"if tr '\\000' '\\n' < \"$d/environ\" 2>/dev/null | grep -qx '{marker}'; then "
            "kill -9 \"${d#/proc/}\" 2>/dev/null; fi; done; done; exit 0"
        )
        try:
            res = subprocess.run(
                [bin_path, "exec", "-u", "0", container_name, "sh", "-c", script],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as e:
            return False, str(e)
        if res.returncode == 0:
            return True, ""
        return False, (res.stderr or res.stdout or "").strip() or f"exit code {res.returncode}"

    @classmethod
    def remove_container(cls, container_name: str, docker_bin: str = "docker") -> bool:
        """Remove the container, stopping it first; True also when there was none."""
        bin_path = cls.find_docker_binary(docker_bin)
        if not bin_path:
            return False
        try:
            res = subprocess.run(
                [bin_path, "rm", "-f", container_name],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return res.returncode == 0 or "no such" in (res.stderr or "").lower()
        except Exception:
            return False


class CommandExecutorInput(BaseModel):
    command: str = Field(..., description="The shell command to execute.")
    cwd: Optional[str] = Field(
        default=None,
        description=(
            "Working directory for execution. In docker mode it is a host directory "
            "inside the mounted one, or a path relative to it."
        ),
    )
    timeout: int = Field(default=120, description="Execution timeout in seconds.")


class CommandExecutorOutput(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    error: Optional[str] = None
    is_valid: bool = True


class CommandExecutorBlock(Block[CommandExecutorInput, CommandExecutorOutput]):
    """General block for executing shell commands either locally or inside a Docker sandbox.

    In docker mode ``volume_mount`` (or, without it, the input's ``cwd``) is
    mounted at ``/workspace``, and the command runs in the matching directory
    there. Without ``container_name`` each mounted directory gets a container of
    its own.
    """

    name: str = "command_executor"
    description: str = "Executes shell commands locally or inside a Docker container."

    execution_mode: Literal["local", "docker"] = "local"
    docker_image: str = "python:3.12-slim"
    container_name: Optional[str] = None
    volume_mount: Optional[str] = None
    network: str = "bridge"
    docker_bin: str = "docker"

    model_config = {"arbitrary_types_allowed": True}

    async def run(self, input: CommandExecutorInput) -> CommandExecutorOutput:
        if self.execution_mode == "local":
            return await asyncio.to_thread(self._run_local, input)
        elif self.execution_mode == "docker":
            return await asyncio.to_thread(self._run_docker, input)
        return CommandExecutorOutput(
            stdout="",
            stderr="",
            exit_code=-1,
            error=f"Unsupported execution mode: {self.execution_mode}",
            is_valid=False,
        )

    def _run_local(self, input: CommandExecutorInput) -> CommandExecutorOutput:
        try:
            res = subprocess.run(
                input.command,
                shell=True,
                cwd=input.cwd,
                capture_output=True,
                text=True,
                timeout=input.timeout,
            )
            return CommandExecutorOutput(
                stdout=res.stdout,
                stderr=res.stderr,
                exit_code=res.returncode,
                error=None if res.returncode == 0 else f"Command failed with code {res.returncode}",
                is_valid=(res.returncode == 0),
            )
        except subprocess.TimeoutExpired:
            return CommandExecutorOutput(
                stdout="",
                stderr=f"Command timed out after {input.timeout}s",
                exit_code=124,
                error="TimeoutExpired",
                is_valid=False,
            )
        except Exception as e:
            return CommandExecutorOutput(
                stdout="",
                stderr=str(e),
                exit_code=-1,
                error=str(e),
                is_valid=False,
            )

    @staticmethod
    def default_container_name(mount: Optional[str]) -> str:
        if not mount:
            return "agenticblocks-sandbox"
        digest = hashlib.sha256(os.path.normcase(os.path.abspath(mount)).encode("utf-8")).hexdigest()[:12]
        return f"agenticblocks-sandbox-{digest}"

    @staticmethod
    def container_cwd(mount: Optional[str], cwd: Optional[str]) -> Optional[str]:
        """Map ``cwd`` to the container: a directory inside ``mount`` becomes the same one under ``/workspace``."""
        if not mount:
            return cwd or None
        if not cwd:
            return WORKSPACE
        if os.path.isabs(cwd):
            try:
                rel = os.path.relpath(os.path.abspath(cwd), os.path.abspath(mount))
            except ValueError:
                rel = None
        else:
            rel = os.path.normpath(cwd)
        if rel is None or rel == os.pardir or rel.startswith(os.pardir + os.sep):
            raise ValueError(f"Working directory {cwd} is outside the mounted directory {mount}.")
        if rel == os.curdir:
            return WORKSPACE
        return posixpath.join(WORKSPACE, rel.replace(os.sep, "/"))

    def _run_docker(self, input: CommandExecutorInput) -> CommandExecutorOutput:
        mount = self.volume_mount or input.cwd
        c_name = self.container_name or self.default_container_name(mount)
        try:
            cwd = self.container_cwd(mount, input.cwd)
        except ValueError as e:
            return CommandExecutorOutput(stdout="", stderr=str(e), exit_code=-1, error=str(e), is_valid=False)

        ok, msg = DockerContainerManager.ensure_container(
            container_name=c_name,
            image=self.docker_image,
            volume_mount=mount,
            network=self.network,
            docker_bin=self.docker_bin,
        )
        if not ok:
            return CommandExecutorOutput(
                stdout="",
                stderr=msg,
                exit_code=-1,
                error=msg,
                is_valid=False,
            )

        res = DockerContainerManager.exec_in_container(
            container_name=c_name,
            command=input.command,
            cwd=cwd,
            timeout=input.timeout,
            docker_bin=self.docker_bin,
        )
        return CommandExecutorOutput(
            stdout=res.get("stdout", ""),
            stderr=res.get("stderr", ""),
            exit_code=res.get("exit_code", -1),
            error=res.get("error"),
            is_valid=(res.get("exit_code") == 0),
        )
