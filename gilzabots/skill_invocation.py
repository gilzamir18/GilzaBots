"""Skills the user runs by typing `/name [arguments]` (PROJECT_DESIGN 2.29).

Claude Code's user-invoked skills: the composer, the terminal and a one-shot
`gilzabots -p "/name ..."` all hand the typed line to the agent turn engine,
which replaces it -- for the model only -- with the skill's instructions and the
arguments. The chat stores and shows what the user typed.

A line names a skill when its first token is `/` plus the name of an active
skill that the user may invoke (`user-invocable`, not a helper). Built-in
commands keep their names: a skill called `help` is not reachable as `/help`.
A `/word` that names no installed skill is not an invocation at all, so a
message such as "/etc/hosts is wrong" still reaches the model as text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .skills import (
    HELPER_ROLE,
    MANDATORY_SKILLS,
    active_skills,
    discover_skills,
    is_user_invocable,
    skill_error,
    skill_role,
)

#: `/name`, then optional arguments separated by whitespace (newlines included).
_INVOCATION = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\s+(.*?))?\s*$", re.DOTALL)

#: Placeholder Claude Code replaces with the arguments inside a skill's body.
ARGUMENTS_PLACEHOLDER = "$ARGUMENTS"


class SkillInvocationError(ValueError):
    """`/name` names an installed skill that cannot run now; the message says why."""


@dataclass(frozen=True)
class SkillInvocation:
    name: str
    arguments: str
    prompt: str


def parse_invocation(text: str) -> tuple[str, str] | None:
    """Split `/name args` into (name, args), or None when *text* is not of that shape."""
    match = _INVOCATION.match(str(text or "").strip())
    if not match:
        return None
    return match.group(1), (match.group(2) or "").strip()


def builtin_command_names() -> set[str]:
    """Every name and alias of the built-in `/` commands, without the slash."""
    from .cli_commands import _registry

    return {name.lstrip("/") for name in _registry.names()}


def invocable_skills(project_path: str) -> list[dict]:
    """The active skills `/name` reaches, in discovery order."""
    builtins = builtin_command_names()
    return [
        skill for skill in active_skills(project_path)
        if skill["name"] not in MANDATORY_SKILLS
        and skill["name"] not in builtins
        and is_user_invocable(skill)
        and skill_role(skill) != HELPER_ROLE
    ]


def resolve_skill_invocation(project_path: str, text: str, profile: str = "full") -> SkillInvocation | None:
    """The skill run that *text* asks for, or None when it asks for none.

    Raises SkillInvocationError when *text* names an installed skill that cannot
    run: not active in this project, broken, a helper, or `user-invocable: false`.
    """
    parsed = parse_invocation(text)
    if parsed is None:
        return None
    name, arguments = parsed
    if name in builtin_command_names() or name in MANDATORY_SKILLS:
        return None

    active = {skill["name"]: skill for skill in active_skills(project_path)}
    meta = active.get(name)
    if meta is None:
        if any(skill["name"] == name for skill in discover_skills(project_path)):
            raise SkillInvocationError(
                f"The skill '{name}' is installed but not active in this project. "
                f"Activate it with /addskill {name}, or from the Asset Store."
            )
        return None
    if skill_error(meta):
        raise SkillInvocationError(f"The skill '{name}' cannot be used: {skill_error(meta)}")
    if skill_role(meta) == HELPER_ROLE:
        raise SkillInvocationError(
            f"'{name}' is a helper skill: the agent delegates jobs to it, and it is not run with /{name}."
        )
    if not is_user_invocable(meta):
        raise SkillInvocationError(
            f"The skill '{name}' sets `user-invocable: false`: only the agent loads it."
        )

    from .memgpt_runtime import render_skill_instructions

    rendered, error = render_skill_instructions(
        name, project_path, profile, [s for s in active.values()]
    )
    if error:
        raise SkillInvocationError(error)
    return SkillInvocation(name=name, arguments=arguments, prompt=_invocation_prompt(name, arguments, rendered))


def _invocation_prompt(name: str, arguments: str, rendered: str) -> str:
    """The turn the model receives for `/name arguments`.

    The arguments go where the body puts `$ARGUMENTS`; a body without the
    placeholder receives them after it, labelled, which is Claude Code's rule.
    """
    if ARGUMENTS_PLACEHOLDER in rendered:
        rendered = rendered.replace(ARGUMENTS_PLACEHOLDER, arguments)
        trailer = ""
    elif arguments:
        trailer = f"\n\nARGUMENTS: {arguments}"
    else:
        trailer = ""
    typed = f"/{name} {arguments}".rstrip()
    return (
        f"{typed}\n\n"
        f"[skill '/{name}' run by the user] The user ran this skill. Carry out its instructions "
        "yourself, with your own tools; where they speak of arguments, those are what the user "
        f"typed after /{name} (none if nothing follows).\n"
        f"{rendered}{trailer}"
    )
