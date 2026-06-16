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


# ----------------------------------------------------------------- retention


def test_run_due_retention_runs_then_throttles(tmp_path: Path) -> None:
    """The sync-cycle retention seam (MOD-27 §Retention 3): the first pass runs and
    stamps the once/day marker; an immediate second pass is throttled."""
    from sqlmodel import Session, select

    from kantaq.cli import _run_due_retention
    from kantaq_core import retention
    from kantaq_db import LocalSetting

    db = create_engine(f"sqlite:///{tmp_path / 'r.sqlite'}")
    SQLModel.metadata.create_all(db)
    with Session(db) as s:
        assert retention.due(s) is True  # never run → due

    _run_due_retention(db, actor_id="mbr_1", safe_watermark_rev=None)

    with Session(db) as s:
        assert retention.due(s) is False, "the once/day marker was not stamped"
        marker = s.exec(
            select(LocalSetting).where(LocalSetting.key == retention.LAST_RUN_KEY)
        ).first()
        assert marker is not None

    # A second pass within the day is a no-op (does not raise, marker unchanged).
    _run_due_retention(db, actor_id="mbr_1", safe_watermark_rev=None)
    with Session(db) as s:
        assert retention.due(s) is False


def test_sync_once_invokes_the_retention_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """E07-T4 wiring: a successful ``kantaq sync once`` invokes the retention pass
    (it rides the sync cycle). The sync mechanics are faked — this pins only that
    the cycle reaches ``_run_due_retention`` with the resolved actor + watermark."""
    import types

    import kantaq.cli as cli
    import kantaq_backend_supabase as backend_mod
    import kantaq_core.identity as identity_mod
    import kantaq_sync_engine as engine_mod

    db_path = tmp_path / "data" / "local.sqlite"
    db_path.parent.mkdir(parents=True)
    SQLModel.metadata.create_all(create_engine(f"sqlite:///{db_path}"))
    monkeypatch.setattr(cli, "_db_url", lambda: f"sqlite:///{db_path}")

    monkeypatch.setattr(
        backend_mod, "lookup_active_members", lambda *a, **k: [_member("mbr_1", "ws_1")]
    )

    class _FakeBackend:
        def __init__(self, *a: object, **k: object) -> None: ...

    class _FakeEngine:
        def __init__(self, *a: object, **k: object) -> None: ...

        def flush_outbox(self, **k: object) -> types.SimpleNamespace:
            return types.SimpleNamespace(
                committed=0, reconciled=0, rejected=0, stale=0, rebased=0, submitted=0
            )

        def apply_inbox(self) -> types.SimpleNamespace:
            return types.SimpleNamespace(applied=0, own_reconciled=0, cursor=0)

    monkeypatch.setattr(backend_mod, "SupabaseSyncBackend", _FakeBackend)
    monkeypatch.setattr(cli, "_verifying_backend", lambda *a, **k: object())
    monkeypatch.setattr(identity_mod, "local_device", lambda *a, **k: None)
    monkeypatch.setattr(engine_mod, "SyncEngine", _FakeEngine)

    calls: list[tuple[str, int | None]] = []
    monkeypatch.setattr(
        cli,
        "_run_due_retention",
        lambda db, *, actor_id, safe_watermark_rev: calls.append((actor_id, safe_watermark_rev)),
    )

    rc = _sync_once(URL, "anon", StubAuth(), _keychain_with_session())  # type: ignore[arg-type]
    assert rc == 0
    assert calls == [("mbr_1", None)], "the sync cycle must invoke the retention seam once"
