"""FakeKeychain honors the Keychain contract (the harness tests itself)."""

from __future__ import annotations

import pytest

from kantaq_test_harness.keychain import FakeKeychain


def test_set_get_roundtrip() -> None:
    keychain = FakeKeychain()
    keychain.set("runtime-token", "kq_abc.def")
    assert keychain.get("runtime-token") == "kq_abc.def"


def test_missing_entry_is_none() -> None:
    assert FakeKeychain().get("nothing") is None


def test_delete_is_idempotent() -> None:
    keychain = FakeKeychain()
    keychain.set("runtime-token", "x")
    keychain.delete("runtime-token")
    keychain.delete("runtime-token")
    assert keychain.get("runtime-token") is None


def test_mutations_are_recorded() -> None:
    keychain = FakeKeychain()
    keychain.set("a", "1")
    keychain.delete("a")
    assert keychain.calls == [("set", "a"), ("delete", "a")]


@pytest.mark.parametrize("name", ["", "../escape", "a/b", ".hidden"])
def test_hostile_entry_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError):
        FakeKeychain().set(name, "x")
