"""Whole-file writes that never leave a half-written file behind.

Settings and credential files used to be rewritten in place. A crash or a full
disk in the middle left them truncated, and their readers treat an unreadable
file as absent: the user's settings silently went back to the defaults. The new
content is written to a temp file next to the target and moved over it with
`os.replace`, which is atomic on the same filesystem.
"""

from __future__ import annotations

import os
import tempfile

# Read once at import, while nothing else is running: the umask can only be
# read by setting it, which is not thread-safe.
_UMASK = os.umask(0)
os.umask(_UMASK)


def write_text_atomic(path: str, text: str, *, mode: int | None = None) -> None:
    """Replace *path* with *text*, encoded and newline-translated as ``open(path, "w")`` does.

    *mode* sets the file's permissions. Without it an existing file keeps its
    own, and a new one gets what `open()` would have given it.
    """
    # Imported here: the cloud package costs ~80 ms to import, and settings are
    # written by front-ends that never sync anything.
    from .cloud.atomic import discard_file, replace_file

    target = os.path.realpath(path)
    directory = os.path.dirname(target) or "."
    if mode is None:
        try:
            mode = os.stat(target).st_mode & 0o7777
        except FileNotFoundError:
            mode = 0o666 & ~_UMASK
    fd, temp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        replace_file(temp_path, target)
    except BaseException:
        discard_file(temp_path)
        raise
