"""Docker sandbox engine for OpalaTex.

Manages container sessions, pre-flight checks, and execution routing for
both Orchestrator and Worker agent turns.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import time
import uuid
from typing import Any, Tuple

from agenticblocks.blocks.patterns.sandbox import BindMount, DockerContainerManager

from .external_tools import find_tool
from .sandbox_config import (
    DEFAULT_IMAGE,
    get_sandbox_image,
    get_sandbox_network,
    load_config,
)

#: Where the project is mounted inside the container.
WORKSPACE = "/workspace"

#: Skill folders outside the project are mounted read-only under this directory.
SKILLS_MOUNT_ROOT = "/opalatex/skills"

#: Project folder for the scripts that terminal commands run in the container.
_SCRIPT_DIR = os.path.join(".opalatex", "sandbox")
_SCRIPT_MAX_AGE = 24 * 3600


def _running_in_snap() -> bool:
    # Only OpalaTex's own snap: a terminal of another snapped app (an IDE) also
    # exports SNAP and SNAP_NAME to the programs started from it.
    return os.environ.get("SNAP_NAME") == "opalatex"


def _snap_docker_plug_connected() -> bool | None:
    """Whether the snap's ``docker`` plug is connected; None when snapctl cannot tell."""
    try:
        proc = subprocess.run(
            ["snapctl", "is-connected", "docker"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    return None


def _docker_group_pending() -> bool:
    """True when the user is in the 'docker' group but this process does not have it yet.

    ``usermod -aG docker`` changes /etc/group only; the groups of a process come
    from the login session, so everything started from a session that began
    before (restarting the app included) still lacks the group until the user
    logs in again.
    """
    try:
        import grp
        import pwd

        group = grp.getgrnam("docker")
        user = pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        return False
    if group.gr_gid == os.getgid() or group.gr_gid in os.getgroups():
        return False
    return user in group.gr_mem


def find_docker_bin() -> str | None:
    """Locate the docker executable across in-app bin, bundled bin, and PATH."""
    return find_tool("docker")


def _project_root(project_path: str = "") -> str:
    from .tools import get_project_path
    return os.path.abspath(project_path or get_project_path() or os.getcwd())


def container_name_for_project(project_path: str = "") -> str:
    """Deterministic container name for a project workspace."""
    norm = os.path.normcase(_project_root(project_path))
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]
    return f"opalatex-sandbox-{digest}"


def _is_within(path: str, root: str) -> bool:
    path = os.path.normcase(os.path.abspath(path))
    root = os.path.normcase(os.path.abspath(root))
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _skill_mount_target(directory: str) -> str:
    digest = hashlib.sha256(os.path.normcase(os.path.abspath(directory)).encode("utf-8")).hexdigest()[:12]
    return f"{SKILLS_MOUNT_ROOT}/{digest}"


def _skill_mounts(project_root: str) -> list[BindMount]:
    """Read-only mounts for the skill folders outside the project, so their scripts run."""
    from .skills import skill_search_dirs

    return [
        BindMount(directory, _skill_mount_target(directory), readonly=True)
        for directory in skill_search_dirs(project_root)
        if os.path.isdir(directory) and not _is_within(directory, project_root)
    ]


def container_path(host_path: str, project_path: str = "") -> str:
    """The path of *host_path* inside the project's container.

    Only the project and the skill folders are mounted; any other path raises
    ``ValueError``.
    """
    root = _project_root(project_path)
    candidates = [(root, WORKSPACE)] + [(m.source, m.target) for m in _skill_mounts(root)]
    for source, target in candidates:
        if _is_within(host_path, source):
            rel = os.path.relpath(os.path.abspath(host_path), os.path.abspath(source))
            return target if rel == os.curdir else f"{target}/{rel.replace(os.sep, '/')}"
    raise ValueError(
        f"{host_path} is outside the project folder. Only the project (mounted at "
        f"{WORKSPACE}) and the skill folders are visible inside the Docker sandbox; "
        "copy the file into the project first."
    )


_rootless_by_bin: dict[str, bool] = {}


def _container_user(docker_bin: str) -> str | None:
    """The user commands run as, so files they create in the project belong to the user.

    Containers run as root by default, and on a Linux host every file a command
    wrote into the bind-mounted project would then be owned by root. Rootless
    Docker (and Podman) already map the container's root to the user who runs
    it, and Windows has no uid, so those keep the image's user. Only a
    successful ``docker info`` answer is remembered.
    """
    if os.name == "nt" or not hasattr(os, "getuid"):
        return None
    rootless = _rootless_by_bin.get(docker_bin)
    if rootless is None:
        try:
            proc = subprocess.run(
                [docker_bin, "info", "--format", "{{json .SecurityOptions}}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and proc.returncode == 0:
            rootless = _rootless_by_bin[docker_bin] = "rootless" in proc.stdout
    if rootless:
        return None
    return f"{os.getuid()}:{os.getgid()}"


def verify_docker() -> dict[str, Any]:
    """Perform pre-flight verification of Docker, returning diagnostic state."""
    cfg = load_config()
    image = get_sandbox_image()
    network = get_sandbox_network()
    docker_bin = find_docker_bin()
    in_snap = _running_in_snap()
    os_name = platform.system()

    result: dict[str, Any] = {
        "enabled": bool(cfg.get("enabled", False)),
        "image": image,
        "network": network,
        "docker_bin": docker_bin or "",
        "daemon_running": False,
        "image_found": False,
        "ready": False,
        "problem": "",
        "action_command": "",
    }

    if not docker_bin:
        if in_snap:
            result["problem"] = (
                "The Docker CLI client was not found inside the Snap environment. "
                "Ensure Docker is installed on your host system."
            )
        else:
            result["problem"] = (
                f"Docker executable was not found on PATH ({os_name}). "
                "Please install Docker Engine (Linux) or Docker Desktop (Windows/macOS)."
            )
        return result

    # Check daemon accessibility
    try:
        proc = subprocess.run(
            [docker_bin, "version", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        result["problem"] = "Checking Docker version timed out. The Docker daemon may be unresponsive."
        return result
    except Exception as e:
        result["problem"] = f"Failed to execute Docker command: {e}"
        return result

    combined_out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    combined_lower = combined_out.lower()

    if proc.returncode != 0:
        if "permission denied" in combined_lower or "dial unix /var/run/docker.sock" in combined_lower:
            # Under Snap the plug only lifts confinement: the socket's own
            # permissions (root:docker) still require the docker group.
            if in_snap and _snap_docker_plug_connected() is not True:
                result["problem"] = (
                    "OpalaTex is running in Snap confinement and does not have permission "
                    "to access the Docker daemon socket."
                )
                result["action_command"] = "sudo snap connect opalatex:docker"
            elif _docker_group_pending():
                result["problem"] = (
                    "Your user is in the 'docker' group, but OpalaTex runs in a login session "
                    "that started before you were added, so it does not have the group yet; "
                    "restarting OpalaTex is not enough. Log out and back in (or restart the "
                    "computer), then open OpalaTex again."
                )
            else:
                result["problem"] = (
                    "Permission denied accessing the Docker daemon socket. "
                    "Add your user to the 'docker' group to run containers without sudo, "
                    "then log out and back in."
                )
                result["action_command"] = "sudo usermod -aG docker $USER"
        elif "is the docker daemon running" in combined_lower or "no such file or directory" in combined_lower or "cannot connect to the docker daemon" in combined_lower:
            result["problem"] = (
                "The Docker daemon is not running. Please start the Docker service or launch Docker Desktop."
            )
            if os_name == "Linux" and not in_snap:
                result["action_command"] = "sudo systemctl start docker"
        else:
            lines = (proc.stderr or proc.stdout or "").strip().splitlines()
            err_line = lines[-1] if lines else f"exit code {proc.returncode}"
            result["problem"] = f"Docker daemon is unreachable: {err_line}"
        return result

    result["daemon_running"] = True

    # Check if target image is available locally
    try:
        inspect = subprocess.run(
            [docker_bin, "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if inspect.returncode == 0:
            result["image_found"] = True
            result["ready"] = True
        else:
            result["image_found"] = False
            result["ready"] = False
            result["problem"] = f"Docker image '{image}' is not downloaded yet. Click 'Pull Image' to download it."
            result["action_command"] = f"docker pull {image}"
    except Exception as e:
        result["problem"] = f"Error inspecting image '{image}': {e}"
        return result

    return result


def pull_sandbox_image(image: str | None = None) -> Tuple[bool, str]:
    """Pull the configured Docker image."""
    docker_bin = find_docker_bin()
    if not docker_bin:
        return False, "Docker executable not found."
    target_image = (image or get_sandbox_image()).strip()
    if not target_image:
        target_image = DEFAULT_IMAGE

    try:
        proc = subprocess.run(
            [docker_bin, "pull", target_image],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode == 0:
            return True, f"Successfully pulled image '{target_image}'."
        err = (proc.stderr or proc.stdout or "").strip()
        return False, f"Failed pulling image '{target_image}': {err}"
    except subprocess.TimeoutExpired:
        return False, f"Timed out pulling image '{target_image}'."
    except Exception as e:
        return False, f"Error pulling image '{target_image}': {e}"


def ensure_project_container(project_path: str = "") -> Tuple[bool, str]:
    """Ensure the persistent sandbox container for *project_path* is active.

    A container created with other settings (image, network, mounts, user) is
    replaced, which discards what was installed in it.
    """
    root = _project_root(project_path)
    docker_bin = find_docker_bin()
    if not docker_bin:
        return False, "Docker executable not found."
    image = get_sandbox_image()
    user = _container_user(docker_bin)

    ok, info = DockerContainerManager.ensure_container(
        container_name=container_name_for_project(root),
        image=image,
        volume_mount=root,
        network=get_sandbox_network(),
        docker_bin=docker_bin,
        mounts=_skill_mounts(root),
        user=user,
        # An arbitrary uid has no home directory in the image; /tmp is writable
        # by everyone, so pip's user installs and tool caches have a place to go.
        env={"HOME": "/tmp"} if user else None,
        pull="never",
    )
    if not ok and ("no such image" in info.lower() or "unable to find image" in info.lower()):
        info += f" The image '{image}' is not downloaded; pull it in Settings → Docker Sandbox."
    return ok, info


def recreate_project_container(project_path: str = "") -> Tuple[bool, str]:
    """Stop and remove the sandbox container for *project_path*."""
    docker_bin = find_docker_bin()
    if not docker_bin:
        return False, "Docker executable not found."
    container_name = container_name_for_project(project_path)
    ok = DockerContainerManager.remove_container(container_name, docker_bin=docker_bin)
    if ok:
        return True, f"Container '{container_name}' removed; it will be created again on the next command."
    return False, f"Container '{container_name}' could not be removed."


def _start_container(project_path: str) -> tuple[str, str]:
    docker_bin = find_docker_bin()
    if not docker_bin:
        raise ValueError(
            "Docker sandbox is enabled but the 'docker' command-line binary was not found. "
            "Please check Docker installation or disable Sandboxing in Settings."
        )
    ok, info = ensure_project_container(project_path)
    if not ok:
        raise ValueError(f"Failed to start sandbox container: {info}")
    return docker_bin, container_name_for_project(project_path)


def run_in_sandbox(command: str, project_path: str, timeout_seconds: int) -> dict[str, Any]:
    """Execute *command* inside the project's sandbox container.

    Returns the ``exec_in_container`` result: ``stdout``, ``stderr``,
    ``exit_code``, and on a timeout ``error == "TimeoutExpired"`` with
    ``stop_error`` set when the command's processes could not be stopped.
    Raises ``ValueError`` when the container cannot be started.
    """
    docker_bin, container_name = _start_container(project_path)
    return DockerContainerManager.exec_in_container(
        container_name=container_name,
        command=command,
        cwd=WORKSPACE,
        timeout=timeout_seconds,
        docker_bin=docker_bin,
    )


def _terminal_token(docker_bin: str) -> str:
    """How a terminal names *docker_bin* without any quoting."""
    on_path = shutil.which("docker")
    if on_path and os.path.normcase(os.path.abspath(on_path)) == os.path.normcase(os.path.abspath(docker_bin)):
        return "docker"
    if re.fullmatch(r"[A-Za-z0-9_./\\:-]+", docker_bin):
        return docker_bin
    raise ValueError(
        f"The Docker CLI at {docker_bin} is not on PATH and its path cannot be typed "
        "into a terminal; add its folder to PATH."
    )


def _prune_scripts(folder: str) -> None:
    cutoff = time.time() - _SCRIPT_MAX_AGE
    try:
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if name.startswith("run-") and name.endswith(".sh") and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except OSError:
        pass


def sandbox_terminal_command(command: str, project_path: str, tty: bool) -> tuple[str, str]:
    """A command line that runs *command* in the project's container from a terminal.

    The command is written to a script in the project, which removes itself when
    it starts, so the line typed into the terminal holds no quoting and reads the
    same in bash, sh, PowerShell and cmd. Returns ``(command_line, script_path)``.
    Raises ``ValueError`` when the container cannot be started.
    """
    root = _project_root(project_path)
    docker_bin, container_name = _start_container(root)
    token = _terminal_token(docker_bin)

    folder = os.path.join(root, _SCRIPT_DIR)
    os.makedirs(folder, exist_ok=True)
    _prune_scripts(folder)
    name = f"run-{uuid.uuid4().hex[:12]}.sh"
    script_path = os.path.join(folder, name)
    with open(script_path, "w", encoding="utf-8", newline="\n") as f:
        f.write('rm -f -- "$0"\n' + command + "\n")

    inner = f"{WORKSPACE}/{_SCRIPT_DIR.replace(os.sep, '/')}/{name}"
    parts = [token, "exec"] + (["-it"] if tty else []) + ["-w", WORKSPACE, container_name, "sh", inner]
    return " ".join(parts), script_path
