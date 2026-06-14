#!/usr/bin/env python
"""E11-T2 — the Tier-1 compatibility runner (MOD-24, PRD §20.4).

Runs the eight Tier-1 acceptance tests (T1–T8) with the **scripted client** —
``FakeAgent``, the official MCP SDK client (the library Claude Code and Cursor
embed) over the real gateway — and prints a matrix-ready result line for
``docs/clients/compatibility.md``. This is the same suite CI runs
(``tests/compat``); the script exists so the **scripted** ``Pass rate`` in the
matrix is reproduced by one command, and exits non-zero on any failure.

    uv run python scripts/compat_check.py
    # → Tier-1 (scripted, MCP SDK client): 8 / 8 PASS  ·  2026-06-14

The **real-client** runs (real Claude Code and Cursor against pinned versions)
are the manual release step — a GUI client cannot be scripted (a human pastes
the snippet from Settings → My Agent and restarts the client). The procedure,
and where to record the pinned version + date + pass rate, is in
``docs/clients/compatibility.md`` §"Running the tests". Use this runner to prove
the server side first; then drive each real client through the same eight
criteria and record the result.
"""

from __future__ import annotations

import argparse
import datetime as _dt
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPAT_SUITE = ROOT / "tests" / "compat"

# node-id substring → the Tier-1 id (PRD §20.4 C1–C8). One test per criterion.
_TIER1 = {
    "test_t1_": "T1 first connection (≤ 5 s)",
    "test_t2_": "T2 role-aware read",
    "test_t3_": "T3 propose + human approval",
    "test_t4_": "T4 permission denial",
    "test_t5_": "T5 token rotation",
    "test_t6_": "T6 untrusted-content tagging",
    "test_t7_": "T7 session expiry",
    "test_t8_": "T8 audit completeness",
}


class _Collector:
    """A pytest plugin that records each Tier-1 test's call-phase outcome.

    A plain class on purpose: a dataclass instance is unhashable under pytest's
    fixture-holder bookkeeping (``eq=True`` drops ``__hash__``).
    """

    def __init__(self) -> None:
        self.outcomes: dict[str, str] = {}

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when != "call":
            return
        for marker, label in _TIER1.items():
            if marker in report.nodeid:
                self.outcomes[label] = report.outcome  # "passed" | "failed" | "skipped"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="compat_check", description=__doc__)
    parser.add_argument(
        "--date",
        default=_dt.date.today().isoformat(),
        help="last-verified date to stamp on the matrix line (default: today)",
    )
    args = parser.parse_args(argv)

    collector = _Collector()
    code = pytest.main(["-q", "-p", "no:cacheprovider", str(COMPAT_SUITE)], plugins=[collector])

    print("\nTier-1 acceptance (scripted client — official MCP SDK over the real gateway):")
    passed = 0
    for label in _TIER1.values():
        outcome = collector.outcomes.get(label, "missing")
        mark = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}.get(outcome, "MISSING")
        passed += outcome == "passed"
        print(f"  [{mark:>4}] {label}")

    total = len(_TIER1)
    verdict = "PASS" if passed == total else "FAIL"
    print(f"\nTier-1 (scripted, MCP SDK client): {passed} / {total} {verdict}  ·  {args.date}")
    if passed < total:
        print(
            "\nThe README advertises a tier only when fully passing (FR-E11-4). "
            "Fix findings before updating the matrix or the badge."
        )
    print(
        "Real Claude Code / Cursor runs are the manual release step — see "
        "docs/clients/compatibility.md."
    )
    return 0 if code == 0 and passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
