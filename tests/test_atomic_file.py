"""Settings and credential files are replaced whole, never left half-written."""
import os
import stat

import pytest

from gilzabots import atomic_file
from gilzabots.atomic_file import write_text_atomic


def test_the_file_is_replaced_with_the_new_text(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text("old", encoding="utf-8")
    write_text_atomic(str(target), "new ✓")
    assert target.read_text(encoding="utf-8") == "new ✓"
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_a_failed_write_keeps_the_old_file_and_leaves_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    target.write_text("old", encoding="utf-8")

    def failing_replace(source, destination):
        raise OSError("disk full")

    import gilzabots.cloud.atomic as cloud_atomic
    monkeypatch.setattr(cloud_atomic, "replace_file", failing_replace)
    with pytest.raises(OSError):
        write_text_atomic(str(target), "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_permissions_are_kept_or_set(tmp_path):
    existing = tmp_path / "existing"
    existing.write_text("x")
    os.chmod(existing, 0o640)
    write_text_atomic(str(existing), "y")
    assert stat.S_IMODE(existing.stat().st_mode) == 0o640

    fresh = tmp_path / "fresh"
    write_text_atomic(str(fresh), "y")
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o666 & ~atomic_file._UMASK

    secret = tmp_path / ".env"
    secret.write_text("KEY=1")
    os.chmod(secret, 0o664)
    write_text_atomic(str(secret), "KEY=2", mode=0o600)
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_file_is_written_through_the_link(tmp_path):
    real = tmp_path / "real.env"
    real.write_text("old")
    link = tmp_path / ".env"
    link.symlink_to(real)
    write_text_atomic(str(link), "new")
    assert link.is_symlink()
    assert real.read_text() == "new"
