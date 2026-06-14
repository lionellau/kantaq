"""Opt-in real-agent compatibility check (E11-T2 Tier-1) — skipped in normal CI.

Drives a real, LLM-backed coding agent (Claude Code / Codex) against kantaq's
MCP gateway via scripts/verify_agent.py and asserts it connected, read a ticket,
and created a proposal. A real agent needs auth + network and is non-
deterministic, so this can never be a blocking CI gate; it runs only when
KANTAQ_VERIFY_AGENT=1 is set and at least one agent CLI is installed (mirroring
the EphemeralPostgres opt-in idiom). `make verify-agent` is the usual entry.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    not os.environ.get("KANTAQ_VERIFY_AGENT")
    or not (shutil.which("claude") or shutil.which("codex")),
    reason="opt-in: set KANTAQ_VERIFY_AGENT=1 and install `claude` and/or `codex`",
)
def test_a_real_agent_connects_reads_and_proposes() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_agent.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
