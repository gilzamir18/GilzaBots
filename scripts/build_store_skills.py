"""Package the skills in assetstore_sources/skills/ into the Asset Store.

    python scripts/build_store_skills.py           # write attribution, build every zip
    python scripts/build_store_skills.py --check   # exit 1 when a zip is out of date

For each skill listed in assetstore_sources/skills/manifest.yaml it writes the
skill's ATTRIBUTION.md (the credit and the upstream licence, which the MIT
licence requires in every copy), then registers the directory with
`assetstore.register_asset`, which writes gilzabots/assetstore/skills/<id>.zip
and <id>.metadata. The metadata carries the skill's description and its
`requires`, which the store installs together with it.

Run it after editing any of these skills: tests/test_store_skill_pack.py fails
while a zip differs from its source.
"""
from __future__ import annotations

import argparse
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import yaml  # noqa: E402

from gilzabots.assetstore import register_asset, _store_dir  # noqa: E402
from gilzabots.skills import parse_skill_md, skill_error  # noqa: E402

SOURCES = os.path.join(ROOT, "assetstore_sources", "skills")
MANIFEST = os.path.join(SOURCES, "manifest.yaml")

MIT_TEXT = """Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def load_manifest() -> dict:
    with open(MANIFEST, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def attribution_text(manifest: dict, name: str) -> str:
    upstream = manifest["upstream"]
    entry = manifest["skills"][name]
    originals = ", ".join(f"`{path}`" for path in entry["original"])
    credits = f"\n{entry['credits']}\n" if entry.get("credits") else ""
    return (
        "# Attribution\n\n"
        f"This skill is adapted for GilzaBots from {originals} in {upstream['name']} "
        f"({upstream['url']}, commit {upstream['commit'][:7]}), distributed under the "
        f"{upstream['license']} License. The adaptation rewrites the instructions for "
        "GilzaBots' tools (load_skill, run_agent, ask_question, run_command, ...).\n"
        f"{credits}\n"
        f"{upstream['copyright']}\n\n"
        f"{MIT_TEXT}"
    )


def metadata_for(manifest: dict, name: str) -> dict:
    meta = parse_skill_md(os.path.join(SOURCES, name))
    if meta is None or skill_error(meta):
        raise SystemExit(f"{name}: {skill_error(meta) if meta else 'no SKILL.md'}")
    if meta["name"] != name:
        raise SystemExit(f"{name}: SKILL.md names itself '{meta['name']}'")
    entry = manifest["skills"][name] or {}
    metadata = {
        "id": name,
        "type": "skill",
        "name": name,
        "desc": meta["description"],
        "pack": manifest["pack"],
    }
    if entry.get("requires"):
        metadata["requires"] = list(entry["requires"])
    return metadata


def zip_matches(name: str) -> bool:
    """True when the store zip holds exactly the files of the source directory."""
    zip_path = os.path.join(str(_store_dir("skill")), f"{name}.zip")
    if not os.path.isfile(zip_path):
        return False
    source = os.path.join(SOURCES, name)
    expected = {}
    for dirpath, _dirs, files in os.walk(source):
        for filename in files:
            if filename.endswith(".pyc"):
                continue
            path = os.path.join(dirpath, filename)
            expected[os.path.relpath(path, SOURCES).replace(os.sep, "/")] = path
    with zipfile.ZipFile(zip_path) as archive:
        packed = {info.filename: info for info in archive.infolist() if not info.is_dir()}
        if set(packed) != set(expected):
            return False
        for arcname, path in expected.items():
            with open(path, "rb") as handle:
                if archive.read(arcname) != handle.read():
                    return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report out-of-date zips, change nothing")
    args = parser.parse_args()

    manifest = load_manifest()
    names = list(manifest["skills"])
    missing = [n for n in names if not os.path.isdir(os.path.join(SOURCES, n))]
    if missing:
        raise SystemExit(f"listed in the manifest but missing: {missing}")
    for name in names:
        for required in (manifest["skills"][name] or {}).get("requires", []):
            if required not in manifest["skills"]:
                raise SystemExit(f"{name} requires '{required}', which the pack does not contain")

    if args.check:
        stale = [n for n in names if not zip_matches(n)]
        for name in stale:
            print(f"out of date: {name}")
        return 1 if stale else 0

    for name in names:
        with open(os.path.join(SOURCES, name, "ATTRIBUTION.md"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(attribution_text(manifest, name))
        register_asset("skill", os.path.join(SOURCES, name), metadata_for(manifest, name))
        print(f"packaged {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
