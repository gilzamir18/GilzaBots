"""AssetStore: local repository of reusable assets (skills and LaTeX templates).

Structure
---------
gilzabots/assetstore/
    skills/
        <ID>.zip        - full skill directory tree
        <ID>.metadata   - YAML: id, type, name, desc

templates/                  (repo root; `<package>/assetstore/templates` also works)
    <ID>.zip            - packaged template directory (.tex files, figures, ...)
    <ID>.yaml           - YAML sidecar with the same stem: name, description,
                          version, and an optional `active` flag

Installation targets (relative to project root)
---------
skill    -> <project>/.gilzabots/skills/<name>/
template -> <project>/            (the zip is unpacked at the project root)
"""

import os
import shutil
import zipfile
from pathlib import Path
from typing import Optional

import yaml

_PACKAGE_DIR = Path(__file__).parent
_STORE_ROOT = _PACKAGE_DIR / "assetstore"
_REPO_ROOT = _PACKAGE_DIR.parent

VALID_TYPES = {"skill", "template"}

# Metadata sidecar suffixes accepted for each asset type. Skills keep the
# historical `.metadata` name; templates use a `.yaml` with the same stem as the
# zip, which is how template authors package them.
_META_SUFFIXES = {
    "skill": (".metadata",),
    "template": (".yaml", ".yml"),
}

# Fields read from a template sidecar. Authors write these files by hand, so key
# lookup is case-insensitive and `desc` is accepted as a shorthand for
# `description`, matching the field name skill metadata already uses.
_TEMPLATE_FIELD_ALIASES = {
    "name": ("name",),
    "desc": ("description", "desc"),
    "version": ("version",),
    "active": ("active",),
    "icon": ("icon",),
}


def _store_dirs(asset_type: str) -> list[Path]:
    """Return the directories that may hold assets of *asset_type*.

    Templates are searched both under the package's assetstore and at the repo
    root `templates/` directory, which is where template packages live in the
    source tree and where PyInstaller drops them in a frozen build.
    """
    if asset_type == "template":
        return [_STORE_ROOT / "templates", _REPO_ROOT / "templates"]
    return [_STORE_ROOT / (asset_type + "s")]


def _store_dir(asset_type: str) -> Path:
    """Return the writable store directory for *asset_type* (registration target)."""
    return _store_dirs(asset_type)[0]


def _parse_metadata(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _pick(raw: dict, aliases: tuple[str, ...]) -> Optional[object]:
    """Return the first alias present in *raw*, matched case-insensitively."""
    lowered = {str(k).strip().lower(): v for k, v in raw.items()}
    for alias in aliases:
        if alias in lowered:
            return lowered[alias]
    return None


def _normalize_template_meta(raw: dict, asset_id: str) -> dict:
    """Map a template sidecar onto the same shape skill metadata already uses.

    The sidecar may either nest its fields under a `template:` key or list them
    at the top level; both forms are read the same way.
    """
    body = raw.get("template") if isinstance(raw.get("template"), dict) else raw
    meta: dict = {"id": asset_id, "type": "template"}
    for field, aliases in _TEMPLATE_FIELD_ALIASES.items():
        value = _pick(body, aliases)
        if value is not None:
            meta[field] = value
    meta["name"] = str(meta.get("name") or asset_id)
    meta["desc"] = str(meta.get("desc") or "")
    meta["version"] = str(meta.get("version") or "")
    return meta


def _iter_assets(asset_type: str) -> list[dict]:
    """Return metadata dictionaries for all assets of the given type."""
    results: list[dict] = []
    seen_ids: set[str] = set()
    for d in _store_dirs(asset_type):
        if not d.exists():
            continue
        for suffix in _META_SUFFIXES.get(asset_type, (".metadata",)):
            for meta_file in sorted(d.glob(f"*{suffix}")):
                asset_id = meta_file.stem
                if asset_id in seen_ids:
                    continue
                zip_path = meta_file.with_suffix(".zip")
                try:
                    raw = _parse_metadata(meta_file)
                except Exception:
                    continue
                if asset_type == "template":
                    if not zip_path.exists():
                        # A sidecar with no package is not installable; skipping
                        # it keeps the store honest about what it can deliver.
                        continue
                    meta = _normalize_template_meta(raw, asset_id)
                    if meta.pop("active", True) is False:
                        continue
                else:
                    meta = raw
                meta["_zip"] = zip_path
                meta["_meta"] = meta_file
                seen_ids.add(asset_id)
                results.append(meta)
    return results


def _match(meta: dict, desc: str) -> bool:
    """Return True if desc matches the asset id or description."""
    desc_l = desc.lower()
    return (
        meta.get("id", "").lower() == desc_l
        or meta.get("desc", "").lower() == desc_l
    )


def list_assets(asset_type: Optional[str] = None) -> list[dict]:
    """Return all assets, optionally filtered by type."""
    types = [asset_type] if asset_type else sorted(VALID_TYPES)
    result = []
    for t in types:
        if t in VALID_TYPES:
            result.extend(_iter_assets(t))
    return result


def find_assets(asset_type: str, desc: str) -> list[dict]:
    """Return matching assets. desc='*' returns all assets of the type."""
    assets = _iter_assets(asset_type) if asset_type in VALID_TYPES else []
    if desc == "*":
        return assets
    return [a for a in assets if _match(a, desc)]


def resolve_asset_icon_path(meta: dict) -> Optional[Path]:
    """Return the absolute path to an asset's icon file, or None.

    The `icon` metadata field names a file expected next to the asset's
    `.metadata`/`.zip` pair in the store. Missing field, missing file, or an
    `icon` value that escapes the store directory all resolve to None so
    callers can fall back to a default icon.
    """
    icon_name = meta.get("icon")
    meta_path: Optional[Path] = meta.get("_meta")
    if not icon_name or not meta_path:
        return None
    store_dir = meta_path.parent.resolve()
    icon_path = (store_dir / icon_name).resolve()
    if not icon_path.is_relative_to(store_dir) or not icon_path.is_file():
        return None
    return icon_path


def installed_skill_dir(meta: dict, project_path: str) -> Optional[Path]:
    """Return the project-local install directory of a skill asset, or None.

    A skill installed into `<project>/.gilzabots/skills/<name>/` shadows the
    bundled copy of the same name (see `skills.skill_search_dirs`), so it is the
    one that actually runs and the one an update has to replace.
    """
    if meta.get("type", "") != "skill":
        return None
    zip_path: Optional[Path] = meta.get("_zip")
    name = meta.get("name") or (zip_path.stem if zip_path else "")
    if not name:
        return None
    dest_root = (Path(os.path.abspath(project_path)) / ".gilzabots" / "skills").resolve()
    candidate = (dest_root / name).resolve()
    # A crafted asset name must not point the caller at a directory outside the
    # project's skills folder -- the update path deletes what this returns.
    if not candidate.is_relative_to(dest_root) or candidate == dest_root:
        return None
    return candidate if candidate.is_dir() else None


def asset_matches_install(meta: dict, project_path: str) -> bool:
    """True when the project-local copy is identical to the catalog asset.

    False means the local copy has drifted -- an older catalog version, or files
    edited/added/removed in the project -- so the Asset Store can offer to
    refresh it. Missing local copy or missing zip is not a match.
    """
    local = installed_skill_dir(meta, project_path)
    zip_path: Optional[Path] = meta.get("_zip")
    if local is None or zip_path is None or not zip_path.exists():
        return False

    root = local.parent
    expected: set[Path] = set()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            target = (root / info.filename).resolve()
            if not target.is_relative_to(local):
                continue
            expected.add(target)
            if not target.is_file() or target.stat().st_size != info.file_size:
                return False
            with zf.open(info) as packed, open(target, "rb") as installed:
                if packed.read() != installed.read():
                    return False

    # Files the local copy has and the asset does not count as drift too: an
    # update deletes them, so reporting "up to date" here would be a lie.
    present = {p.resolve() for p in local.rglob("*") if p.is_file()}
    return present == expected


def _is_archive_junk(name: str) -> bool:
    """True for archiver bookkeeping entries that are not template content.

    Zips produced on macOS carry an `__MACOSX/` tree of `._name` resource forks;
    unpacking those into a user's project root would litter it with files the
    template author never authored.
    """
    parts = Path(name).parts
    if any(p == "__MACOSX" for p in parts):
        return True
    base = parts[-1] if parts else name
    return base.startswith("._") or base == ".DS_Store"


def template_entries(meta: dict) -> list[str]:
    """Return the file paths a template installs, relative to the project root."""
    zip_path: Optional[Path] = meta.get("_zip")
    if zip_path is None or not zip_path.exists():
        return []
    with zipfile.ZipFile(zip_path, "r") as zf:
        return [
            info.filename for info in zf.infolist()
            if not info.is_dir() and not _is_archive_junk(info.filename)
        ]


def template_conflicts(meta: dict, project_path: str) -> list[str]:
    """Return the template's entries that already exist in the project.

    Installing overwrites them, so the caller can warn before doing so.
    """
    project = Path(os.path.abspath(project_path))
    return [name for name in template_entries(meta) if (project / name).exists()]


def template_is_installed(meta: dict, project_path: str) -> bool:
    """True when every file the template carries is already in the project."""
    entries = template_entries(meta)
    if not entries:
        return False
    return len(template_conflicts(meta, project_path)) == len(entries)


def _extract_template(meta: dict, project_path: str, overwrite: bool) -> str:
    zip_path: Path = meta["_zip"]
    project = Path(os.path.abspath(project_path)).resolve()
    project.mkdir(parents=True, exist_ok=True)

    if not overwrite:
        conflicts = template_conflicts(meta, project_path)
        if conflicts:
            shown = ", ".join(sorted(conflicts)[:5])
            more = "" if len(conflicts) <= 5 else f" (+{len(conflicts) - 5} more)"
            raise FileExistsError(
                f"template '{meta.get('name', meta.get('id', '?'))}' would overwrite "
                f"existing files: {shown}{more}"
            )

    written = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir() or _is_archive_junk(info.filename):
                continue
            target = (project / info.filename).resolve()
            # Zip-slip guard: an entry with `..` or an absolute path must not be
            # able to write outside the project the user asked to install into.
            if not target.is_relative_to(project):
                raise ValueError(
                    f"template entry '{info.filename}' escapes the project directory"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            written += 1

    name = meta.get("name", zip_path.stem)
    return f"template '{name}' installed into {project} ({written} files)"


def install_asset(
    meta: dict,
    project_path: str,
    replace: bool = False,
    overwrite: bool = False,
) -> str:
    """Extract an asset into project_path and return a summary.

    Skills land in `<project>/.gilzabots/skills/<name>/`; with *replace*, the
    existing project-local copy is deleted first, so files dropped from the asset
    since the last install do not survive the update. Plain extraction only
    overwrites the entries the zip still carries.

    Templates are unpacked at the project root. Because that can clobber the
    user's own files, an install that would overwrite anything raises
    FileExistsError unless *overwrite* is set.
    """
    asset_type = meta.get("type", "")

    zip_path: Path = meta["_zip"]
    if not zip_path.exists():
        raise FileNotFoundError(f"Zip not found: {zip_path}")

    project = Path(os.path.abspath(project_path))

    if asset_type == "skill":
        dest = project / ".gilzabots" / "skills"
        dest.mkdir(parents=True, exist_ok=True)
        replaced = False
        if replace:
            existing = installed_skill_dir(meta, project_path)
            if existing is not None:
                shutil.rmtree(existing)
                replaced = True
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(dest)
        skill_name = meta.get("name", zip_path.stem)
        verb = "updated" if replaced else "installed"
        return f"skill '{skill_name}' {verb} at {dest / skill_name}"

    if asset_type == "template":
        return _extract_template(meta, project_path, overwrite=overwrite or replace)

    raise ValueError(f"Unknown asset type '{asset_type}'")


def skill_requirements(meta: dict) -> list[dict]:
    """The skill assets *meta* requires, transitively, dependencies first.

    A skill's `.metadata` may list `requires: [<skill id>, ...]`: skills whose
    instructions it loads (`load_skill`) and that it cannot work without, as a
    Claude Code plugin ships the skills that call each other together. Raises
    ValueError when a required id is not in the store, rather than installing a
    skill that would fail at run time in the user's project.
    """
    by_id = {a.get("id"): a for a in _iter_assets("skill")}
    ordered: list[dict] = []
    seen: set[str] = {str(meta.get("id"))}

    def visit(current: dict) -> None:
        requires = current.get("requires") or []
        if isinstance(requires, str):
            requires = [requires]
        for required_id in requires:
            required_id = str(required_id)
            if required_id in seen:
                continue
            seen.add(required_id)
            required = by_id.get(required_id)
            if required is None:
                raise ValueError(
                    f"skill '{current.get('id')}' requires '{required_id}', which is not in the Asset Store."
                )
            visit(required)
            ordered.append(required)

    visit(meta)
    return ordered


def install_skill_with_requirements(meta: dict, project_path: str, replace: bool = False) -> list[tuple[str, str]]:
    """Install a skill asset and the skills it requires; return (name, summary) pairs.

    A required skill the project already reaches -- installed locally, bundled
    or in the user's skill folder -- is left as it is and not listed: its copy
    may hold the user's own edits. The requested skill itself is always
    installed (with *replace*, refreshed).
    """
    from .skills import find_skill_dir

    installed: list[tuple[str, str]] = []
    for required in skill_requirements(meta):
        name = str(required.get("name") or required.get("id"))
        if find_skill_dir(name, project_path) is not None:
            continue
        installed.append((name, install_asset(required, project_path)))
    name = str(meta.get("name") or meta.get("id"))
    installed.append((name, install_asset(meta, project_path, replace=replace)))
    return installed


def register_asset(asset_type: str, source_path: str, metadata: dict) -> Path:
    """Package a local directory as an asset and register it in the store."""
    if asset_type not in VALID_TYPES:
        raise ValueError(f"type must be one of {VALID_TYPES}")

    asset_id = metadata.get("id")
    if not asset_id:
        raise ValueError("metadata must have an 'id' field")

    store_dir = _store_dir(asset_type)
    store_dir.mkdir(parents=True, exist_ok=True)

    zip_path = store_dir / f"{asset_id}.zip"
    meta_path = store_dir / f"{asset_id}{_META_SUFFIXES[asset_type][0]}"

    source = Path(source_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if source.is_dir():
            for f in sorted(source.rglob("*")):
                if f.is_file() and "__pycache__" not in str(f) and not f.name.endswith(".pyc"):
                    zf.write(f, f.relative_to(source.parent))
        else:
            zf.write(source, source.name)

    with open(meta_path, "w", encoding="utf-8") as f:
        yaml.dump(metadata, f, allow_unicode=True, default_flow_style=False)

    return zip_path
