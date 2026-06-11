"""Docs profile (MOD-16): every internal doc link resolves.

Hermetic by design — only repo-relative links are checked, so the test never
touches the network (harness standard §2.3). External URLs are out of scope;
they would make the gate flaky and are better spot-checked at release time.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Markdown inline links/images: [text](target) — captures the target.
_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

# Directories that hold third-party or generated content.
_SKIP_DIRS = {".git", ".venv", "node_modules", "dist", "build", "__pycache__", ".pytest_cache"}


def _markdown_files() -> list[Path]:
    files = [
        path
        for path in REPO_ROOT.rglob("*.md")
        if not (_SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts))
    ]
    assert files, "no markdown files found — wrong repo root?"
    return files


def _internal_targets(markdown: Path) -> list[str]:
    text = markdown.read_text(encoding="utf-8")
    targets = []
    for raw in _LINK.findall(text):
        if raw.startswith(("http://", "https://", "mailto:", "#", "<")):
            continue
        targets.append(raw.split("#", 1)[0])  # strip in-page anchors
    return [t for t in targets if t]


@pytest.mark.parametrize("markdown", _markdown_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_internal_links_resolve(markdown: Path) -> None:
    missing = [
        target
        for target in _internal_targets(markdown)
        if not (markdown.parent / target).resolve().exists()
    ]
    assert not missing, f"{markdown.relative_to(REPO_ROOT)} has dead links: {missing}"
