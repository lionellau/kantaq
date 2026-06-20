"""The self-hosted sync-server (E25-T1 / MOD-28) — FastAPI over Postgres.

The HTTP face of the self-hosted backend: the endpoint set the runtime's sync
engine drives, served by a plain FastAPI app over a plain Postgres, with **no
Supabase, no PostgREST, no RLS**. Authorisation is the validator core, not
database policy (D-31): a Bearer **member token** authenticates the caller
(``TokenVerifier`` — the same one the local runtime uses), the server binds the
acting member (``actor_id`` must be the authenticated member — the
``is_self_in_workspace`` wall the plpgsql RPC enforces), and the shared
``verify_event`` authorises every write against the server's own trust tables.
OIDC is deferred (DEBT-14); the auth is token + grant, the same as Supabase mode.

Endpoints (scoped to the authenticated member's workspace):

- ``GET  /healthz``            — liveness (the compose healthcheck hits this).
- ``POST /v1/session``         — the §B7 version handshake.
- ``POST /v1/events``          — the atomic commit (the events.sql twin).
- ``GET  /v1/events``          — pull committed events since a cursor.
- ``GET  /v1/snapshot``        — the LWW fold of a collection.
- ``POST /v1/acks``            — report a replica's ack watermark.
- ``GET  /v1/acks/watermark``  — the safe (MIN) watermark across live replicas.

Blob storage and the audit-range read are the Sprint-9 self-host hardening
(MOD-28 "continues in Sprint 9: backup, object storage, …"); they are tracked,
not silently dropped (see the deliverable + DEBT-40).
"""

# NOTE: deliberately NOT `from __future__ import annotations`. FastAPI resolves
# route annotations via get_type_hints against module globals; the auth
# dependency (`require_actor`) is a closure local to `create_app`, so a
# stringized `Annotated[VerifiedActor, Depends(require_actor)]` could not be
# resolved and the param would be mis-read as a query field. Real (eager)
# annotations capture the closure correctly. (Py 3.12 handles the builtin
# generics/unions used below natively, so no future import is needed.)

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_backend_postgres.backend import PostgresSyncBackend
from kantaq_core.identity.tokens import TokenVerifier, VerifiedActor
from kantaq_db.models import Member
from kantaq_sync_engine.events import Event, RebaseRequired
from kantaq_sync_engine.verify import POLICY_DENIED, EventRejected, EventVerification

# ----------------------------------------------------------------- wire models


class WireEvent(BaseModel):
    event_id: str
    collection: str
    entity_id: str
    actor_id: str
    actor_seq: int
    op: str
    base_rev: int | None = None
    policy_ref: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    sig: str | None = None
    # workspace_id is accepted for wire-compat with the Supabase client but is
    # NOT trusted: the server scopes to the authenticated member's workspace.
    workspace_id: str | None = None

    def to_event(self) -> Event:
        return Event(
            event_id=self.event_id,
            collection=self.collection,
            entity_id=self.entity_id,
            actor_id=self.actor_id,
            actor_seq=self.actor_seq,
            op=self.op,  # type: ignore[arg-type]
            base_rev=self.base_rev,
            policy_ref=self.policy_ref,
            payload=dict(self.payload),
            sig=self.sig,
        )


class EventsRequest(BaseModel):
    events: list[WireEvent]
    require_signature: bool = True
    cas: bool = False


class SessionRequest(BaseModel):
    sync_version: int
    schema_version: int


class AckRequest(BaseModel):
    member_id: str
    replica_id: str
    acked_rev: int


def _committed_to_wire(ce: Any) -> dict[str, Any]:
    e = ce.event
    return {
        "revision": ce.revision,
        "event_id": e.event_id,
        "collection": e.collection,
        "entity_id": e.entity_id,
        "actor_id": e.actor_id,
        "actor_seq": e.actor_seq,
        "op": e.op,
        "base_rev": e.base_rev,
        "policy_ref": e.policy_ref,
        "payload": dict(e.payload),
        "sig": e.sig,
    }


def create_app(
    engine: Engine,
    *,
    require_signature: bool = False,
    now: Callable[[], int] | None = None,
    token_now: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Build the sync-server over ``engine``.

    ``require_signature`` is the SERVER's signature-cutover floor (SEC): once a
    self-hosted workspace cuts over to signed sync (``KANTAQ_REQUIRE_SIGNATURE=
    true``), the server rejects every unsigned / grant-less write and a client
    **cannot relax it** — the client's own ``require_signature`` may only ratchet
    the requirement *stricter*, never below the server floor. This closes the
    no-RLS gap: without it a member could send ``require_signature=false`` to
    commit unsigned events that skip the per-verb grant check (in Supabase mode
    RLS is the backstop; the self-hosted server has none, so the floor lives
    here). Default ``False`` matches a fresh, pre-cutover workspace (the runtime's
    ``sign_events`` default, D-15); the caller-binding (actor == member) still
    holds pre-cutover.

    ``now`` is unix seconds for the grant-window checks (injectable in tests);
    ``token_now`` is the monotonic clock for the token-verify cache.
    """
    app = FastAPI(title="kantaq self-hosted sync-server", version="0.3.0")
    verifier = TokenVerifier(engine, now=token_now)
    grant_clock = now or (lambda: int(datetime.now(UTC).timestamp()))
    server_require_signature = require_signature

    def require_actor(request: Request) -> VerifiedActor:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise HTTPException(
                401,
                "bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        actor = verifier.verify(header[7:].strip())
        if actor is None:
            raise HTTPException(
                401,
                "invalid or revoked token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return actor

    def _workspace_of(member_id: str) -> str:
        with Session(engine) as session:
            member = session.get(Member, member_id)
            if member is None:
                raise HTTPException(403, "member not found")
            return member.workspace_id

    def _backend(member_id: str) -> PostgresSyncBackend:
        return PostgresSyncBackend(engine, workspace_id=_workspace_of(member_id), now=grant_clock)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/session")
    def session_init(
        body: SessionRequest, actor: Annotated[VerifiedActor, Depends(require_actor)]
    ) -> dict[str, int]:
        init = _backend(actor.member_id).session_init(
            sync_version=body.sync_version, schema_version=body.schema_version
        )
        return {"sync_version": init.sync_version, "schema_version": init.schema_version}

    @app.post("/v1/events")
    def commit(
        body: EventsRequest, actor: Annotated[VerifiedActor, Depends(require_actor)]
    ) -> list[dict[str, Any]]:
        backend = _backend(actor.member_id)
        # The signature requirement is the SERVER's floor; a client may only make
        # it STRICTER, never relax it (SEC — no-RLS gap). So a cut-over server
        # (server floor True) rejects unsigned/grant-less writes even if the
        # client sends require_signature=false.
        effective_require_signature = server_require_signature or body.require_signature
        # The shared verifier, wrapped with caller-binding: the actor_id must be
        # the authenticated member (the plpgsql is_self_in_workspace wall) — a
        # member can never submit an event as someone else, signed or not.
        inner = backend.verifier(require_signature=effective_require_signature)

        def verify(event: Event) -> EventVerification:
            if event.actor_id != actor.member_id:
                return EventVerification(
                    False, POLICY_DENIED, "actor is not the authenticated member"
                )
            return inner(event)

        events = [w.to_event() for w in body.events]
        try:
            return backend.commit_events_raw(events, verify=verify, cas=body.cas)
        except EventRejected as exc:
            raise HTTPException(422, {"code": exc.code, "reason": exc.reason}) from exc
        except RebaseRequired as exc:
            raise HTTPException(
                409, {"code": "rebase_required", "event_id": exc.event.event_id}
            ) from exc

    @app.get("/v1/events")
    def pull(
        actor: Annotated[VerifiedActor, Depends(require_actor)],
        since: int = 0,
        collection: str | None = None,
    ) -> list[dict[str, Any]]:
        return [_committed_to_wire(ce) for ce in _backend(actor.member_id).pull(collection, since)]

    @app.get("/v1/snapshot")
    def snapshot(
        actor: Annotated[VerifiedActor, Depends(require_actor)], collection: str
    ) -> dict[str, dict[str, Any]]:
        return _backend(actor.member_id).snapshot(collection)

    @app.post("/v1/acks")
    def ack(
        body: AckRequest, actor: Annotated[VerifiedActor, Depends(require_actor)]
    ) -> dict[str, str]:
        _backend(actor.member_id).update_ack_watermark(
            member_id=body.member_id, replica_id=body.replica_id, acked_rev=body.acked_rev
        )
        return {"status": "ok"}

    @app.get("/v1/acks/watermark")
    def watermark(
        actor: Annotated[VerifiedActor, Depends(require_actor)], ttl_days: int = 30
    ) -> dict[str, int | None]:
        return {
            "safe_watermark_rev": _backend(actor.member_id).safe_watermark_rev(ttl_days=ttl_days)
        }

    return app
