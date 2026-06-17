"""Boot a disposable runtime for the Playwright hero-flow e2e (MOD-11/MOD-12).

Playwright's ``webServer`` runs this script (see ``web/playwright.config.ts``).
It stands up the same stack a member runs — migrations, the bootstrap Owner,
the FastAPI app serving the built web UI — against a throwaway temp database,
then seeds the approve-flow fixtures: a project, a ticket, and one pending
agent proposal created through the real propose path (``agent_action_propose``).

The Owner token and seeded ids land in ``web/e2e/.runtime/state.json`` so the
specs can connect exactly like a human would (paste the token in Settings).
The file lives under e2e/.runtime (gitignored) and dies with the run.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "web" / "e2e" / ".runtime"
PORT = int(os.environ.get("KANTAQ_E2E_PORT", "39391"))


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="kantaq-e2e-"))
    # Configure before any kantaq import reads settings: the runtime must
    # serve, store, and origin-check against this disposable identity.
    os.environ["LOCAL_DB_PATH"] = str(data_dir / "local.sqlite")
    os.environ["HUB_MODE"] = "local"
    os.environ["PORT"] = str(PORT)
    os.environ.pop("KANTAQ_DB_URL", None)

    from kantaq.cli import main as kantaq_cli

    rc = kantaq_cli(["db", "migrate"])
    if rc != 0:
        print("e2e server: migrations failed", file=sys.stderr)
        return rc

    from sqlmodel import Session, select

    from kantaq_core.identity import IdentityService, Role
    from kantaq_core.tracker import TrackerService
    from kantaq_db import new_ulid
    from kantaq_db.models import Workspace
    from kantaq_db.session import get_engine, sqlite_url
    from kantaq_mcp.tools import agent_action_propose
    from kantaq_runtime.app import create_app
    from kantaq_runtime.auth import ensure_local_identity, keychain_for
    from kantaq_runtime.config import get_settings
    from kantaq_sync_engine import (
        Event,
        EventLogSink,
        SyncEngine,
        conflict_record_id,
        entity_base_rev,
        insert_event,
        next_actor_seq,
        refold_entity,
    )
    from kantaq_test_harness.backend import FakeBackend

    settings = get_settings()
    engine = get_engine(sqlite_url(settings.local_db_path))

    token = ensure_local_identity(engine, keychain_for(settings))
    if token is None:
        print("e2e server: expected a fresh database (owner already exists)", file=sys.stderr)
        return 1

    backend = FakeBackend()

    # Seed an open sync-conflict (E20-T5) FIRST: create + commit a ticket to the
    # shared backend, then mint an open conflict_record on its status field. The
    # same FakeBackend backs the injected resolve engine, so the CAS at resolve
    # time matches the committed head. Done before the hero fixtures so those
    # stay local-and-pending for the approve flow.
    with Session(engine) as session:
        identity = IdentityService(session)
        owner = identity.list_members()[0]
        owner_id = owner.id
        agent = identity.invite(
            email="agent@e2e.local",
            role=Role.agent,
            scopes=["tickets.read", "proposals.write"],
        )
        workspace = session.exec(select(Workspace)).one()
        workspace_id = workspace.id
        tracker = TrackerService(
            session, actor_id=owner_id, source="cli", sink=EventLogSink(session, owner_id)
        )
        project = tracker.create_project(workspace_id=workspace_id, name="Hero Project")
        project_id = project.id
        conflict_ticket = tracker.create_ticket(
            project_id=project_id, title="Conflicted ticket", status="todo"
        )
        conflict_ticket_id = conflict_ticket.id
        session.commit()

    sync = SyncEngine(engine, backend, actor_id=owner_id, workspace_id=workspace_id)
    sync.flush_outbox()  # commit the project + conflict ticket to the shared backend

    with Session(engine) as session:
        head = entity_base_rev(session, "tickets", conflict_ticket_id)
        assert head is not None
        conflict_id = conflict_record_id(conflict_ticket_id, "status", [head])
        cr_event = Event(
            event_id=new_ulid(),
            collection="conflict_records",
            entity_id=conflict_id,
            actor_id=owner_id,
            actor_seq=next_actor_seq(session, owner_id),
            op="patch",
            payload={
                "workspace_id": workspace_id,
                "collection": "tickets",
                "entity_id": conflict_ticket_id,
                "field": "status",
                "contending_revisions": [head],
                "candidate_values": {"keep_a": "doing", "keep_b": "todo"},
                "base_rev": 0,
                "head_rev": head,
                "actor": owner_id,
                "status": "open",
            },
        )
        committed = backend.commit_events([cr_event])
        insert_event(session, cr_event, committed_rev=committed[0].revision)
        refold_entity(session, "conflict_records", conflict_id)
        session.commit()

    # The hero approve-flow fixtures (kept local + pending, after the flush): a
    # Seeded ticket + a pending agent proposal created through the real path.
    with Session(engine) as session:
        tracker = TrackerService(
            session, actor_id=owner_id, source="cli", sink=EventLogSink(session, owner_id)
        )
        ticket = tracker.create_ticket(
            project_id=project_id,
            title="Seeded ticket",
            description="Seeded for the approve-a-proposal end-to-end.",
        )
        ticket_id = ticket.id
    with Session(engine) as session:
        agent_action_propose(
            session,
            actor_id=agent.member_id,
            args={
                "ticket_id": ticket_id,
                "changes": {"status": "doing"},
                "note": "e2e seeded proposal",
            },
            now=lambda: datetime.now(UTC).replace(tzinfo=None),
        )

    # Seed one *denied* agent call so the Inbox "Denied calls" tab shows the
    # 試錯 → human-visibility loop end to end: a propose-only agent that reaches
    # for the approve tool is denied at the gateway (tool_allowlist) and the
    # denial is an audited tool.deny row a human can see (UAT-A6.1 / FR-E07-2).
    from kantaq_mcp.gateway import Gateway, GatewayDenied

    deny_gateway = Gateway(engine)
    agent_actor = deny_gateway.authenticate(agent.plaintext)
    if agent_actor is not None:
        agent_session = deny_gateway.session_for(agent_actor, session_id="uat-denied-seed")
        # The denial is the point: handle_call raises GatewayDenied and writes an
        # audited tool.deny row the Inbox "Denied calls" tab then surfaces.
        with contextlib.suppress(GatewayDenied):
            deny_gateway.handle_call(
                actor=agent_actor,
                session=agent_session,
                tool_name="agent_action_approve",
                args={"proposal_id": "01JZZZZZZZZZZZZZZZZZZZZZZZ"},
            )

    # Mint one token per base role so the role-UAT spec can connect to the SAME
    # web UI as each user type (Owner already has `token`; Agent above). These
    # are throwaway sandbox credentials — never real members (UAT plan Track B).
    role_tokens = {"Owner": token, "Agent": agent.plaintext}
    with Session(engine) as session:
        identity = IdentityService(session)
        for role in (Role.maintainer, Role.member, Role.viewer):
            minted = identity.invite(email=f"{role.value.lower()}@e2e.local", role=role, scopes=[])
            role_tokens[role.value] = minted.plaintext

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "state.json").write_text(
        json.dumps(
            {
                "base_url": f"http://127.0.0.1:{PORT}",
                "token": token,
                "ticket_id": ticket_id,
                "project_id": project_id,
                "conflict_id": conflict_id,
                "conflict_ticket_id": conflict_ticket_id,
                "role_tokens": role_tokens,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    import uvicorn

    app = create_app(settings=settings, engine=engine)
    # Inject the resolve engine over the same FakeBackend that holds the seeded
    # conflict's committed head, so POST /v1/conflicts/{id}/resolve CASes for real
    # without a live backend (E20-T5; the Supabase build is the live path).
    app.state.conflict_engine_factory = lambda **_kw: SyncEngine(
        engine, backend, actor_id=owner_id, workspace_id=workspace_id
    )
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
