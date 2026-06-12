"""``kantaq sync`` CLI glue (E24-T4): guards, session slots, status.

The sync mechanics live (tested) in the adapter and the engine; these pin the
CLI's own behavior — config guards fail closed with actionable messages, the
login flow parks the session in the keychain (and never prints a token), the
member resolution refuses the unresolvable cases, and ``status`` stays
network-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine

from kantaq.cli import (
    SUPABASE_ACCESS_KEY,
    SUPABASE_EMAIL_KEY,
    SUPABASE_REFRESH_KEY,
    _sync_login,
    _sync_once,
    main,
)
from kantaq_backend_supabase import Session, SyncMember, User
from kantaq_test_harness import FakeKeychain

URL = "https://proj.supabase.co"


@dataclass
class StubAuth:
    """Records GoTrue calls; returns a canned session."""

    sent: list[str] = field(default_factory=list)
    verified: list[tuple[str, str]] = field(default_factory=list)
    session: Session = field(
        default_factory=lambda: Session(
            access_token="access-jwt",
            refresh_token="refresh-jwt",
            expires_in=3600,
            user=User(id="u1", email="dev@team.dev"),
        )
    )

    def request_magic_link(self, email: str, *, create_user: bool = False) -> None:
        self.sent.append(email)

    def verify(self, email: str, token: str) -> Session:
        self.verified.append((email, token))
        return self.session

    def refresh(self, refresh_token: str) -> Session:
        return self.session


def _member(member_id: str, workspace_id: str, email: str = "dev@team.dev") -> SyncMember:
    return SyncMember(
        id=member_id, workspace_id=workspace_id, email=email, role="Member", status="active"
    )


# ------------------------------------------------------------------ guards


def test_sync_refuses_outside_supabase_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # away from any developer .env
    monkeypatch.setenv("HUB_MODE", "local")
    assert main(["sync", "once"]) == 1
    assert "HUB_MODE=local" in capsys.readouterr().err


def test_sync_refuses_without_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # away from any developer .env
    monkeypatch.setenv("HUB_MODE", "supabase")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    assert main(["sync", "once"]) == 1
    assert "SUPABASE_URL" in capsys.readouterr().err


# ------------------------------------------------------------------- login


def test_login_parks_the_session_in_the_keychain(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    auth = StubAuth()
    keychain = FakeKeychain()
    monkeypatch.setattr("builtins.input", lambda prompt="": " 123456 ")

    assert _sync_login(auth, keychain, "dev@team.dev") == 0  # type: ignore[arg-type]

    assert auth.sent == ["dev@team.dev"]
    assert auth.verified == [("dev@team.dev", "123456")]
    assert keychain.get(SUPABASE_EMAIL_KEY) == "dev@team.dev"
    assert keychain.get(SUPABASE_ACCESS_KEY) == "access-jwt"
    assert keychain.get(SUPABASE_REFRESH_KEY) == "refresh-jwt"
    output = capsys.readouterr()
    assert "access-jwt" not in output.out + output.err  # tokens never print


# -------------------------------------------------------- member resolution


def _keychain_with_session() -> FakeKeychain:
    keychain = FakeKeychain()
    keychain.set(SUPABASE_EMAIL_KEY, "dev@team.dev")
    keychain.set(SUPABASE_ACCESS_KEY, "access-jwt")
    keychain.set(SUPABASE_REFRESH_KEY, "refresh-jwt")
    return keychain


def test_once_requires_a_session(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _sync_once(URL, "anon", StubAuth(), FakeKeychain())  # type: ignore[arg-type]
    assert rc == 1
    assert "kantaq sync login" in capsys.readouterr().err


def test_once_refuses_an_uninvited_email(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "kantaq_backend_supabase.lookup_active_members",
        lambda *a, **k: [_member("mbr_x", "ws_x", email="other@team.dev")],
    )
    rc = _sync_once(URL, "anon", StubAuth(), _keychain_with_session())  # type: ignore[arg-type]
    assert rc == 1
    assert "ask the maintainer" in capsys.readouterr().err


def test_once_refuses_multi_workspace_membership(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "kantaq_backend_supabase.lookup_active_members",
        lambda *a, **k: [_member("mbr_1", "ws_1"), _member("mbr_2", "ws_2")],
    )
    rc = _sync_once(URL, "anon", StubAuth(), _keychain_with_session())  # type: ignore[arg-type]
    assert rc == 1
    assert "more than one workspace" in capsys.readouterr().err


# ------------------------------------------------------------------ status


def test_status_reports_locally_without_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "data" / "local.sqlite"
    db_path.parent.mkdir(parents=True)
    SQLModel.metadata.create_all(create_engine(f"sqlite:///{db_path}"))
    monkeypatch.chdir(tmp_path)  # away from any developer .env
    monkeypatch.setenv("HUB_MODE", "supabase")
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("KANTAQ_DB_URL", f"sqlite:///{db_path}")

    assert main(["sync", "status"]) == 0

    out = capsys.readouterr().out
    assert "hub_mode = supabase" in out
    assert "(not signed in)" in out
    assert "pending  = 0 event(s)" in out
