"""`kantaq enroll` against the real self-hosted backend (docs/design/enroll.md).

The DEBT-42 lesson applied from day one: everything here drives a REAL
runtime replica and the REAL ASGI sync-server (`create_app` + `TestClient`) —
no stamped actor ids, no faked joins. Postgres-gated like the rest of the
backend suite (skips without ``KANTAQ_TEST_POSTGRES_URL``; the CI Postgres
service provides one).

Deny paths proven here (the E27-T3 rule): a tampered bundle imports nothing, a
wrong passcode exits non-zero and writes nothing, an expired bundle refuses
before touching the network, a bundle/server identity mismatch refuses before
touching local state, a superseded (re-exported) bundle's credential is dead
at preflight, and a revoked enrollment stops syncing inside the NFR-E06-2
five-second budget.
"""

from __future__ import annotations

import io
import re
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from kantaq.cli import main as cli_main
from kantaq.enroll import (
    BundleExpiredError,
    EnrollError,
    EnrollPayload,
    ImportResult,
    import_enrollment,
    provision_enrollment,
    revoke_enrollment,
    seal,
    unseal,
)
from kantaq_backend_postgres import SyncBackendError, SyncServerBackend, create_app, create_schema
from kantaq_core.identity import (
    FileKeychain,
    IdentityError,
    IdentityService,
    local_device,
    verification_roots,
)
from kantaq_db.models import Member
from kantaq_db.session import get_engine, sqlite_url
from kantaq_protocol import Event
from kantaq_test_harness.db import EphemeralPostgres

SERVER_URL = "http://testserver"
PASSCODE = "test-passcode-for-the-suite"
OWNER = "founder@acme.dev"
JOINER = "joiner@acme.dev"


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    """A disposable Postgres with the self-hosted schema; no workspace yet —
    enrollment's own provisioning creates it, like a first `seed` would."""
    if not EphemeralPostgres.available():
        pytest.skip("no KANTAQ_TEST_POSTGRES_URL (the CI Postgres service provides one)")
    with EphemeralPostgres() as engine:
        create_schema(engine)
        yield engine


@pytest.fixture
def app_client(pg_engine: Engine) -> TestClient:
    """An httpx client wired to the real ASGI sync-server."""
    return TestClient(create_app(pg_engine))


def _pg_url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def _payload_for(
    provisioned: object, *, hub_url: str = SERVER_URL, ttl: int = 3600
) -> EnrollPayload:
    now = int(time.time())
    return EnrollPayload(
        backend_mode="postgres",
        hub_url=hub_url,
        hub_token=provisioned.token_plaintext,  # type: ignore[attr-defined]
        member_id=provisioned.member_id,  # type: ignore[attr-defined]
        member_email=JOINER,
        workspace_id=provisioned.workspace_id,  # type: ignore[attr-defined]
        workspace_name=provisioned.workspace_name,  # type: ignore[attr-defined]
        issued_at=now,
        expires_at=now + ttl,
    )


def _import(payload: EnrollPayload, tmp_path: Path, app_client: TestClient) -> ImportResult:
    return import_enrollment(
        payload,
        local_db_path=str(tmp_path / "data" / "runtime.sqlite"),
        keychain_dir=tmp_path / "data" / "keychain",
        env_path=tmp_path / ".env",
        client=app_client,
    )


def _ticket_event(actor_id: str, *, n: int, title: str) -> Event:
    return Event(
        event_id=f"e{n:025d}",
        collection="tickets",
        entity_id="tkt_enrol0".ljust(26, "0"),
        actor_id=actor_id,
        actor_seq=n,
        op="patch",
        base_rev=None,
        policy_ref=None,
        payload={"title": title},
        sig=None,
    )


# ------------------------------------------------------------- owner side


def test_cli_export_provisions_and_seals_end_to_end(
    pg_engine: Engine, app_client: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real command: `kantaq enroll export` mints the member through
    `seed_member`, seals the bundle, prints the passcode — and never prints
    the token. The sealed credential actually authenticates at the server."""
    out = tmp_path / "joiner.kqe"
    rc = cli_main(
        [
            "enroll",
            "export",
            "--email",
            JOINER,
            "--workspace",
            "Acme",
            "--hub-url",
            "https://hub.internal:8889",
            "--ttl",
            "7d",
            "--database-url",
            _pg_url(pg_engine),
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    printed = capsys.readouterr().out
    match = re.search(r"passcode: (\S+)", printed)
    assert match is not None
    payload = unseal(out.read_bytes(), match.group(1))
    assert payload.member_email == JOINER
    assert payload.hub_url == "https://hub.internal:8889"
    assert payload.hub_token.startswith("kq_")
    assert payload.hub_token not in printed  # the token is sealed, never shown
    assert (out.stat().st_mode & 0o777) == 0o600
    me = SyncServerBackend(SERVER_URL, payload.hub_token, client=app_client).whoami()
    assert me["member_id"] == payload.member_id
    assert me["workspace_id"] == payload.workspace_id


def test_reexport_rotates_the_previous_token(pg_engine: Engine, app_client: TestClient) -> None:
    """Idempotent per (email, workspace): same member, fresh token — and the
    superseded bundle's credential is DEAD, even inside its TTL."""
    first = provision_enrollment(pg_engine, email=JOINER, workspace="Acme")
    second = provision_enrollment(pg_engine, email=JOINER, workspace="Acme")
    assert second.member_id == first.member_id
    assert second.rotated is True
    assert second.token_plaintext != first.token_plaintext
    with pytest.raises(SyncBackendError) as denied:
        SyncServerBackend(SERVER_URL, first.token_plaintext, client=app_client).whoami()
    assert denied.value.status_code == 401
    me = SyncServerBackend(SERVER_URL, second.token_plaintext, client=app_client).whoami()
    assert me["member_id"] == first.member_id


def test_workspace_guard_refuses_the_wrong_backend(pg_engine: Engine) -> None:
    provision_enrollment(pg_engine, email=OWNER, workspace="Acme", role="Owner")
    with pytest.raises(EnrollError, match="refusing to enroll into"):
        provision_enrollment(pg_engine, email=JOINER, workspace="SomeOtherTeam")


def test_reexport_for_a_revoked_member_refuses(pg_engine: Engine) -> None:
    provision_enrollment(pg_engine, email=OWNER, workspace="Acme", role="Owner")
    provision_enrollment(pg_engine, email=JOINER, workspace="Acme")
    revoke_enrollment(pg_engine, email=JOINER)
    with pytest.raises(EnrollError, match="revoked"):
        provision_enrollment(pg_engine, email=JOINER, workspace="Acme")


# ------------------------------------------------------------- joiner side


def test_import_joins_devices_and_pushes_end_to_end(
    pg_engine: Engine, app_client: TestClient, tmp_path: Path
) -> None:
    """The whole joiner story on a REAL runtime replica: schema migrated,
    identity adopted (the DEBT-42 path), runtime token parked in the keychain,
    device keypair registered as a verification root, `.env` written 0600 —
    and an event authored AS the adopted member commits at the server."""
    provisioned = provision_enrollment(pg_engine, email=JOINER, workspace="Acme")
    result = _import(_payload_for(provisioned), tmp_path, app_client)

    assert result.already_joined is False
    assert result.member_id == provisioned.member_id

    runtime = get_engine(sqlite_url(str(tmp_path / "data" / "runtime.sqlite")))
    keychain = FileKeychain(tmp_path / "data" / "keychain")
    with Session(runtime) as session:
        owner = session.exec(select(Member)).one()
        assert owner.id == provisioned.member_id  # the runtime IS the seeded member
        device = local_device(session, keychain)
        assert device is not None  # keypair generated + registered
        roots = verification_roots(session)
        assert roots[device.id] == device.public_key  # a live verification root
    assert keychain.get("runtime-token") is not None  # local API token parked

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "HUB_MODE=postgres" in env_text
    assert f"HUB_URL={SERVER_URL}" in env_text
    assert f"HUB_TOKEN={provisioned.token_plaintext}" in env_text
    assert ((tmp_path / ".env").stat().st_mode & 0o777) == 0o600

    committed = SyncServerBackend(
        SERVER_URL, provisioned.token_plaintext, client=app_client
    ).commit_events(
        [_ticket_event(provisioned.member_id, n=1, title="from the enrolled runtime")],
        require_signature=False,
    )
    assert committed[0].status == "committed"


def test_import_is_idempotent(pg_engine: Engine, app_client: TestClient, tmp_path: Path) -> None:
    provisioned = provision_enrollment(pg_engine, email=JOINER, workspace="Acme")
    payload = _payload_for(provisioned)
    first = _import(payload, tmp_path, app_client)
    second = _import(payload, tmp_path, app_client)
    assert first.already_joined is False
    assert second.already_joined is True


def test_import_refuses_an_expired_bundle_before_any_network(
    pg_engine: Engine, tmp_path: Path
) -> None:
    """TTL runs first: hub_url points at a dead port and is never contacted."""
    provisioned = provision_enrollment(pg_engine, email=JOINER, workspace="Acme")
    payload = _payload_for(provisioned, hub_url="http://127.0.0.1:1", ttl=-10)
    with pytest.raises(BundleExpiredError):
        import_enrollment(
            payload,
            local_db_path=str(tmp_path / "data" / "runtime.sqlite"),
            keychain_dir=tmp_path / "data" / "keychain",
            env_path=tmp_path / ".env",
        )
    assert not (tmp_path / "data").exists()  # nothing was written
    assert not (tmp_path / ".env").exists()


def test_import_refuses_a_bundle_server_identity_mismatch(
    pg_engine: Engine, app_client: TestClient, tmp_path: Path
) -> None:
    """A bundle claiming a different member than its token authenticates as is
    refused at preflight — before the replica, keychain, or `.env` exist."""
    provisioned = provision_enrollment(pg_engine, email=JOINER, workspace="Acme")
    payload = _payload_for(provisioned)
    forged = EnrollPayload(
        **{
            **payload.__dict__,
            "member_id": "mbr_someoneelse".ljust(26, "0"),
        }
    )
    with pytest.raises(EnrollError, match="identity mismatch"):
        _import(forged, tmp_path, app_client)
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / ".env").exists()


def test_import_refuses_to_rehome_a_runtime_with_an_identity(
    pg_engine: Engine, app_client: TestClient, tmp_path: Path
) -> None:
    """The adopt_owner guard holds through enroll: a replica that already has
    its own Owner cannot silently become someone else."""
    from kantaq_db import migrations

    db_path = tmp_path / "data" / "runtime.sqlite"
    db_path.parent.mkdir(parents=True)
    migrations.upgrade(sqlite_url(str(db_path)))
    with Session(get_engine(sqlite_url(str(db_path)))) as session:
        IdentityService(session).bootstrap_owner()

    provisioned = provision_enrollment(pg_engine, email=JOINER, workspace="Acme")
    with pytest.raises(IdentityError, match="already has a local identity"):
        _import(_payload_for(provisioned), tmp_path, app_client)
    assert not (tmp_path / ".env").exists()  # refused before the config write


def test_import_refuses_a_superseded_bundle_at_preflight(
    pg_engine: Engine, app_client: TestClient, tmp_path: Path
) -> None:
    """Replay of an old (re-exported) bundle: decrypts fine, but its token is
    revoked, so `/v1/me` 401s and nothing local is written."""
    first = provision_enrollment(pg_engine, email=JOINER, workspace="Acme")
    provision_enrollment(pg_engine, email=JOINER, workspace="Acme")  # rotates
    with pytest.raises(SyncBackendError) as denied:
        _import(_payload_for(first), tmp_path, app_client)
    assert denied.value.status_code == 401
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / ".env").exists()


def test_import_merges_env_and_backs_up_the_original(
    pg_engine: Engine, app_client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        "LOCAL_DB_PATH=./data/local.sqlite\nHUB_MODE=local\n", encoding="utf-8"
    )
    provisioned = provision_enrollment(pg_engine, email=JOINER, workspace="Acme")
    result = _import(_payload_for(provisioned), tmp_path, app_client)
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "LOCAL_DB_PATH=./data/local.sqlite" in env_text  # unrelated line kept
    assert "HUB_MODE=postgres" in env_text and "HUB_MODE=local" not in env_text
    assert result.env_backup is not None and result.env_backup.exists()
    assert "HUB_MODE=local" in result.env_backup.read_text(encoding="utf-8")


# ------------------------------------------------------- revocation budget


def test_revoked_enrollment_stops_syncing_within_the_5s_budget(
    pg_engine: Engine, app_client: TestClient
) -> None:
    """NFR-E06-2 through the enroll surface: revoke → the server refuses the
    old credential in under five seconds of wall clock (the TokenVerifier
    cache TTL is pinned below the budget)."""
    provisioned = provision_enrollment(pg_engine, email=JOINER, workspace="Acme")
    backend = SyncServerBackend(SERVER_URL, provisioned.token_plaintext, client=app_client)
    assert backend.whoami()["member_id"] == provisioned.member_id  # live (and cached)

    start = time.monotonic()
    revoke_enrollment(pg_engine, email=JOINER)
    while True:
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, "revocation did not propagate within the NFR-E06-2 budget"
        try:
            backend.whoami()
        except SyncBackendError as denied:
            assert denied.status_code == 401
            break
        time.sleep(0.1)


def test_revoke_is_idempotent_and_unknown_email_refuses(pg_engine: Engine) -> None:
    provision_enrollment(pg_engine, email=OWNER, workspace="Acme", role="Owner")
    provision_enrollment(pg_engine, email=JOINER, workspace="Acme")
    member_id, already = revoke_enrollment(pg_engine, email=JOINER)
    assert already is False
    member_id_again, already_again = revoke_enrollment(pg_engine, email=JOINER)
    assert (member_id_again, already_again) == (member_id, True)
    with pytest.raises(EnrollError, match="no member"):
        revoke_enrollment(pg_engine, email="ghost@acme.dev")


def test_revoking_the_last_owner_refuses(pg_engine: Engine) -> None:
    provision_enrollment(pg_engine, email=OWNER, workspace="Acme", role="Owner")
    with pytest.raises(IdentityError, match="last active Owner"):
        revoke_enrollment(pg_engine, email=OWNER)


# ------------------------------------------------------------- CLI deny paths


def test_cli_import_wrong_passcode_fails_closed_and_writes_nothing(
    pg_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deliberately-failing fixture for the passcode gate: non-interactive
    import gets exactly one attempt, exits non-zero, and the working dir gains
    no replica, no keychain, no `.env`."""
    provisioned = provision_enrollment(pg_engine, email=JOINER, workspace="Acme")
    bundle = tmp_path / "joiner.kqe"
    bundle.write_bytes(seal(_payload_for(provisioned), PASSCODE))
    workdir = tmp_path / "joiner-machine"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setattr("sys.stdin", io.StringIO("not-the-passcode\n"))
    rc = cli_main(["enroll", "import", str(bundle)])
    assert rc == 1
    assert list(workdir.iterdir()) == []  # nothing was written


def test_cli_import_tampered_bundle_fails_closed(
    pg_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deliberately-failing fixture for the tamper gate: one flipped
    ciphertext byte and the import refuses with nothing applied."""
    provisioned = provision_enrollment(pg_engine, email=JOINER, workspace="Acme")
    data = bytearray(seal(_payload_for(provisioned), PASSCODE))
    data[-20] ^= 0x01  # inside the base64 ciphertext
    bundle = tmp_path / "tampered.kqe"
    bundle.write_bytes(bytes(data))
    workdir = tmp_path / "joiner-machine"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{PASSCODE}\n"))
    rc = cli_main(["enroll", "import", str(bundle)])
    assert rc == 1
    assert list(workdir.iterdir()) == []


def test_cli_export_passcode_file_keeps_it_off_the_terminal(
    pg_engine: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Follow-up #8: --passcode-file writes the passcode to a 0600 file and does
    NOT print it to stdout (avoids scrollback / CI logs)."""
    pf = tmp_path / "pass.txt"
    rc = cli_main(
        [
            "enroll",
            "export",
            "--email",
            JOINER,
            "--workspace",
            "Acme",
            "--hub-url",
            "https://hub.internal:8889",
            "--database-url",
            _pg_url(pg_engine),
            "--out",
            str(tmp_path / "j.kqe"),
            "--passcode-file",
            str(pf),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    passcode = pf.read_text(encoding="utf-8").strip()
    assert passcode and passcode not in out  # the code is in the file, not on screen
    assert (pf.stat().st_mode & 0o777) == 0o600


def test_cli_export_refuses_plaintext_remote_hub(
    pg_engine: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Follow-up #4: exporting a bundle that points at a plaintext-http remote is
    refused, so no token-leaking bundle is ever minted."""
    rc = cli_main(
        [
            "enroll",
            "export",
            "--email",
            JOINER,
            "--workspace",
            "Acme",
            "--hub-url",
            "http://hub.acme.example:8889",
            "--database-url",
            _pg_url(pg_engine),
            "--out",
            str(tmp_path / "j.kqe"),
        ]
    )
    assert rc == 1
    assert "https is required" in capsys.readouterr().err
    assert not (tmp_path / "j.kqe").exists()


def test_cli_export_refuses_the_deferred_supabase_backend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli_main(
        [
            "enroll",
            "export",
            "--email",
            JOINER,
            "--workspace",
            "Acme",
            "--hub-url",
            "http://hub:8889",
            "--backend",
            "supabase",
            "--database-url",
            "postgresql://unused/unused",
            "--out",
            str(tmp_path / "x.kqe"),
        ]
    )
    assert rc == 2
    assert "DEBT-43" in capsys.readouterr().err
    assert not (tmp_path / "x.kqe").exists()
