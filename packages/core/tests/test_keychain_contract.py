"""Shared contract test: FakeKeychain behaves like the real FileKeychain.

Harness standard §3: a fake must honor the same contract as the real thing,
verified by a shared test. This is that test — both implementations run the
same scenarios, so the fake can never drift from production behavior.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from kantaq_core.identity import FileKeychain, Keychain
from kantaq_test_harness import FakeKeychain


@pytest.fixture(params=["file", "fake"])
def make_keychain(request: pytest.FixtureRequest, tmp_path: Path) -> Callable[[], Keychain]:
    if request.param == "file":
        return lambda: FileKeychain(tmp_path / "keys")
    return lambda: FakeKeychain()


def test_contract_roundtrip_and_delete(make_keychain: Callable[[], Keychain]) -> None:
    keychain = make_keychain()
    assert keychain.get("runtime-token") is None
    keychain.set("runtime-token", "kq_abc.def")
    assert keychain.get("runtime-token") == "kq_abc.def"
    keychain.set("runtime-token", "kq_new.value")
    assert keychain.get("runtime-token") == "kq_new.value"  # overwrite wins
    keychain.delete("runtime-token")
    keychain.delete("runtime-token")  # idempotent
    assert keychain.get("runtime-token") is None


@pytest.mark.parametrize("name", ["", "../escape", "a/b", ".hidden"])
def test_contract_rejects_hostile_names(make_keychain: Callable[[], Keychain], name: str) -> None:
    with pytest.raises(ValueError):
        make_keychain().set(name, "x")
