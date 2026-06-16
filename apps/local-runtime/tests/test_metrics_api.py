"""Metrics API over HTTP (E20-T5, MOD-27).

Pins the Settings → Sync dashboard contract: the WorkspaceMetrics shape, the
non-dollar capacity gauge, the tokens.rotate scope gate on per-actor rows, and
the Supabase billing deep-link (D-16). The estimate accuracy is gated in
test_metrics_calibration.py; here we pin the HTTP surface.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core import audit
from kantaq_core.identity import IdentityService, Role, TokenVerifier
from kantaq_db.models import Project, Ticket, Workspace
from kantaq_runtime.app import create_app
from kantaq_runtime.config import HubMode, Settings
from kantaq_test_harness.clock import FakeClock

WS = "ws" + "0" * 24


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    with Session(temp_sqlite) as session:
        session.add(Workspace(id=WS, name="Acme"))
        session.add(Project(id="prj1", workspace_id=WS, name="Alpha"))
        session.add(Ticket(id="tkt1", project_id="prj1", title="T"))
        session.commit()
    return temp_sqlite


def _make_app(engine: Engine, tmp_path: Path, settings: Settings | None = None) -> FastAPI:
    verifier = TokenVerifier(engine, now=FakeClock().monotonic)
    # Pin local mode by default so the shape test doesn't pick up a dogfood .env.
    s = settings or Settings(
        local_db_path=str(tmp_path / "data" / "local.sqlite"), hub_mode=HubMode.local
    )
    return create_app(settings=s, engine=engine, verifier=verifier)


@pytest.fixture
def app(engine: Engine, tmp_path: Path) -> FastAPI:
    return _make_app(engine, tmp_path)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def owner_token(engine: Engine) -> str:
    with Session(engine) as session:
        minted = IdentityService(session).bootstrap_owner()
    assert minted is not None
    return minted.plaintext


@pytest.fixture
def viewer(engine: Engine, owner_token: str) -> tuple[str, str]:
    with Session(engine) as session:
        minted = IdentityService(session).invite(email="v@x.com", role=Role.viewer)
    return minted.member_id, minted.plaintext


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_summary_shape_local_mode(client: TestClient, owner_token: str) -> None:
    resp = client.get("/v1/metrics/summary", headers=_bearer(owner_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hub_mode"] == "local"
    assert body["backend"] is None  # solo → no shared cost
    assert set(body["counts"]) == {
        "workspaces",
        "projects",
        "tickets",
        "comments",
        "ticket_relationships",
        "members",
        "agent_proposals",
        "memory_entries",
        "memory_links",
        "audit_events",
    }
    assert body["counts"]["tickets"] == 1
    assert body["replica"]["total_bytes"] > 0
    assert body["billing_url"] is None  # no SUPABASE_URL in local mode
    assert any("payload-size proxy" in n for n in body["notes"])


def test_capacity_gauge_and_billing_link_supabase(engine: Engine, tmp_path: Path) -> None:
    settings = Settings(
        local_db_path=str(tmp_path / "data" / "local.sqlite"),
        hub_mode=HubMode.supabase,
        supabase_url="https://abcdefgh.supabase.co",
    )
    app = _make_app(engine, tmp_path, settings)
    with Session(engine) as session:
        owner = IdentityService(session).bootstrap_owner()
    assert owner is not None
    with TestClient(app) as client:
        resp = client.get("/v1/metrics/summary", headers=_bearer(owner.plaintext))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backend"] is not None
    cap = body["backend"]["capacity"]
    assert cap["tier"] == "free"
    assert cap["db_limit_bytes"] == 500_000_000
    assert "db_pct" in cap and "headroom_warning" in cap
    assert body["billing_url"] == "https://supabase.com/dashboard/project/abcdefgh/settings/billing"


def test_scope_gate_hides_other_actors_without_tokens_rotate(
    client: TestClient, engine: Engine, owner_token: str, viewer: tuple[str, str]
) -> None:
    viewer_id, viewer_token = viewer
    # Seed agent activity for a different actor.
    with Session(engine) as session:
        audit.write(session, actor_id="agent_bot", action="proposal.create", source="mcp")
        session.commit()

    # Owner has tokens.rotate → sees every actor (incl. agent_bot).
    owner_body = client.get("/v1/metrics/summary", headers=_bearer(owner_token)).json()
    assert "agent_bot" in {a["actor_id"] for a in owner_body["agents"]["by_actor"]}
    # Totals stay regardless (an aggregate leaks no per-member detail).
    assert owner_body["agents"]["totals"]["proposes"] == 1

    # Viewer lacks tokens.rotate → sees only their own row (not agent_bot).
    viewer_body = client.get("/v1/metrics/summary", headers=_bearer(viewer_token)).json()
    actor_ids = {a["actor_id"] for a in viewer_body["agents"]["by_actor"]}
    assert "agent_bot" not in actor_ids
    assert actor_ids <= {viewer_id}


def test_summary_needs_a_token(client: TestClient) -> None:
    assert client.get("/v1/metrics/summary").status_code == 401
