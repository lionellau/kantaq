"""Docs profile (MOD-16): the QUICKSTART's commands actually exist.

The fresh-clone CI job *executes* the core path (setup → migrate → test); this
gate covers the rest hermetically: every ``make`` target and ``kantaq``
subcommand the QUICKSTART (and README) name must exist in the Makefile / CLI
parser, so the docs cannot drift from the tooling without failing the build.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kantaq.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_WITH_COMMANDS = [
    REPO_ROOT / "QUICKSTART.md",
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "setup-supabase.md",
]

_FENCE = re.compile(r"```(?:bash|sh|console)?\n(.*?)```", re.DOTALL)


def _fenced_commands(path: Path) -> list[str]:
    commands: list[str] = []
    for block in _FENCE.findall(path.read_text(encoding="utf-8")):
        for raw in block.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            commands.append(line)
    return commands


def _make_targets() -> set[str]:
    text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    return set(re.findall(r"^([a-zA-Z_-]+):", text, re.MULTILINE))


def _kantaq_subcommands() -> dict[str, set[str]]:
    """Top-level subcommands and their nested choices from the live parser."""
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if action.dest == "command"  # noqa: SLF001
    )
    result: dict[str, set[str]] = {}
    for name, sub in subparsers.choices.items():  # type: ignore[union-attr]
        nested: set[str] = set()
        for action in sub._actions:  # noqa: SLF001
            if action.choices and action.dest.endswith("_command"):
                nested = set(action.choices)
        result[name] = nested
    return result


def test_quickstart_exists_and_readme_links_it() -> None:
    assert (REPO_ROOT / "QUICKSTART.md").is_file()
    assert "QUICKSTART.md" in (REPO_ROOT / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("doc", DOCS_WITH_COMMANDS, ids=lambda p: p.name)
def test_documented_commands_exist(doc: Path) -> None:
    makes = _make_targets()
    kantaq_cmds = _kantaq_subcommands()
    problems: list[str] = []

    for line in _fenced_commands(doc):
        if line.startswith("make "):
            target = line.split()[1]
            if target not in makes:
                problems.append(f"unknown make target: {line!r}")
        # `kantaq ...` lines, including inside $(...) substitutions.
        for match in re.finditer(r"\bkantaq\s+([a-z-]+)(?:\s+([a-z-]+))?", line):
            top, nested = match.group(1), match.group(2)
            if top not in kantaq_cmds:
                problems.append(f"unknown kantaq subcommand: {line!r}")
            elif nested and kantaq_cmds[top] and nested not in kantaq_cmds[top]:
                problems.append(f"unknown kantaq {top} subcommand: {line!r}")

    assert not problems, f"{doc.name}: {problems}"


def test_env_examples_referenced_by_quickstart_are_tracked() -> None:
    quickstart = (REPO_ROOT / "QUICKSTART.md").read_text(encoding="utf-8")
    for example in re.findall(r"\.env[\w.-]*\.example", quickstart):
        assert (REPO_ROOT / example).is_file(), f"QUICKSTART references missing {example}"
