import os


# ===========================================================================
# Skills-oriented architecture (target design) — see docs/specs/06
# ---------------------------------------------------------------------------
# A skill is a DIRECTORY containing a SKILL.md (Anthropic format). Frontmatter is
# YAML: Claude Code's `name`, `description`, `disable-model-invocation`,
# `user-invocable` and `argument-hint`, plus GilzaBots' `model`, `role`,
# `extends` and `icon`. Discovery loads Level-1 metadata (name + description)
# into the MemGPT system prompt; the body and bundled resources/scripts
# (Level 2/3) are read on demand.
# ===========================================================================

# The chat-orchestrator skill is always loaded regardless of skills.yaml.
MANDATORY_SKILLS = ("chat-orchestrator", "command-line")

# A skill's `role` frontmatter decides who carries it out (PROJECT_DESIGN 2.23).
#
# * "helper": an auxiliary job -- read a long document, extract what was asked,
#   condense a log, search the web -- run by an ephemeral helper agent through
#   `run_skill`. The helper returns data to the main agent and never changes the
#   project; it may keep scratch notes under `<project>/.gilzabots/`.
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
      2. <project>/.gilzabots/skills/
      3. ~/.gilzabots/skills/
      4. <package>/skills/  and  <repo-root>/skills/  (bundled / mandatory)
    """
    dirs: list[str] = []
    if project_path:
        dirs.append(os.path.join(project_path, "skills"))
        dirs.append(os.path.join(project_path, ".gilzabots", "skills"))
    dirs.append(os.path.expanduser("~/.gilzabots/skills"))
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


class FrontmatterError(ValueError):
    """A SKILL.md whose frontmatter is not a YAML mapping."""


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a SKILL.md into (frontmatter dict, body).

    The frontmatter is YAML delimited by leading/trailing `---`, read with
    ``yaml.safe_load`` exactly as Claude Code reads a skill's: quoted values lose
    their quotes, block scalars span lines, and booleans are booleans. Keys are
    lowercased. Returns ({}, text) when there is no frontmatter.

    Raises FrontmatterError when the block is not valid YAML or not a mapping.
    The old reader split each line at its first colon; YAML rejects some lines
    it accepted (an unquoted value that itself contains ": "), and such a file is
    reported with the parser's message rather than read by a second, guessing
    reader (PROJECT_DESIGN 2.29).
    """
    stripped = text.lstrip("﻿")  # tolerate BOM
    if not stripped.startswith("---"):
        return {}, text
    lines = stripped.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    import yaml

    raw = "\n".join(lines[1:end])
    try:
        data = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError as exc:
        detail = " ".join(str(exc).split())
        raise FrontmatterError(
            f"the frontmatter is not valid YAML ({detail}). Quote a value that contains "
            "': ' or starts with a special character."
        ) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise FrontmatterError("the frontmatter must be a YAML mapping of `key: value` pairs.")
    fm = {str(key).strip().lower(): value for key, value in data.items()}
    body = "\n".join(lines[end + 1:]).strip()
    return fm, body


def _fm_text(fm: dict, key: str) -> str:
    """A frontmatter value as text ("" when absent or null)."""
    value = fm.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _fm_flag(fm: dict, key: str, default: bool) -> bool:
    """A boolean frontmatter field; anything but true/false is an error."""
    if key not in fm or fm[key] is None:
        return default
    value = fm[key]
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "false"):
        return text == "true"
    raise FrontmatterError(f"`{key}` must be true or false, not {value!r}.")


def parse_skill_md(skill_dir: str, profile: str = "full") -> dict | None:
    """Parse a skill directory's SKILL.md into a metadata dict, or None.

    Returned dict:
      name, description, model (optional, "" if absent),
      role ("helper" or "instructions", the default; see HELPER_ROLE),
      disable_model_invocation (Claude Code's `disable-model-invocation`: only
        the user reaches the skill, by typing `/name`; the agent's catalog and
        `load_skill` leave it out),
      user_invocable (Claude Code's `user-invocable`, default true: whether
        `/name` reaches it),
      argument_hint (Claude Code's `argument-hint`, shown after `/name`),
      error ("" when the SKILL.md is usable, else why it is not),
      dir (absolute path to the skill directory),
      manifest (absolute path to SKILL.md),
      body (Level-2 instructions text).
    The directory name is the canonical fallback for `name`. A skill whose
    frontmatter cannot be read is still returned, with `error` set and an empty
    body, so it is listed as unusable instead of vanishing.

    *profile* (see `gilzabots/prompt_profiles.py`) optionally overrides the body
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
    try:
        fm, body = _parse_frontmatter(text)
    except FrontmatterError as exc:
        return _unusable_skill(skill_dir, manifest, str(exc))

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

    try:
        disable_model_invocation = _fm_flag(fm, "disable-model-invocation", False)
        user_invocable = _fm_flag(fm, "user-invocable", True)
    except FrontmatterError as exc:
        return _unusable_skill(skill_dir, manifest, str(exc), name=_fm_text(fm, "name"))

    name = _fm_text(fm, "name") or os.path.basename(os.path.normpath(skill_dir))
    # An unknown role is kept as written, not coerced: `skill_role` reports it,
    # and both run_skill and load_skill refuse the skill with that diagnostic
    # instead of guessing which agent was meant to run it.
    role = (_fm_text(fm, "role") or INSTRUCTIONS_ROLE).lower()
    return {
        "name": name,
        "description": _fm_text(fm, "description"),
        "model": _fm_text(fm, "model"),
        "extends": _fm_text(fm, "extends"),
        "icon": _fm_text(fm, "icon"),
        "role": role,
        "disable_model_invocation": disable_model_invocation,
        "user_invocable": user_invocable,
        "argument_hint": _fm_text(fm, "argument-hint"),
        "error": "",
        "dir": os.path.abspath(skill_dir),
        "manifest": os.path.abspath(manifest),
        "body": body,
    }


def _unusable_skill(skill_dir: str, manifest: str, error: str, name: str = "") -> dict:
    """The metadata of a skill whose SKILL.md cannot be used, with the reason."""
    return {
        "name": name or os.path.basename(os.path.normpath(skill_dir)),
        "description": "",
        "model": "",
        "extends": "",
        "icon": "",
        "role": INSTRUCTIONS_ROLE,
        "disable_model_invocation": False,
        "user_invocable": False,
        "argument_hint": "",
        "error": f"{SKILL_MANIFEST}: {error}",
        "dir": os.path.abspath(skill_dir),
        "manifest": os.path.abspath(manifest),
        "body": "",
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


def skill_error(skill_meta: dict) -> str:
    """Why the skill cannot be used, or "" when it can."""
    return str(skill_meta.get("error") or "")


def is_model_invocable(skill_meta: dict) -> bool:
    """Whether the agent may reach the skill on its own (catalog, load_skill, run_skill)."""
    return not skill_error(skill_meta) and not skill_meta.get("disable_model_invocation", False)


def is_user_invocable(skill_meta: dict) -> bool:
    """Whether the user may run the skill by typing `/name` (Claude Code's rule)."""
    return not skill_error(skill_meta) and bool(skill_meta.get("user_invocable", True))


def local_skill_dir(skill_name: str, project_path: str) -> str | None:
    """Return `<project>/.gilzabots/skills/<skill_name>` when that copy exists.

    This is the copy the Asset Store installs, and the one that shadows every
    other search dir (see `skill_search_dirs`).
    """
    if not project_path or not skill_name:
        return None
    candidate = os.path.abspath(os.path.join(project_path, ".gilzabots", "skills", skill_name))
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
