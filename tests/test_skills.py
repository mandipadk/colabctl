"""The colabctl Agent Skill: bundled files resolve, install/status/uninstall, version stamp."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from colabctl import __version__, skills
from colabctl import cli as cli_mod

runner = CliRunner()


def test_bundled_skill_is_present_and_well_formed():
    src = skills.bundled_skill_dir()
    skill_md = (src / "SKILL.md").read_text(encoding="utf-8")
    assert skill_md.startswith("---")  # YAML frontmatter
    assert "name: colabctl" in skill_md  # name must equal the dir name
    assert "description:" in skill_md
    # reference + example files the body points at
    assert (src / "references" / "commands.md").is_file()
    assert (src / "references" / "mcp-tools.md").is_file()
    assert (src / "examples" / "recipes.md").is_file()


def test_skill_name_obeys_spec_rules():
    # name: lowercase letters/digits/hyphens, <=64 chars, no reserved 'claude'/'anthropic'
    line = next(
        ln
        for ln in (skills.bundled_skill_dir() / "SKILL.md").read_text().splitlines()
        if ln.startswith("name:")
    )
    name = line.split(":", 1)[1].strip()
    assert name == "colabctl"
    assert name.replace("-", "").isalnum() and name.islower() and len(name) <= 64
    assert "claude" not in name and "anthropic" not in name


def test_install_copies_files_and_stamps_version(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    res = skills.install("user")
    assert res.action == "installed"
    dest = tmp_path / ".claude" / "skills" / "colabctl"
    assert (dest / "SKILL.md").is_file()
    assert (dest / "references" / "commands.md").is_file()
    assert skills.installed_version("user") == __version__
    # idempotent: a second install is a no-op unless forced
    assert skills.install("user").action == "current"
    assert skills.install("user", force=True).action == "updated"


def test_needs_hint_tracks_install_state(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert skills.needs_hint() is True  # not installed yet
    skills.install("user")
    assert skills.needs_hint() is False  # installed at the current version


def test_uninstall_removes_only_the_skill_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    skills.install("user")
    assert skills.uninstall("user") is True
    assert not (tmp_path / ".claude" / "skills" / "colabctl").exists()
    assert skills.uninstall("user") is False  # already gone


def test_cli_skill_install_and_status(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("COLABCTL_NO_SKILL_HINT", "1")  # don't let the hint interfere
    out = runner.invoke(cli_mod.app, ["skill", "install"])
    assert out.exit_code == 0 and "colabctl skill at" in out.output
    status = runner.invoke(cli_mod.app, ["skill", "status"])
    assert status.exit_code == 0 and __version__ in status.output


def test_first_run_hint_shows_once_then_silences(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    colabctl_home = tmp_path / "cc-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("COLABCTL_HOME", str(colabctl_home))
    monkeypatch.delenv("COLABCTL_NO_SKILL_HINT", raising=False)
    # skill not installed → first invocation hints (to stderr), second is silent (marker written)
    first = runner.invoke(cli_mod.app, ["version"])
    second = runner.invoke(cli_mod.app, ["version"])
    assert "colabctl skill install" in first.output
    assert "colabctl skill install" not in second.output
