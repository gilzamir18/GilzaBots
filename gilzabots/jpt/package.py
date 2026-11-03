"""The on-disk package for an GilzaBots presentation.

The slide model remains the JSON object specified by ``jpt_format.md``.  A
``.jpt`` file on disk is a ZIP container holding that JSON as ``deck.json`` and
every local image/video it references under ``assets/``.  Keeping binary data
out of the JSON is what makes a deck self-contained without making the editor
parse a film as a base64 string on every open.

Readers accept the original plain-JSON spelling as a migration input.  Writers
always emit the package spelling, so the first real save upgrades a legacy deck
atomically.  Network resources (for example a YouTube page) remain URLs: they
are services, not local files that can be copied into a document.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import tempfile
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator


PACKAGE_MIMETYPE = "application/vnd.gilzabots.presentation+zip"
MIMETYPE_MEMBER = "mimetype"
DECK_MEMBER = "deck.json"
ASSET_PREFIX = "assets/"
ASSET_URI_PREFIX = "jpt:"
MAX_DECK_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_MEMBERS = 10_000
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_ASSET_MEMBER = re.compile(r"^assets/[0-9a-f]{64}(?:\.[a-z0-9]{1,12})?$")
_DATA_URI = re.compile(r"^data:([^;,]*)(;base64)?,(.*)$", re.I | re.S)
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)

_EXTENSION_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/ogg": ".ogv",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
}


class JptPackageError(ValueError):
    """A malformed package or an asset that cannot be made self-contained."""


@dataclass(frozen=True)
class JptDocument:
    text: str
    packaged: bool


@dataclass(frozen=True)
class PackageWriteResult:
    text: str
    assets: int
    asset_bytes: int
    external_urls: tuple[str, ...]


@dataclass(frozen=True)
class AssetInfo:
    size: int
    mime: str


@dataclass(frozen=True)
class _AssetSource:
    kind: str  # bytes | path | member
    value: bytes | str
    size: int


def is_packaged_jpt(path: str | os.PathLike[str]) -> bool:
    """Whether *path* begins with a ZIP signature.

    The extension identifies the editor; the signature identifies which of the
    legacy JSON and packaged serializations is on disk.
    """
    try:
        with open(path, "rb") as handle:
            return handle.read(4) in _ZIP_SIGNATURES
    except OSError:
        return False


def read_jpt(path: str | os.PathLike[str]) -> JptDocument:
    """Read the logical JSON without loading packaged media into memory."""
    with open(path, "rb") as handle:
        signature = handle.read(4)
        if signature not in _ZIP_SIGNATURES:
            data = signature + handle.read()
            try:
                return JptDocument(data.decode("utf-8-sig"), False)
            except UnicodeDecodeError as error:
                raise JptPackageError(f"legacy JPT is not UTF-8: {error}") from error

    try:
        with zipfile.ZipFile(path, "r") as archive:
            _validate_archive(archive)
            text = _read_deck_member(archive)
    except (zipfile.BadZipFile, OSError) as error:
        raise JptPackageError(f"invalid JPT package: {error}") from error
    return JptDocument(text, True)


def read_jpt_bytes(data: bytes) -> JptDocument:
    """Read either a legacy JSON deck or a packaged deck from bytes."""
    if data[:4] not in _ZIP_SIGNATURES:
        try:
            return JptDocument(data.decode("utf-8-sig"), False)
        except UnicodeDecodeError as error:
            raise JptPackageError(f"legacy JPT is not UTF-8: {error}") from error

    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
            _validate_archive(archive)
            text = _read_deck_member(archive)
    except (zipfile.BadZipFile, OSError) as error:
        raise JptPackageError(f"invalid JPT package: {error}") from error
    return JptDocument(text, True)


def asset_member(ref: str) -> str:
    """Turn ``jpt:assets/<hash>.<ext>`` into its validated ZIP member name."""
    if not isinstance(ref, str) or not ref.startswith(ASSET_URI_PREFIX):
        raise JptPackageError(f"not an internal JPT asset reference: {ref!r}")
    member = ref[len(ASSET_URI_PREFIX):]
    if not _ASSET_MEMBER.fullmatch(member):
        raise JptPackageError(f"unsafe or malformed JPT asset reference: {ref!r}")
    return member


def read_asset(
    path: str | os.PathLike[str],
    ref: str,
    *,
    start: int = 0,
    end: int | None = None,
) -> tuple[bytes, int, str]:
    """Read an inclusive byte range from an internal asset.

    Assets are stored without ZIP compression, so seeking to a video range does
    not require decoding the preceding part of the film.
    """
    info = get_asset_info(path, ref)
    payload = b"".join(iter_asset(path, ref, start=start, end=end))
    return payload, info.size, info.mime


def get_asset_info(
    path: str | os.PathLike[str], ref: str,
) -> AssetInfo:
    """Return an internal member's uncompressed size and media type."""
    member = asset_member(ref)
    try:
        with zipfile.ZipFile(path, "r") as archive:
            _validate_archive(archive)
            try:
                member_info = archive.getinfo(member)
            except KeyError as error:
                raise JptPackageError(f"JPT package has no asset {member!r}") from error
    except zipfile.BadZipFile as error:
        raise JptPackageError(f"invalid JPT package: {error}") from error
    return AssetInfo(member_info.file_size, _mime_for_member(member))


def iter_asset(
    path: str | os.PathLike[str],
    ref: str,
    *,
    start: int = 0,
    end: int | None = None,
    chunk_size: int = 1024 * 1024,
) -> Iterator[bytes]:
    """Yield an inclusive internal-asset range in bounded chunks."""
    member = asset_member(ref)
    if chunk_size < 1:
        raise JptPackageError("asset chunk size must be positive")
    try:
        with zipfile.ZipFile(path, "r") as archive:
            _validate_archive(archive)
            try:
                member_info = archive.getinfo(member)
            except KeyError as error:
                raise JptPackageError(f"JPT package has no asset {member!r}") from error
            total = member_info.file_size
            if total <= 0:
                return
            first = max(0, int(start))
            last = total - 1 if end is None else min(total - 1, int(end))
            if first >= total or last < first:
                raise JptPackageError(
                    f"asset byte range {first}-{last} is outside a {total}-byte asset"
                )
            remaining = last - first + 1
            with archive.open(member_info, "r") as source:
                source.seek(first)
                while remaining:
                    payload = source.read(min(chunk_size, remaining))
                    if not payload:
                        raise JptPackageError(
                            f"JPT asset {member!r} ended before its declared size"
                        )
                    remaining -= len(payload)
                    yield payload
    except zipfile.BadZipFile as error:
        raise JptPackageError(f"invalid JPT package: {error}") from error


def write_packaged_jpt(
    path: str | os.PathLike[str],
    deck: str | dict[str, Any],
    *,
    project_root: str | os.PathLike[str],
) -> PackageWriteResult:
    """Write a deterministic, self-contained JPT package atomically.

    Every data URI and project-relative source in an asset-bearing field is
    moved under ``assets/`` and replaced with a content-addressed ``jpt:`` URI.
    Existing internal assets are retained only while referenced.  Missing local
    assets abort the write; leaving a broken path in a file advertised as
    self-contained would be a silent contract violation.
    """
    raw = _deck_object(deck)
    target = os.path.abspath(os.fspath(path))
    root = os.path.realpath(os.fspath(project_root))
    existing = target if os.path.isfile(target) and is_packaged_jpt(target) else None

    sources: dict[str, _AssetSource] = {}
    external_urls: set[str] = set()

    existing_archive: zipfile.ZipFile | None = None
    try:
        if existing:
            existing_archive = zipfile.ZipFile(existing, "r")
            _validate_archive(existing_archive)

        for owner, key in _asset_slots(raw):
            source = owner.get(key)
            if not source or not isinstance(source, str):
                continue
            replacement, member, asset_source = _package_source(
                source,
                project_root=root,
                existing=existing_archive,
            )
            if source.startswith(("http://", "https://")):
                external_urls.add(source)
            owner[key] = replacement
            if member and asset_source:
                sources.setdefault(member, asset_source)

        # Use the format's sanctioned serializer: key order, number spelling
        # and trailing newline stay identical on the Python and JavaScript sides.
        from .model import serialize

        text = serialize(raw)
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_DECK_BYTES:
            raise JptPackageError(
                f"deck.json is {len(encoded):,} bytes; the package limit is "
                f"{MAX_DECK_BYTES:,} bytes"
            )

        parent = os.path.dirname(target) or "."
        os.makedirs(parent, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{os.path.basename(target)}.", suffix=".tmp", dir=parent
        )
        os.close(descriptor)
        try:
            with zipfile.ZipFile(temporary, "w", allowZip64=True) as output:
                output.writestr(
                    _zip_info(MIMETYPE_MEMBER, zipfile.ZIP_STORED),
                    PACKAGE_MIMETYPE.encode("ascii"),
                )

                # Assets precede deck.json. A text-only edit therefore does not
                # shift the large unchanged byte ranges, which gives Git's delta
                # compression and file sync tools the best chance to reuse them.
                for member in sorted(sources):
                    _write_asset(output, member, sources[member], existing_archive)

                if existing_archive is not None:
                    _copy_extension_members(existing_archive, output, set(sources))

                output.writestr(_zip_info(DECK_MEMBER, zipfile.ZIP_DEFLATED), encoded)

            # Verify the temporary package before it can replace the user's file.
            checked = read_jpt(temporary)
            if checked.text != text or not checked.packaged:
                raise JptPackageError("the written package did not verify")
            # Windows will not replace an archive that is still held open.
            if existing_archive is not None:
                existing_archive.close()
                existing_archive = None
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    finally:
        if existing_archive is not None:
            existing_archive.close()

    return PackageWriteResult(
        text=text,
        assets=len(sources),
        asset_bytes=sum(source.size for source in sources.values()),
        external_urls=tuple(sorted(external_urls)),
    )


def _deck_object(deck: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(deck, str):
        if not deck.strip():
            from .model import create_deck, create_slide

            raw = create_deck()
            raw["slides"].append(create_slide())
            return raw
        try:
            raw = json.loads(deck)
        except json.JSONDecodeError as error:
            raise JptPackageError(f"deck.json is not valid JSON: {error}") from error
    else:
        raw = json.loads(json.dumps(deck, ensure_ascii=False))
    if not isinstance(raw, dict):
        raise JptPackageError("deck.json must contain a JSON object")
    return raw


def _asset_slots(deck: dict[str, Any]) -> Iterator[tuple[dict[str, Any], str]]:
    theme = deck.get("theme")
    if isinstance(theme, dict):
        yield theme, "backgroundImage"
    slides = deck.get("slides")
    if not isinstance(slides, list):
        return
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        yield slide, "backgroundImage"
        elements = slide.get("elements")
        if not isinstance(elements, list):
            continue
        for element in elements:
            if not isinstance(element, dict):
                continue
            if element.get("type") == "image":
                yield element, "src"
            elif element.get("type") == "video":
                yield element, "poster"
                yield element, "src"


def _package_source(
    source: str,
    *,
    project_root: str,
    existing: zipfile.ZipFile | None,
) -> tuple[str, str | None, _AssetSource | None]:
    if source.startswith(ASSET_URI_PREFIX):
        member = asset_member(source)
        if existing is None:
            raise JptPackageError(
                f"{source!r} references a package asset, but there is no source package"
            )
        try:
            info = existing.getinfo(member)
        except KeyError as error:
            raise JptPackageError(f"the source package has no asset {member!r}") from error
        return source, member, _AssetSource("member", member, info.file_size)

    if source.startswith("data:"):
        payload, mime = _decode_data_uri(source)
        extension = _extension_for_mime(mime)
        member = _member_for_bytes(payload, extension)
        return ASSET_URI_PREFIX + member, member, _AssetSource("bytes", payload, len(payload))

    if source.startswith(("http://", "https://")):
        return source, None, None
    if source.startswith("blob:"):
        raise JptPackageError(
            "a blob: URL exists only for the current browser session and cannot be saved"
        )

    candidate = os.path.realpath(
        source if os.path.isabs(source) else os.path.join(project_root, source)
    )
    try:
        inside = os.path.commonpath([candidate, project_root]) == project_root
    except ValueError:
        inside = False
    if not inside:
        raise JptPackageError(f"asset path escapes the project: {source!r}")
    if not os.path.isfile(candidate):
        raise JptPackageError(f"asset file does not exist: {source!r}")

    digest = _hash_file(candidate)
    extension = _safe_extension(Path(candidate).suffix)
    member = f"{ASSET_PREFIX}{digest}{extension}"
    return ASSET_URI_PREFIX + member, member, _AssetSource(
        "path", candidate, os.path.getsize(candidate)
    )


def _decode_data_uri(uri: str) -> tuple[bytes, str]:
    match = _DATA_URI.match(uri)
    if not match:
        raise JptPackageError("malformed data URI in presentation asset")
    mime = (match.group(1) or "application/octet-stream").lower()
    try:
        if match.group(2):
            payload = base64.b64decode(match.group(3), validate=True)
        else:
            payload = urllib.parse.unquote_to_bytes(match.group(3))
    except (binascii.Error, ValueError) as error:
        raise JptPackageError(f"invalid data URI payload: {error}") from error
    return payload, mime


def _member_for_bytes(payload: bytes, extension: str) -> str:
    return f"{ASSET_PREFIX}{hashlib.sha256(payload).hexdigest()}{extension}"


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extension(extension: str) -> str:
    cleaned = extension.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,12}", cleaned):
        return ".jpg" if cleaned == ".jpeg" else cleaned
    return ""


def _extension_for_mime(mime: str) -> str:
    if mime in _EXTENSION_BY_MIME:
        return _EXTENSION_BY_MIME[mime]
    guessed = mimetypes.guess_extension(mime, strict=False) or ""
    return _safe_extension(guessed)


def _mime_for_member(member: str) -> str:
    return mimetypes.guess_type(member)[0] or "application/octet-stream"


def _zip_info(name: str, compression: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, _FIXED_ZIP_TIME)
    info.compress_type = compression
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _write_asset(
    output: zipfile.ZipFile,
    member: str,
    source: _AssetSource,
    existing: zipfile.ZipFile | None,
) -> None:
    info = _zip_info(member, zipfile.ZIP_STORED)
    if source.kind == "bytes":
        output.writestr(info, source.value)
        return
    if source.kind == "path":
        input_handle: BinaryIO = open(os.fspath(source.value), "rb")
    elif source.kind == "member" and existing is not None:
        input_handle = existing.open(os.fspath(source.value), "r")
    else:  # pragma: no cover - construction above makes this unreachable.
        raise JptPackageError(f"cannot materialize JPT asset {member!r}")
    with input_handle, output.open(info, "w", force_zip64=source.size >= zipfile.ZIP64_LIMIT) as target:
        shutil.copyfileobj(input_handle, target, length=1024 * 1024)


def _copy_extension_members(
    existing: zipfile.ZipFile,
    output: zipfile.ZipFile,
    referenced_assets: set[str],
) -> None:
    reserved = {MIMETYPE_MEMBER, DECK_MEMBER, *referenced_assets}
    for info in sorted(existing.infolist(), key=lambda item: item.filename):
        name = info.filename
        if name in reserved or name.startswith(ASSET_PREFIX) or info.is_dir():
            continue
        # Unknown package namespaces survive a round trip, matching the JSON
        # model's unknown-key guarantee. They are copied safely and canonically.
        with existing.open(info, "r") as source, output.open(
            _zip_info(name, zipfile.ZIP_STORED), "w", force_zip64=info.file_size >= zipfile.ZIP64_LIMIT
        ) as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)


def _validate_archive(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_PACKAGE_MEMBERS:
        raise JptPackageError(
            f"JPT package has {len(infos):,} members; limit is {MAX_PACKAGE_MEMBERS:,}"
        )
    names: set[str] = set()
    for info in infos:
        name = info.filename
        if name in names:
            raise JptPackageError(f"JPT package contains duplicate member {name!r}")
        names.add(name)
        if not _safe_member_name(name):
            raise JptPackageError(f"JPT package contains unsafe member {name!r}")
        if name.startswith(ASSET_PREFIX) and not _ASSET_MEMBER.fullmatch(name):
            raise JptPackageError(f"JPT package contains malformed asset member {name!r}")
        if name.startswith(ASSET_PREFIX) and info.compress_type != zipfile.ZIP_STORED:
            raise JptPackageError(
                f"JPT asset member must be stored without compression: {name!r}"
            )
        if info.flag_bits & 0x1:
            raise JptPackageError(f"encrypted JPT member is not supported: {name!r}")

    try:
        mime_info = archive.getinfo(MIMETYPE_MEMBER)
        if mime_info.file_size > 256:
            raise JptPackageError("JPT mimetype member is unreasonably large")
        mime = archive.read(mime_info).decode("ascii")
    except (KeyError, UnicodeDecodeError) as error:
        raise JptPackageError("JPT package is missing a valid mimetype member") from error
    if mime != PACKAGE_MIMETYPE:
        raise JptPackageError(f"not an GilzaBots JPT package (mimetype is {mime!r})")
    try:
        deck_info = archive.getinfo(DECK_MEMBER)
    except KeyError as error:
        raise JptPackageError("JPT package is missing deck.json") from error
    if deck_info.file_size > MAX_DECK_BYTES:
        raise JptPackageError(
            f"deck.json is {deck_info.file_size:,} bytes; limit is {MAX_DECK_BYTES:,}"
        )


def _read_deck_member(archive: zipfile.ZipFile) -> str:
    try:
        payload = archive.read(DECK_MEMBER)
        text = payload.decode("utf-8")
        raw = json.loads(text)
    except UnicodeDecodeError as error:
        raise JptPackageError(f"deck.json is not UTF-8: {error}") from error
    except json.JSONDecodeError as error:
        raise JptPackageError(f"deck.json is not valid JSON: {error}") from error
    if not isinstance(raw, dict):
        raise JptPackageError("deck.json must contain a JSON object")
    for owner, key in _asset_slots(raw):
        source = owner.get(key)
        if not isinstance(source, str) or not source.startswith(ASSET_URI_PREFIX):
            continue
        member = asset_member(source)
        try:
            archive.getinfo(member)
        except KeyError as error:
            raise JptPackageError(
                f"deck.json references missing asset {member!r}"
            ) from error
    return text


def _safe_member_name(name: str) -> bool:
    if not name or "\\" in name or name.startswith("/"):
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and all(part not in ("", ".", "..") for part in path.parts)
