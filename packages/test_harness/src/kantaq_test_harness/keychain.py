"""FakeKeychain — in-memory stand-in for the OS keychain (Identity profile).

Honors the same contract as ``kantaq_core.identity.Keychain`` (get/set/delete,
hostile names rejected) without touching the filesystem. Lands with E06 per
the harness standard §8; the gateway (MOD-08) and agent snippet reuse it.
The harness stays a leaf dependency, so the contract is duplicated here rather
than imported from core; ``packages/core/tests`` runs the shared contract test
against both implementations.
"""

from __future__ import annotations


class FakeKeychain:
    """An in-memory keychain. ``calls`` records mutations for assertions."""

    def __init__(self) -> None:
        self._entries: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _validate(name: str) -> str:
        if not name or "/" in name or name.startswith("."):
            raise ValueError(f"invalid keychain entry name: {name!r}")
        return name

    def get(self, name: str) -> str | None:
        return self._entries.get(self._validate(name))

    def set(self, name: str, value: str) -> None:
        self._entries[self._validate(name)] = value
        self.calls.append(("set", name))

    def delete(self, name: str) -> None:
        self._entries.pop(self._validate(name), None)
        self.calls.append(("delete", name))
