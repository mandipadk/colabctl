"""Install the colabctl Agent Skill into a Claude Code skills directory.

The skill files ship inside the wheel (under ``colabctl/_skills/``). Claude Code does NOT scan
Python site-packages for skills, so this copies them into a watched location —
``~/.claude/skills/colabctl/`` (user scope, all projects) or ``./.claude/skills/colabctl/``
(project scope, committable). The install is version-stamped so ``colabctl skill install
--force`` refreshes it in place after an upgrade and the agent-facing docs never drift.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from colabctl import __version__

_STAMP = ".colabctl-skill-version"
_SKILL_NAME = "colabctl"


def bundled_skill_dir() -> Path:
    """The skill directory shipped inside the package (sibling of this module)."""
    return Path(__file__).resolve().parent / "_skills" / _SKILL_NAME


def target_dir(scope: str) -> Path:
    """Where the skill installs for ``scope`` ('user' → ~/.claude, 'project' → ./.claude)."""
    if scope == "user":
        return Path.home() / ".claude" / "skills" / _SKILL_NAME
    if scope == "project":
        return Path.cwd() / ".claude" / "skills" / _SKILL_NAME
    raise ValueError(f"unknown scope {scope!r} (use 'user' or 'project')")


def installed_version(scope: str = "user") -> str | None:
    """The version stamp of the installed skill for ``scope`` (None if not installed)."""
    try:
        return (target_dir(scope) / _STAMP).read_text(encoding="utf-8").strip()
    except OSError:
        return None


@dataclass
class InstallResult:
    path: Path
    action: str  # "installed" | "updated" | "current"


def install(scope: str = "user", *, force: bool = False) -> InstallResult:
    """Copy the bundled skill into ``scope``'s skills dir, version-stamped. Idempotent."""
    dest = target_dir(scope)
    current = installed_version(scope)
    if current == __version__ and not force:
        return InstallResult(path=dest, action="current")
    src = bundled_skill_dir()
    if not (src / "SKILL.md").is_file():
        raise FileNotFoundError(f"bundled skill missing at {src} (packaging error)")
    shutil.copytree(src, dest, dirs_exist_ok=True)
    (dest / _STAMP).write_text(__version__ + "\n", encoding="utf-8")
    return InstallResult(path=dest, action="updated" if current is not None else "installed")


def uninstall(scope: str = "user") -> bool:
    """Remove the installed colabctl skill dir for ``scope``. Returns whether it existed."""
    dest = target_dir(scope)
    if (dest / "SKILL.md").exists():
        shutil.rmtree(dest)
        return True
    return False


def needs_hint() -> bool:
    """True when the user-scope skill is missing or older than the installed colabctl."""
    return installed_version("user") != __version__
