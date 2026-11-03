"""The binary, self-contained serialization of a JPT presentation."""

import base64
import json
import zipfile
from io import BytesIO

import pytest

from gilzabots import jpt


def _deck(*elements, background_image=""):
    deck = jpt.create_deck("Package", theme={"backgroundImage": background_image})
    slide = jpt.create_slide(id="slide-1")
    deck["slides"].append(slide)
    slide["elements"].extend(elements)
    return deck


def _image(src, element_id="image-1"):
    return jpt.create_element(
        "image", id=element_id, src=src, x=10, y=10, w=100, h=100
    )


def _video(src, poster=""):
    return jpt.create_element(
        "video", id="video-1", src=src, poster=poster,
        x=10, y=120, w=320, h=180,
    )


def _data_uri(payload=b"same image"):
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


def test_writer_creates_a_zip_with_json_and_content_addressed_assets(tmp_path):
    target = tmp_path / "deck.jpt"
    result = jpt.write_packaged_jpt(
        target, _deck(_image(_data_uri())), project_root=tmp_path
    )

    assert result.assets == 1
    assert result.asset_bytes == len(b"same image")
    assert target.read_bytes().startswith(b"PK")
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert names[0] == "mimetype"
        assert names[-1] == "deck.json"
        assert archive.read("mimetype").decode() == jpt.PACKAGE_MIMETYPE
        asset = next(name for name in names if name.startswith("assets/"))
        assert archive.getinfo(asset).compress_type == zipfile.ZIP_STORED
        assert archive.read(asset) == b"same image"

    document = jpt.read_jpt(target)
    assert document.packaged is True
    raw = json.loads(document.text)
    assert raw["slides"][0]["elements"][0]["src"] == f"jpt:{asset}"


def test_local_images_and_videos_are_packaged_and_deduplicated(tmp_path):
    picture = tmp_path / "picture.png"
    film = tmp_path / "film.mp4"
    picture.write_bytes(b"picture bytes")
    film.write_bytes(b"video bytes" * 100)
    target = tmp_path / "deck.jpt"
    deck = _deck(
        _image("picture.png", "image-1"),
        _image("picture.png", "image-2"),
        _video("film.mp4", "picture.png"),
        background_image="picture.png",
    )

    result = jpt.write_packaged_jpt(target, deck, project_root=tmp_path)
    raw = json.loads(result.text)
    refs = jpt.used_sources(raw)
    assert result.assets == 2
    assert all(ref.startswith("jpt:assets/") for ref in refs)
    assert len(set(refs)) == 2


def test_asset_reader_returns_inclusive_ranges_without_unpacking_to_disk(tmp_path):
    film = tmp_path / "film.mp4"
    film.write_bytes(bytes(range(100)))
    target = tmp_path / "deck.jpt"
    result = jpt.write_packaged_jpt(
        target, _deck(_video("film.mp4")), project_root=tmp_path
    )
    ref = json.loads(result.text)["slides"][0]["elements"][0]["src"]

    payload, total, mime = jpt.read_asset(target, ref, start=17, end=24)
    assert payload == bytes(range(17, 25))
    assert total == 100
    assert mime == "video/mp4"
    assert list(jpt.iter_asset(
        target, ref, start=17, end=31, chunk_size=6
    )) == [bytes(range(17, 23)), bytes(range(23, 29)), bytes(range(29, 32))]


def test_reader_accepts_legacy_plain_json(tmp_path):
    target = tmp_path / "legacy.jpt"
    expected = jpt.serialize(_deck())
    target.write_text(expected, encoding="utf-8")

    document = jpt.read_jpt(target)
    assert document == jpt.JptDocument(expected, False)


def test_first_save_atomically_upgrades_legacy_json(tmp_path):
    target = tmp_path / "legacy.jpt"
    target.write_text(jpt.serialize(_deck(_image(_data_uri()))), encoding="utf-8")

    jpt.write_packaged_jpt(
        target, jpt.read_jpt(target).text, project_root=tmp_path
    )

    assert jpt.is_packaged_jpt(target)
    assert json.loads(jpt.read_jpt(target).text)["slides"][0]["elements"][0][
        "src"
    ].startswith("jpt:assets/")


def test_empty_creation_input_becomes_a_packaged_default_deck(tmp_path):
    target = tmp_path / "empty.jpt"
    jpt.write_packaged_jpt(target, "", project_root=tmp_path)
    raw = json.loads(jpt.read_jpt(target).text)
    assert raw["title"] == "Untitled presentation"
    assert len(raw["slides"]) == 1


def test_package_output_is_deterministic(tmp_path):
    deck = _deck(_image(_data_uri()))
    first = tmp_path / "first.jpt"
    second = tmp_path / "second.jpt"
    jpt.write_packaged_jpt(first, deck, project_root=tmp_path)
    jpt.write_packaged_jpt(second, deck, project_root=tmp_path)
    assert first.read_bytes() == second.read_bytes()

    before = first.read_bytes()
    jpt.write_packaged_jpt(
        first, jpt.read_jpt(first).text, project_root=tmp_path
    )
    assert first.read_bytes() == before


def test_missing_or_escaping_asset_aborts_without_touching_target(tmp_path):
    target = tmp_path / "deck.jpt"
    original = jpt.serialize(_deck())
    target.write_text(original, encoding="utf-8")

    with pytest.raises(jpt.JptPackageError, match="does not exist"):
        jpt.write_packaged_jpt(
            target, _deck(_image("missing.png")), project_root=tmp_path
        )
    assert target.read_text(encoding="utf-8") == original

    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"outside")
    try:
        with pytest.raises(jpt.JptPackageError, match="escapes the project"):
            jpt.write_packaged_jpt(
                target, _deck(_image(str(outside))), project_root=tmp_path
            )
    finally:
        outside.unlink()
    assert target.read_text(encoding="utf-8") == original


def test_asset_symlink_cannot_escape_the_project(tmp_path):
    outside = tmp_path.parent / "secret.png"
    outside.write_bytes(b"secret")
    link = tmp_path / "linked.png"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError):
        outside.unlink()
        pytest.skip("symlinks are unavailable")
    try:
        with pytest.raises(jpt.JptPackageError, match="escapes the project"):
            jpt.write_packaged_jpt(
                tmp_path / "deck.jpt", _deck(_image("linked.png")),
                project_root=tmp_path,
            )
    finally:
        link.unlink()
        outside.unlink()

def test_unknown_safe_package_members_survive_a_rewrite(tmp_path):
    target = tmp_path / "deck.jpt"
    first = jpt.write_packaged_jpt(
        target, _deck(_image(_data_uri())), project_root=tmp_path
    )
    with zipfile.ZipFile(target, "a") as archive:
        archive.writestr("extensions/vendor.json", b'{"keep":true}')

    jpt.write_packaged_jpt(target, first.text, project_root=tmp_path)
    with zipfile.ZipFile(target) as archive:
        assert archive.read("extensions/vendor.json") == b'{"keep":true}'


def test_unsafe_or_incomplete_archives_are_rejected():
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("mimetype", jpt.PACKAGE_MIMETYPE)
        archive.writestr("deck.json", "{}")
        archive.writestr("../escape", "bad")
    with pytest.raises(jpt.JptPackageError, match="unsafe member"):
        jpt.read_jpt_bytes(stream.getvalue())

    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("mimetype", jpt.PACKAGE_MIMETYPE)
    with pytest.raises(jpt.JptPackageError, match="missing deck.json"):
        jpt.read_jpt_bytes(stream.getvalue())

    deck = jpt.serialize(_deck(_image(
        "jpt:assets/" + "0" * 64 + ".png"
    )))
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("mimetype", jpt.PACKAGE_MIMETYPE)
        archive.writestr("deck.json", deck)
    with pytest.raises(jpt.JptPackageError, match="references missing asset"):
        jpt.read_jpt_bytes(stream.getvalue())


def test_existing_internal_asset_must_exist_in_the_source_package(tmp_path):
    target = tmp_path / "deck.jpt"
    result = jpt.write_packaged_jpt(
        target, _deck(_image(_data_uri())), project_root=tmp_path
    )
    raw = json.loads(result.text)
    raw["slides"][0]["elements"][0]["src"] = "jpt:assets/" + "0" * 64 + ".png"

    with pytest.raises(jpt.JptPackageError, match="has no asset"):
        jpt.write_packaged_jpt(target, raw, project_root=tmp_path)


def test_web_player_urls_remain_explicit_external_services(tmp_path):
    target = tmp_path / "deck.jpt"
    url = "https://www.youtube.com/watch?v=example"
    result = jpt.write_packaged_jpt(
        target, _deck(_video(url)), project_root=tmp_path
    )
    assert result.external_urls == (url,)
    assert json.loads(result.text)["slides"][0]["elements"][0]["src"] == url
