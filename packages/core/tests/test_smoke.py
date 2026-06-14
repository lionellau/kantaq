"""Trivial import smoke test (MOD-01 test plan: one passing test per package)."""

import kantaq_core


def test_package_imports() -> None:
    assert kantaq_core.__version__ == "0.1.0"
