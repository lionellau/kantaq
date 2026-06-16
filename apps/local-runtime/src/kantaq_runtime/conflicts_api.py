"""Conflicts API: review + resolve a sync conflict (E20-T5, MOD-12 / MOD-26 §B4).

The Inbox's sync-conflict tab reads open ``conflict_records`` here and resolves
one by picking a side. A ``conflict_record`` is **minted at the authoritative
merge** (E05-T2/T3) and folds into every replica, so this surface only *lists*
the local rows and *resolves* one — it never mints.

Resolution reuses the shipped, adversarially-reviewed
``SyncEngine.resolve_conflict`` (E05-T3): the superseding field write and the
``status=resolved`` flip commit **together** as one compare-and-swap against the
record's ``head_rev``. If the field moved the RPC commits nothing and raises
``RebaseRequired`` — the value never clobbers a live newer contender (the
resolver-vs-writer hole), the record stays open, and the human re-decides. One
CAS path, no drift (the §"one validator" rule).

The engine needs the shared backend; the runtime gets it from
``app.state.conflict_engine_factory`` (tests/e2e inject a ``FakeBackend``) or, in
production, builds the verifying Supabase backend from the keychain session. A
local-only workspace has no shared log and thus no conflicts, so resolve there
returns 409 with a clear pointer to sync.

Permission: reading needs ``tickets.read``; resolving needs ``tickets.write`` —
an agent scope carries only ``proposals.write``, so an agent can never silently
resolve a human's conflict (the §8.5 propose-first rule, enforced at this edge).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from kantaq_core.identity import Action, VerifiedActor
from kantaq_db.models import ConflictRecord
from kantaq_runtime.auth import get_engine_dep, require_action
from kantaq_sync_engine import RebaseRequired, ResolveResult, SyncEngine

router = APIRouter(prefix="/v1/conflicts", tags=["conflicts"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
ReaderActor = Annotated[VerifiedActor, Depends(require_action(Action.tickets_read))]
WriterActor = Annotated[VerifiedActor, Depends(require_action(Action.tickets_write))]

CONFLICT_STATUSES = ("open", "resolved")
RESOLVE_CHOICES = ("keep-A", "keep-B", "new-value")


class ConflictOut(BaseModel):
    id: str
    collection: str
    entity_id: str
    field: str
    candidate_values: dict[str, Any]  # {"keep_a": <head>, "keep_b": <loser>}
    contending_revisions: list[int]
    base_rev: int
    head_rev: int
    actor: str  # the losing write's actor
    status: str
    resolved_by: str | None
    resolved_choice: str | None
    created_at: datetime
    resolved_at: datetime | None

    @classmethod
    def from_row(cls, row: ConflictRecord) -> ConflictOut:
        return cls(
            id=row.id,
            collection=row.collection,
            entity_id=row.entity_id,
            field=row.field,
            candidate_values=row.candidate_values,
            contending_revisions=row.contending_revisions,
            base_rev=row.base_rev,
            head_rev=row.head_rev,
            actor=row.actor,
            status=row.status,
            resolved_by=row.resolved_by,
            resolved_choice=row.resolved_choice,
            created_at=row.created_at,
            resolved_at=row.resolved_at,
        )


class ResolveIn(BaseModel):
    choice: str  # keep-A | keep-B | new-value
    new_value: Any | None = None


class ResolveOut(BaseModel):
    conflict_id: str
    resolved: bool
    # True when the contended field moved past head_rev: nothing was applied, the
    # record stays open, and the human re-decides against the live contender.
    rebase_required: bool


@router.get("", response_model=list[ConflictOut])
def list_conflicts(
    actor: ReaderActor, engine: EngineDep, status: str = "open"
) -> list[ConflictOut]:
    """Open (default) or resolved conflict records, newest first. Live, no cache."""
    if status not in CONFLICT_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown conflict status {status!r}; expected one of {CONFLICT_STATUSES}",
        )
    with Session(engine) as session:
        rows = session.exec(
            select(ConflictRecord)
            .where(col(ConflictRecord.status) == status)
            .order_by(col(ConflictRecord.created_at).desc(), col(ConflictRecord.id).desc())
        ).all()
        return [ConflictOut.from_row(row) for row in rows]


@router.post("/{conflict_id}/resolve", response_model=ResolveOut)
def resolve_conflict(
    conflict_id: str, body: ResolveIn, actor: WriterActor, engine: EngineDep, request: Request
) -> ResolveOut:
    if body.choice not in RESOLVE_CHOICES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown resolve choice {body.choice!r}; expected one of {RESOLVE_CHOICES}",
        )
    with Session(engine) as session:
        record = session.get(ConflictRecord, conflict_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no such conflict: {conflict_id}")

    sync_engine = _resolve_engine(request, engine, actor.member_id)
    if sync_engine is None:
        raise HTTPException(
            status_code=409,
            detail="conflict resolution needs the shared backend; sign in and sync "
            "(`kantaq sync login`) — a local-only workspace has no conflicts to resolve",
        )
    try:
        result: ResolveResult = sync_engine.resolve_conflict(
            conflict_id, body.choice, new_value=body.new_value, resolved_by=actor.member_id
        )
    except RebaseRequired:
        # Defensive: the engine maps this to rebase_required internally, but a
        # backend that raises out is surfaced as the same not-applied outcome.
        return ResolveOut(conflict_id=conflict_id, resolved=False, rebase_required=True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ResolveOut(
        conflict_id=result.conflict_id,
        resolved=result.resolved,
        rebase_required=result.rebase_required,
    )


def _resolve_engine(request: Request, db: Engine, actor_id: str) -> SyncEngine | None:
    """The backend-backed engine for resolution.

    Tests + the e2e server inject ``app.state.conflict_engine_factory`` (a
    ``FakeBackend``-backed engine) so the CAS path runs hermetically. Production
    builds the verifying Supabase backend from the keychain session; any
    misconfiguration (no session, local mode) returns ``None`` → a 409, never a
    crash.
    """
    factory = getattr(request.app.state, "conflict_engine_factory", None)
    if factory is not None:
        return factory(db=db, actor_id=actor_id)  # type: ignore[no-any-return]
    return _build_supabase_engine(request.app.state.settings, db, actor_id)


def _build_supabase_engine(settings: Any, db: Engine, actor_id: str) -> SyncEngine | None:
    """Build the verifying Supabase-backed engine (mirrors `kantaq sync`).

    Network-dependent and exercised by the live smoke; the resolution *logic* is
    covered hermetically via the injected ``FakeBackend`` factory. Returns
    ``None`` (→ 409) when the workspace is local-only or has no signed-in session.
    """
    from kantaq_runtime.config import HubMode

    if settings.hub_mode != HubMode.supabase or not settings.supabase_url:
        return None
    try:  # pragma: no cover - network path, covered by the live smoke
        from datetime import UTC, datetime

        from kantaq_backend_supabase import SupabaseAuth, SupabaseSyncBackend, lookup_active_members
        from kantaq_core import audit
        from kantaq_core.identity import local_grant_index, verification_roots
        from kantaq_runtime.auth import keychain_for
        from kantaq_sync_engine import VerifyContext, VerifyingBackend

        keychain = keychain_for(settings)
        email = keychain.get("supabase-session-email")
        refresh_token = keychain.get("supabase-refresh-token")
        if not email or not refresh_token:
            return None
        url, anon_key = settings.supabase_url, settings.supabase_anon_key
        auth = SupabaseAuth(url, anon_key)
        session_tokens = auth.refresh(refresh_token)
        access_token = session_tokens.access_token
        # RLS scopes the lookup to the signed-in user's workspaces (no email arg).
        members = lookup_active_members(url, anon_key, access_token)
        mine = [m for m in members if m.email == email]
        if len(mine) != 1:
            return None
        me = mine[0]

        def context() -> VerifyContext:
            with Session(db) as session:
                grants, revoked = local_grant_index(session)
                return VerifyContext(
                    roots=verification_roots(session),
                    grants=grants,
                    now=int(datetime.now(UTC).timestamp()),
                    revoked_ids=revoked,
                    require_signature=settings.sign_events,
                    workspace_id=me.workspace_id,
                )

        def on_deny(event: Any, verdict: Any) -> None:
            with Session(db) as session:
                audit.write(
                    session,
                    actor_id=actor_id,
                    action="sync.denied",
                    source="sync",
                    object_ref=f"{event.collection}/{event.entity_id}",
                    after={"code": verdict.code, "reason": verdict.reason},
                )
                session.commit()

        backend = VerifyingBackend(
            SupabaseSyncBackend(
                url,
                anon_key,
                workspace_id=me.workspace_id,
                access_token=lambda: access_token,  # the caller owns the session
            ),
            context,
            cutover_rev=settings.sign_cutover_rev,
            on_deny=on_deny,
        )
        return SyncEngine(db, backend, actor_id=me.id, workspace_id=me.workspace_id)
    except Exception:
        return None
