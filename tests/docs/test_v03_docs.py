"""Docs profile (MOD-16, E29-T5): the v0.3 self-host doc set ships, links, and
stays honest.

Pins the v0.3 addition — the ``setup-self-hosted.md`` front-door guide — so a
future edit cannot drop it, sever its cross-links, drift the stdio env-var
contract away from the code, fabricate a notification command that does not
exist yet (E20 is unshipped), or quietly re-promise Tier-3 as v0.3 (it is
Sprint 10+). The internal-link gate (``test_doc_links``) already proves links
resolve and the command-drift gate (``test_quickstart_commands``) proves every
fenced command exists; this pins the *shape* and the load-bearing facts.
Hermetic — files + the live constants, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
SELF_HOSTED = DOCS / "setup-self-hosted.md"
COMPAT = DOCS / "clients" / "compatibility.md"


def test_self_hosted_guide_exists_and_readme_links_it() -> None:
    assert SELF_HOSTED.is_file(), "docs/setup-self-hosted.md is missing"
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/setup-self-hosted.md" in readme, "README must link the self-host guide"


def test_self_hosted_guide_walks_the_v03_steps() -> None:
    """The walkthrough must reach a running team backend with an agent connected.

    Clone/compose → point the runtime (``HUB_MODE=postgres``) → connect a stdio
    agent → invite teammates. Pin the load-bearing pieces so a future edit cannot
    quietly shrink the guide back to "just ``docker compose up``".
    """
    text = SELF_HOSTED.read_text(encoding="utf-8")
    for piece in (
        "docker compose up",
        "HUB_MODE=postgres",
        "kantaq mcp stdio",
        "kantaq sync once",
        "Members",  # the invite-teammates step
    ):
        assert piece in text, f"setup-self-hosted.md must document {piece!r}"


def test_self_hosted_stdio_contract_matches_code() -> None:
    """The documented stdio env-var contract must be the one the code reads.

    If ``kantaq_mcp.stdio`` renames the env vars, this fails — the guide cannot
    drift from the transport it documents. Imported in-function (the MOD-30 rule:
    keep the MCP SDK off the pytest plugin/collection path).
    """
    from kantaq_mcp.stdio import GRANT_ENV, TOKEN_ENV

    text = SELF_HOSTED.read_text(encoding="utf-8")
    assert TOKEN_ENV in text, f"the self-host guide must name {TOKEN_ENV} (the stdio bearer)"
    assert GRANT_ENV in text, f"the self-host guide must name {GRANT_ENV} (the grant binding)"


def test_self_hosted_guide_is_honest_about_notifications() -> None:
    """E20 (notification dispatch) is unshipped, so the guide must not promise a
    working webhook command — it forward-references the opt-in, content-free v0.3
    feature in prose instead. Guards against a future edit fabricating config.
    """
    text = SELF_HOSTED.read_text(encoding="utf-8").lower()
    assert "notifications" in text
    assert "opt-in" in text and "content-free" in text
    # There is no `kantaq notify` / `kantaq notification` subcommand; the guide
    # must not invent one (the command-drift gate would also catch it).
    assert "kantaq notify" not in text and "kantaq notification" not in text


def test_self_hosted_guide_cross_links_the_web() -> None:
    """The doc set is a web, not islands (the v0.1 rule carries to v0.3)."""
    text = SELF_HOSTED.read_text(encoding="utf-8")
    for target in (
        "docker/self-hosted-backend/README.md",
        "mcp.md",
        "compatibility.md",
        "QUICKSTART.md",
    ):
        assert target in text, f"the self-host guide must cross-link {target}"


def test_self_hosted_env_example_is_tracked() -> None:
    """Any ``.env*.example`` the guide names must exist (mirrors the QUICKSTART gate).

    The self-host example lives beside the compose file, not at the repo root.
    """
    text = SELF_HOSTED.read_text(encoding="utf-8")
    for example in re.findall(r"\.env[\w.-]*\.example", text):
        assert (REPO_ROOT / "docker" / "self-hosted-backend" / example).is_file(), (
            f"setup-self-hosted.md references missing {example}"
        )


def test_compatibility_defers_tier3_to_sprint_10() -> None:
    """Drift fix (E29-T5): the Tiers table listed Tier-3 as v0.3; sprint-9 defers
    it to Sprint 10+. Pin it so the matrix cannot silently re-promise it.
    """
    text = COMPAT.read_text(encoding="utf-8")
    assert "3 (H1–H3) — Sprint 10+" in text, "Tier-3 must be deferred to Sprint 10+"
    assert "3 (H1–H3) — v0.3" not in text, "the stale Tier-3 = v0.3 claim must be gone"


def test_compatibility_records_the_v03_refresh_and_stdio_path() -> None:
    text = COMPAT.read_text(encoding="utf-8")
    assert "v0.3 refresh" in text, "the matrix must carry a v0.3 refresh note"
    # The stdio connect path is surfaced and points at the self-host guide.
    assert "stdio" in text and "setup-self-hosted.md" in text
