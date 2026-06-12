"""Token-gated loopback auth for the runtime API (E06-T2, SEC).

The runtime binds to 127.0.0.1, but localhost is not trust: any local process
or any web page the user's browser has open can reach a loopback port. So
every ``/v1/*`` request needs a bearer token (sprint rule: "a token is
required even on localhost"), and browser-initiated requests must come from
the runtime's own origin — a cross-origin ``Origin`` header is rejected
before the token is even read (DNS-rebinding / CSRF hardening). ``/healthz``
and the static SPA stay open; they expose nothing private.

The verifier and engine live on ``app.state`` so tests inject a temp database
and a FakeClock-driven verifier.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.engine import Engine

from kantaq_core.identity import (
    Action,
    FileKeychain,
    IdentityService,
    Keychain,
    TokenVerifier,
    VerifiedActor,
    can,
)
from kantaq_runtime.config import Settings

RUNTIME_TOKEN_KEY = "runtime-token"


def keychain_for(settings: Settings) -> FileKeychain:
    """The runtime's keychain: 0600 files next to the local database (D-06)."""
    return FileKeychain(Path(settings.local_db_path).parent / "keychain")


def ensure_local_identity(engine: Engine, keychain: Keychain) -> str | None:
    """First boot: mint the Owner and park their token in the keychain.

    Returns the plaintext when freshly minted (the caller prints it once);
    None when members already exist. Idempotent across boots.
    """
    from sqlmodel import Session

    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
    if minted is None:
        return None
    keychain.set(RUNTIME_TOKEN_KEY, minted.plaintext)
    return minted.plaintext


def ensure_device_identity(engine: Engine, keychain: Keychain) -> str:
    """Boot-time device keypair + registration (E06-T4, FR-E06-3, D-01).

    Idempotent: the seed lives in the keychain, the verify key in the
    ``devices`` row. Registration is local in v0.1; it reaches the backend
    when Sprint 4 wires the verified sync path (E24-T5). Returns the device
    row id; the private key never leaves the keychain.
    """
    from sqlmodel import Session, col, select

    from kantaq_core.identity import ensure_device
    from kantaq_db.models import Member
    from kantaq_sync_engine import EventLogSink

    with Session(engine) as session:
        owner = session.exec(
            select(Member).where(Member.status == "active").order_by(col(Member.id))
        ).first()
        owner_id = owner.id if owner is not None else None
        device = ensure_device(
            session,
            keychain,
            member_id=owner_id,
            sink=EventLogSink(session, owner_id) if owner_id is not None else None,
        )
        session.commit()
        return device.id


def _allowed_origins(settings: Settings) -> frozenset[str]:
    return frozenset(
        {
            f"http://{settings.host}:{settings.port}",
            f"http://localhost:{settings.port}",
            f"http://127.0.0.1:{settings.port}",
        }
    )


def get_engine_dep(request: Request) -> Engine:
    engine: Engine | None = getattr(request.app.state, "engine", None)
    if engine is None:
        from kantaq_db.session import get_engine, sqlite_url

        settings: Settings = request.app.state.settings
        engine = get_engine(sqlite_url(settings.local_db_path))
        request.app.state.engine = engine
    return engine


def get_verifier(request: Request) -> TokenVerifier:
    verifier: TokenVerifier | None = getattr(request.app.state, "verifier", None)
    if verifier is None:
        verifier = TokenVerifier(get_engine_dep(request))
        request.app.state.verifier = verifier
    return verifier


def require_actor(
    request: Request, verifier: Annotated[TokenVerifier, Depends(get_verifier)]
) -> VerifiedActor:
    """The auth gate on every /v1 route: origin first, then the bearer token."""
    settings: Settings = request.app.state.settings
    origin = request.headers.get("origin")
    if origin is not None and origin not in _allowed_origins(settings):
        raise HTTPException(status_code=403, detail="origin not allowed")

    header = request.headers.get("authorization", "")
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        raise HTTPException(
            status_code=401,
            detail="bearer token required (even on localhost)",
            headers={"WWW-Authenticate": "Bearer"},
        )
    actor = verifier.verify(credentials.strip())
    if actor is None:
        raise HTTPException(
            status_code=401,
            detail="invalid or revoked token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return actor


class require_action:  # noqa: N801 - reads as a dependency, not a class
    """Dependency factory: 403 unless the actor's role/scopes allow ``action``."""

    def __init__(self, action: Action) -> None:
        self._action = action

    def __call__(self, actor: Annotated[VerifiedActor, Depends(require_actor)]) -> VerifiedActor:
        if not can(actor.role, self._action, scopes=list(actor.scopes)):
            raise HTTPException(
                status_code=403, detail=f"role {actor.role!r} may not {self._action.value}"
            )
        return actor
