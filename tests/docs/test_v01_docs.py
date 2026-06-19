"""Docs profile (MOD-16, E29-T2): the v0.1 published doc set exists and coheres.

The internal-link gate (``test_doc_links``) already proves every link resolves;
this gate pins the *shape* of the v0.1 launch doc set so a future edit cannot
quietly drop a published doc, sever the cross-link web, or weaken the
standards-honesty claim. Hermetic — reads files only, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"

# The docs FR-E29-2 names as published at v0.1.
PUBLISHED_DOCS = [
    DOCS / "protocol.md",
    DOCS / "security.md",
    DOCS / "mcp.md",
    DOCS / "clients" / "compatibility.md",
    DOCS / "portability.md",
]


@pytest.mark.parametrize("doc", PUBLISHED_DOCS, ids=lambda p: p.name)
def test_published_doc_exists(doc: Path) -> None:
    assert doc.is_file(), f"v0.1 published doc missing: {doc.relative_to(REPO_ROOT)}"


def test_readme_links_every_published_doc() -> None:
    """The README's Documentation table is the front door — it must link them all."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for doc in PUBLISHED_DOCS:
        rel = doc.relative_to(REPO_ROOT).as_posix()
        assert rel in readme, f"README does not link {rel}"


def test_protocol_doc_names_the_standards_it_implements() -> None:
    """Standards-honesty (MOD-17): protocol.md cites only standards we implement."""
    protocol = (DOCS / "protocol.md").read_text(encoding="utf-8")
    for standard in ("RFC 8785", "RFC 8032"):
        assert standard in protocol, f"protocol.md must name {standard}"
    # We sign grants with the same Ed25519 codec — we must NOT claim JWT/UCAN/Biscuit
    # semantics. The doc states this disclaimer explicitly; pin it so a future edit
    # cannot silently start claiming a token standard we do not implement.
    assert "not** JWT" in protocol or "not JWT" in protocol, (
        "protocol.md must keep the 'grants are not JWT/UCAN/Biscuit' honesty note"
    )


def test_doc_set_is_cross_linked() -> None:
    """The v0.1 doc set is a web, not islands: each core doc links protocol.md."""
    for doc in (DOCS / "security.md", DOCS / "mcp.md", DOCS / "portability.md"):
        text = doc.read_text(encoding="utf-8")
        assert "protocol.md" in text, f"{doc.name} should cross-link protocol.md"
