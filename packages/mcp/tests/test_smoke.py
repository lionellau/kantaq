"""Trivial import smoke test (MOD-01 test plan: one passing test per package)."""

import kantaq_mcp


def test_package_imports() -> None:
    assert kantaq_mcp.__version__ == "0.2.0"
