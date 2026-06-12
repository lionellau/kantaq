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
    from kantaq_db.models import Workspace
    from kantaq_db.session import get_engine, sqlite_url
    from kantaq_mcp.tools import agent_action_propose
    from kantaq_runtime.app import create_app
    from kantaq_runtime.auth import ensure_local_identity, keychain_for
    from kantaq_runtime.config import get_settings
    from kantaq_sync_engine import EventLogSink

    settings = get_settings()
    engine = get_engine(sqlite_url(settings.local_db_path))

    token = ensure_local_identity(engine, keychain_for(settings))
    if token is None:
        print("e2e server: expected a fresh database (owner already exists)", file=sys.stderr)
        return 1

    # Seed the approve flow: an Agent member proposes a status change on a
    # ticket, exactly as it would through the MCP gateway.
    with Session(engine) as session:
        identity = IdentityService(session)
        owner = identity.list_members()[0]
        agent = identity.invite(
            email="agent@e2e.local",
            role=Role.agent,
            scopes=["tickets.read", "proposals.write"],
        )
        workspace = session.exec(select(Workspace)).one()
        tracker = TrackerService(
            session, actor_id=owner.id, source="cli", sink=EventLogSink(session, owner.id)
        )
        project = tracker.create_project(workspace_id=workspace.id, name="Hero Project")
        project_id = project.id
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

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "state.json").write_text(
        json.dumps(
            {
                "base_url": f"http://127.0.0.1:{PORT}",
                "token": token,
                "ticket_id": ticket_id,
                "project_id": project_id,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    import uvicorn

    app = create_app(settings=settings, engine=engine)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
