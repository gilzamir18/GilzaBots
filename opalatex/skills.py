import os


# ===========================================================================
# Skills-oriented architecture (target design) — see docs/specs/06
# ---------------------------------------------------------------------------
# A skill is a DIRECTORY containing a SKILL.md (Anthropic format). Frontmatter
# carries only `name`, `description`, and an optional `model`. Discovery loads
# Level-1 metadata (name + description) into the MemGPT system prompt; the body
# and bundled resources/scripts (Level 2/3) are read on demand by the sub-agent.
# ===========================================================================

# The chat-orchestrator skill is always loaded regardless of skills.yaml.
MANDATORY_SKILLS = ("chat-orchestrator", "command-line")

# A skill's `role` frontmatter decides who carries it out (PROJECT_DESIGN 2.23).
#
# * "helper": an auxiliary job -- read a long document, extract what was asked,
#   condense a log, search the web -- run by an ephemeral helper agent through
#   `run_skill`. The helper returns data to the main agent and never changes the
#   project; it may keep scratch notes under `<project>/.opalatex/`.
# * "instructions" (the default): the main agent loads the body with
#   `load_skill` and does the work itself, with its own tools. A skill that
#   changes the project is one of these, so every change is made by the agent
#   that answers for it and sees each of its tool results.
HELPER_ROLE = "helper"
INSTRUCTIONS_ROLE = "instructions"
SKILL_ROLES = (HELPER_ROLE, INSTRUCTIONS_ROLE)

# Directory name of the skill manifest file inside each skill directory.
SKILL_MANIFEST = "SKILL.md"


def skill_search_dirs(project_path: str = "") -> list[str]:
    """Return directories that may contain skill subdirectories, in priority order.

    Target design (docs/specs/06 §5):
      1. <project>/skills/
      2. <project>/.opalatex/skills/
      3. ~/.opalatex/skills/
      4. <package>/skills/  and  <repo-root>/skills/  (bundled / mandatory)
    """
    dirs: list[str] = []
    if project_path:
        dirs.append(os.path.join(project_path, "skills"))
        dirs.append(os.path.join(project_path, ".opalatex", "skills"))
    dirs.append(os.path.expanduser("~/.opalatex/skills"))
    package_dir = os.path.dirname(os.path.abspath(__file__))
    dirs.append(os.path.join(package_dir, "skills"))
    repo_root = os.path.dirname(package_dir)
    dirs.append(os.path.join(repo_root, "skills"))
    # De-duplicate while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for d in dirs:
        ad = os.path.abspath(d)
        if ad not in seen:
            seen.add(ad)
            out.append(d)
    return out


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a SKILL.md into (frontmatter dict, body).

    Accepts standard YAML frontmatter delimited by leading/trailing `---`.
    Only flat `key: value` pairs are read (name, description, model); this avoids
    a hard YAML dependency for the common case. Returns ({}, text) when absent.
    """
    fm: dict[str, str] = {}
    stripped = text.lstrip("﻿")  # tolerate BOM
    if not stripped.startswith("---"):
        return fm, text
    # Find the closing delimiter line
    lines = stripped.splitlines()
    if not lines or lines[0].strip() != "---":
        return fm, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return fm, text
    for line in lines[1:end]:
        if not line.strip() or ":" not in line:
            continue
        key, _, val = line.partition(":")
        fm[key.strip().lower()] = val.strip()
    body = "\n".join(lines[end + 1:]).strip()
    return fm, body


def parse_skill_md(skill_dir: str, profile: str = "full") -> dict | None:
    """Parse a skill directory's SKILL.md into a metadata dict, or None.

    Returned dict:
      name, description, model (optional, "" if absent),
      role ("helper" or "instructions", the default; see HELPER_ROLE),
      dir (absolute path to the skill directory),
      manifest (absolute path to SKILL.md),
      body (Level-2 instructions text).
    The directory name is the canonical fallback for `name`.

    *profile* (see `opalatex/prompt_profiles.py`) optionally overrides the body
    with a sibling ``SKILL.<profile>.md`` file when one exists next to
    ``SKILL.md`` — e.g. ``SKILL.light.md`` for a condensed body. Frontmatter
    (name/description/model/extends/icon) always comes from the canonical
    SKILL.md so routing metadata never depends on which profile is active; the
    variant file needs no frontmatter of its own. "full" (the default) never
    looks for a variant, so existing skills with no variant file are
    unaffected.
    """
    manifest = os.path.join(skill_dir, SKILL_MANIFEST)
    if not os.path.isfile(manifest):
        return None
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return None
    fm, body = _parse_frontmatter(text)

    profile = (profile or "full").strip().lower()
    if profile and profile != "full":
        variant_path = os.path.join(skill_dir, f"SKILL.{profile}.md")
        if os.path.isfile(variant_path):
            try:
                with open(variant_path, "r", encoding="utf-8") as vf:
                    variant_text = vf.read()
                _, variant_body = _parse_frontmatter(variant_text)
                body = variant_body or body
            except Exception:
                pass

    name = fm.get("name") or os.path.basename(os.path.normpath(skill_dir))
    description = fm.get("description", "")
    model = fm.get("model", "")
    extends = fm.get("extends", "")
    icon = fm.get("icon", "")
    # An unknown role is kept as written, not coerced: `skill_role` reports it,
    # and both run_skill and load_skill refuse the skill with that diagnostic
    # instead of guessing which agent was meant to run it.
    role = (fm.get("role", "") or INSTRUCTIONS_ROLE).strip().lower()
    return {
        "name": name,
        "description": description,
        "model": model,
        "extends": extends,
        "icon": icon,
        "role": role,
        "dir": os.path.abspath(skill_dir),
        "manifest": os.path.abspath(manifest),
        "body": body,
    }


def resolve_skill_icon_path(skill_meta: dict) -> str | None:
    """Return the absolute path to a skill's icon file, or None.

    The `icon` frontmatter field names a file expected inside the skill's own
    directory. Missing field, missing file, or an `icon` value that escapes
    the skill directory all resolve to None so callers can fall back to a
    default icon.
    """
    icon_name = skill_meta.get("icon")
    skill_dir = skill_meta.get("dir")
    if not icon_name or not skill_dir:
        return None
    base = os.path.abspath(skill_dir)
    icon_path = os.path.abspath(os.path.join(base, icon_name))
    if os.path.commonpath([base, icon_path]) != base or not os.path.isfile(icon_path):
        return None
    return icon_path


def discover_skills(project_path: str = "") -> list[dict]:
    """Discover all skill directories (containing SKILL.md) across search dirs.

    First occurrence of a skill name wins (project skills shadow bundled ones).
    Returns a list of metadata dicts (see parse_skill_md).
    """
    # Read explicit skill declarations; None means no skills.yaml (load all except store).
    declared = read_skills_yaml(project_path)  # list or None
    found: list[dict] = []
    seen_names: set[str] = set()
    for base in skill_search_dirs(project_path):
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            skill_dir = os.path.join(base, entry)
            if not os.path.isdir(skill_dir):
                continue
            meta = parse_skill_md(skill_dir)
            if meta is None or meta["name"] in seen_names:
                continue
            seen_names.add(meta["name"])
            found.append(meta)
    return found


def read_skills_yaml(project_path: str) -> list[str] | None:
    """Return the list of skill names declared in <project>/skills.yaml, or None.

    None means the file is absent → caller loads all discovered skills.
    An empty/!malformed file yields an empty list → only mandatory skills load.
    """
    path = os.path.join(project_path, "skills.yaml")
    if not os.path.isfile(path):
        return None
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        declared = data.get("skills", []) if isinstance(data, dict) else []
        return [str(s).strip() for s in declared if str(s).strip()]
    except Exception:
        return []


def write_skills_yaml(project_path: str, skill_names: list[str]) -> None:
    """Write the active skill set to <project>/skills.yaml.

    skills.yaml is the single source of truth for which skills' Level-1 metadata
    the MemGPT loads (docs/specs/06 §5). Mandatory skills are always implied; we
    do not persist them here.
    """
    import yaml
    declared = [s for s in dict.fromkeys(skill_names) if s not in MANDATORY_SKILLS]
    os.makedirs(project_path, exist_ok=True)
    path = os.path.join(project_path, "skills.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump({"skills": declared}, f, allow_unicode=True, sort_keys=False)


def add_skill_to_project(project_path: str, skill_name: str) -> tuple[bool, str]:
    """Add a skill to the project's skills.yaml. Returns (changed, message)."""
    skill_dir = find_skill_dir(skill_name, project_path)
    if skill_dir is None:
        return False, f"skill '{skill_name}' not found in any search dir."
    if skill_name in MANDATORY_SKILLS:
        return False, f"skill '{skill_name}' is always active (mandatory)."
        
    meta = parse_skill_md(skill_dir)
    if meta and meta.get("extends"):
        parent_name = meta["extends"]
        active = [s["name"] for s in active_skills(project_path)]
        if parent_name not in active:
            return False, f"Cannot add skill '{skill_name}' because it extends '{parent_name}', which is not active."
            
    # Absent skills.yaml means "only mandatory active" → start from an empty set.
    declared = read_skills_yaml(project_path) or []
    if skill_name in declared:
        return False, f"skill '{skill_name}' is already active."
    declared.append(skill_name)
    write_skills_yaml(project_path, declared)
    return True, f"skill '{skill_name}' added."


def remove_skill_from_project(project_path: str, skill_name: str) -> tuple[bool, str]:
    """Remove a skill from the project's skills.yaml. Returns (changed, message)."""
    if skill_name in MANDATORY_SKILLS:
        return False, f"skill '{skill_name}' is mandatory and cannot be removed."
    declared = read_skills_yaml(project_path) or []
    if skill_name not in declared:
        return False, f"skill '{skill_name}' is not active."
    declared = [s for s in declared if s != skill_name]
    write_skills_yaml(project_path, declared)
    return True, f"skill '{skill_name}' removed."


def active_skills(project_path: str = "") -> list[dict]:
    """Return the skills active for a project.

    A project only loads the mandatory skills plus the ones it explicitly opts into
    via <project>/skills.yaml. This keeps new projects minimal — no development skill
    is loaded unless the user adds it (with /addskill or by listing it in skills.yaml).

    Rules:
      - Mandatory skills (chat-orchestrator) are always active.
      - No skills.yaml (or empty) → only the mandatory skills.
      - With skills.yaml → mandatory + the declared skills.
    """
    discovered = discover_skills(project_path)
    declared = read_skills_yaml(project_path) if project_path else None
    allowed = set(MANDATORY_SKILLS) | set(declared or [])
    return [s for s in discovered if s["name"] in allowed]


def level1_metadata(skills: list[dict]) -> str:
    """Render Level-1 metadata (name + description) for the MemGPT system prompt."""
    return "\n".join(f"- {s['name']}: {s['description']}".rstrip() for s in skills)


def skill_role(skill_meta: dict) -> str:
    """The skill's role as declared ("instructions" when the field is absent)."""
    return str(skill_meta.get("role") or INSTRUCTIONS_ROLE).strip().lower()


def is_helper_skill(skill_meta: dict) -> bool:
    return skill_role(skill_meta) == HELPER_ROLE


def local_skill_dir(skill_name: str, project_path: str) -> str | None:
    """Return `<project>/.opalatex/skills/<skill_name>` when that copy exists.

    This is the copy the Asset Store installs, and the one that shadows every
    other search dir (see `skill_search_dirs`).
    """
    if not project_path or not skill_name:
        return None
    candidate = os.path.abspath(os.path.join(project_path, ".opalatex", "skills", skill_name))
    if os.path.isfile(os.path.join(candidate, SKILL_MANIFEST)):
        return candidate
    return None


def shadowed_skill_dirs(skill_name: str, project_path: str = "") -> list[str]:
    """Return the skill dirs named *skill_name* that lose to the active one.

    Discovery keeps the first hit across the search dirs, so a project-local copy
    silently freezes the bundled skill of the same name at whatever it was when
    it was installed. A non-empty list here is what makes that shadowing visible
    to the user instead of leaving them with a copy that never gets updates.
    """
    hits: list[str] = []
    for base in skill_search_dirs(project_path):
        skill_dir = os.path.join(base, skill_name)
        if os.path.isfile(os.path.join(skill_dir, SKILL_MANIFEST)):
            hits.append(os.path.abspath(skill_dir))
    return hits[1:]


def find_skill_dir(skill_name: str, project_path: str = "") -> str | None:
    """Return the absolute path of a skill directory by name, or None."""
    for s in discover_skills(project_path):
        if s["name"] == skill_name:
            return s["dir"]
    return None
