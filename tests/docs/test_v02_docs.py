"""Docs profile (MOD-16, E29-T4): the v0.2 doc set ships, links, and stays honest.

Pins the v0.2 additions — the sync/conflict/retention doc and the cost-model
post — so a future edit cannot drop one, sever its links, or let the cost claim
drift from the MOD-27 numbers. The internal-link gate (``test_doc_links``)
already proves links resolve; this pins the *shape* and the load-bearing facts.
Hermetic — files only, no network.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
SYNC = DOCS / "sync.md"
COST = DOCS / "blog" / "what-a-4-person-team-actually-pays.md"


def test_v02_docs_exist() -> None:
    assert SYNC.is_file(), "docs/sync.md (offline/conflict/retention) is missing"
    assert COST.is_file(), "the v0.2 cost-model post is missing"


def test_readme_links_the_v02_docs() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/sync.md" in readme
    assert "docs/blog/what-a-4-person-team-actually-pays.md" in readme


def test_sync_doc_cross_links_protocol() -> None:
    """The doc set is a web, not islands (the v0.1 rule carries to v0.2)."""
    assert "protocol.md" in SYNC.read_text(encoding="utf-8")


def test_cost_post_states_the_real_tiers() -> None:
    """The cost claim must match MOD-27: Free $0 / 500 MB, Pro $25, VPS $5–10."""
    text = COST.read_text(encoding="utf-8")
    for fact in ("500 MB", "$25", "$5–10", "$0", "290 MB"):
        assert fact in text, f"cost post must cite {fact!r} (MOD-27)"
    # The honest ceiling: <$10 is the VPS path, not Supabase cloud.
    assert "Pro floor is $25" in text or "Pro's floor is $25" in text or "floor is $25" in text


def test_cost_post_does_not_promise_a_dollar_dashboard() -> None:
    """D-16: the dashboard shows capacity, never a projected dollar bill."""
    text = COST.read_text(encoding="utf-8").lower()
    assert "capacity gauge, not a dollar bill" in text


def test_sync_doc_covers_offline_conflicts_and_retention() -> None:
    text = SYNC.read_text(encoding="utf-8").lower()
    for topic in ("offline", "conflict", "retention", "compare-and-swap", "watermark"):
        assert topic in text, f"sync.md must cover {topic!r}"
