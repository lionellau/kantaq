"""The prompt-injection regression corpus (MOD-18 / MOD-30, with E09/E10).

Checked-in fixtures under ``packages/test_harness/fixtures/injection``: hostile
strings planted in human-authored tracker content by the Gateway/Agent profile
tests. The contract every read tool must hold: the payload comes back inside
exactly one untrusted fence, neutralized, and is never executed. The corpus
grows with MOD-18; tests iterate it so a new fixture is a new regression test
for free.

Stdlib-only on purpose — safe to import anywhere, including the plugin path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CORPUS_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "injection" / "corpus.json"


@dataclass(frozen=True)
class InjectionFixture:
    """One hostile payload and where the threat model says it comes from."""

    id: str
    source: str
    payload: str


def load_injection_corpus(path: Path | None = None) -> tuple[InjectionFixture, ...]:
    """Load the corpus; tests parametrize over the returned fixtures."""
    raw = json.loads((path or CORPUS_PATH).read_text(encoding="utf-8"))
    fixtures = tuple(
        InjectionFixture(id=entry["id"], source=entry["source"], payload=entry["payload"])
        for entry in raw["fixtures"]
    )
    if not fixtures:
        raise ValueError(f"injection corpus at {path or CORPUS_PATH} is empty")
    return fixtures
