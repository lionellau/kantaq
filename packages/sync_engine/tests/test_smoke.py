"""Trivial import smoke test (MOD-01 test plan: one passing test per package)."""

import kantaq_sync_engine


def test_package_imports() -> None:
    assert kantaq_sync_engine.__version__ == "0.1.0"
