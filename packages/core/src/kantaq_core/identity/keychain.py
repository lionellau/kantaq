"""Where the runtime keeps its own bearer token (D-06, MOD-08 "keychain token").

The database stores only hashes, but the local runtime needs its own plaintext
token so the user (and their agent) can authenticate against it. v0.0.5 uses a
0600 file under the data directory; the OS-keychain backend arrives with the
v0.1 device keys (D-01), when the Golden-rule pass for a keychain library is
re-run (no Python keychain library currently clears the reuse bar — see
docs/stack.md). The ``Keychain`` protocol keeps callers indifferent, and the
test harness substitutes ``FakeKeychain``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Keychain(Protocol):
    """A place to keep one named secret."""

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...


class FileKeychain:
    """Secrets as 0600 files under a directory (the v0.0.5 backend)."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _path(self, name: str) -> Path:
        if not name or "/" in name or name.startswith("."):
            raise ValueError(f"invalid keychain entry name: {name!r}")
        return self._dir / name

    def get(self, name: str) -> str | None:
        path = self._path(name)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8").strip()

    def set(self, name: str, value: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(name)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)  # repair a pre-existing looser mode before writing
        path.write_text(value + "\n", encoding="utf-8")

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)
