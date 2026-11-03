"""Tests for AssetStore icon resolution (Asset Store icon support)."""

from pathlib import Path

from gilzabots.assetstore import resolve_asset_icon_path


def _meta(meta_dir: Path, icon: str | None) -> dict:
    meta_path = meta_dir / "demo.metadata"
    meta_path.touch()
    d = {"id": "demo", "type": "skill", "name": "demo", "desc": "Demo skill.", "_meta": meta_path}
    if icon is not None:
        d["icon"] = icon
    return d


def test_resolve_asset_icon_path_missing_field_returns_none(tmp_path):
    assert resolve_asset_icon_path(_meta(tmp_path, None)) is None


def test_resolve_asset_icon_path_missing_file_returns_none(tmp_path):
    assert resolve_asset_icon_path(_meta(tmp_path, "demo.png")) is None


def test_resolve_asset_icon_path_returns_path_when_present(tmp_path):
    (tmp_path / "demo.png").write_bytes(b"fake-png")
    icon_path = resolve_asset_icon_path(_meta(tmp_path, "demo.png"))
    assert icon_path == (tmp_path / "demo.png").resolve()


def test_resolve_asset_icon_path_rejects_path_traversal(tmp_path):
    store_dir = tmp_path / "skills"
    store_dir.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"fake-png")
    assert resolve_asset_icon_path(_meta(store_dir, "../secret.png")) is None
