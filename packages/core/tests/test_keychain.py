"""FileKeychain: 0600 at rest, roundtrips, fails on hostile names."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from kantaq_core.identity import FileKeychain


def test_set_get_roundtrip(tmp_path: Path) -> None:
    keychain = FileKeychain(tmp_path / "keys")
    keychain.set("runtime-token", "kq_abc.def")
    assert keychain.get("runtime-token") == "kq_abc.def"


def test_missing_entry_is_none(tmp_path: Path) -> None:
    assert FileKeychain(tmp_path).get("nothing-here") is None


def test_secret_file_is_owner_only(tmp_path: Path) -> None:
    keychain = FileKeychain(tmp_path)
    keychain.set("runtime-token", "secret")
    mode = stat.S_IMODE((tmp_path / "runtime-token").stat().st_mode)
    assert mode == 0o600


def test_overwrite_repairs_loose_mode(tmp_path: Path) -> None:
    path = tmp_path / "runtime-token"
    path.write_text("old\n", encoding="utf-8")
    path.chmod(0o644)
    FileKeychain(tmp_path).set("runtime-token", "new")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert FileKeychain(tmp_path).get("runtime-token") == "new"


def test_delete_is_idempotent(tmp_path: Path) -> None:
    keychain = FileKeychain(tmp_path)
    keychain.set("runtime-token", "secret")
    keychain.delete("runtime-token")
    keychain.delete("runtime-token")
    assert keychain.get("runtime-token") is None


@pytest.mark.parametrize("name", ["", "../escape", "a/b", ".hidden"])
def test_hostile_entry_names_are_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError):
        FileKeychain(tmp_path).set(name, "x")
