"""Conflicts API over HTTP (E20-T5, MOD-12 / MOD-26 §B4).

Pins the Inbox conflict-tab contract: list open records, resolve by picking a
side, and the not-applied (``rebase_required``) outcome surfaces. The CAS
resolution *logic* is proven in ``test_conflict_resolve.py`` (engine +
FakeBackend) and end-to-end in the Playwright e2e; here we pin the HTTP surface
+ its auth and error mapping, using an injected engine factory.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core.identity import IdentityService, Role, TokenVerifier
from kantaq_db.models import ConflictRecord, Project, Ticket, Workspace
from kantaq_runtime.app import create_app
from kantaq_runtime.config import HubMode, Settings
from kantaq_sync_engine import ResolveResult
from kantaq_test_harness.clock import FakeClock

WS = "ws" + "0" * 24


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    with Session(temp_sqlite) as session:
        session.add(Workspace(id=WS, name="Acme"))
        session.add(Project(id="prj1", workspace_id=WS, name="P"))
        session.add(Ticket(id="tkt1", project_id="prj1", title="T", status="todo"))
        session.commit()
    return temp_sqlite


@pytest.fixture
def app(engine: Engine, tmp_path: Path) -> FastAPI:
    verifier = TokenVerifier(engine, now=FakeClock().monotonic)
    # Pin local mode so the no-backend 409 path is deterministic regardless of .env.
    settings = Settings(
        local_db_path=str(tmp_path / "data" / "local.sqlite"), hub_mode=HubMode.local
    )
    return create_app(settings=settings, engine=engine, verifier=verifier)


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
def viewer_token(engine: Engine, owner_token: str) -> str:
    with Session(engine) as session:
        return IdentityService(session).invite(email="v@x.com", role=Role.viewer).plaintext


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_conflict(engine: Engine, *, status: str = "open", cid: str = "cr1") -> str:
    with Session(engine) as session:
        session.add(
            ConflictRecord(
                id=cid,
                workspace_id=WS,
                collection="tickets",
                entity_id="tkt1",
                field="status",
                contending_revisions=[1, 2],
                candidate_values={"keep_a": "doing", "keep_b": "todo"},
                base_rev=1,
                head_rev=2,
                actor="mbr_loser",
                status=status,
            )
        )
        session.commit()
    return cid


class _StubEngine:
    """Stands in for the backend-backed SyncEngine (the CAS path is tested in
    test_conflict_resolve.py). Records the call and returns a canned outcome."""

    def __init__(self, outcome: ResolveResult) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str, object, str | None]] = []

    def resolve_conflict(
        self,
        conflict_id: str,
        choice: str,
        *,
        new_value: object = None,
        resolved_by: str | None = None,
    ) -> ResolveResult:
        self.calls.append((conflict_id, choice, new_value, resolved_by))
        return self.outcome


def _inject(app: FastAPI, outcome: ResolveResult) -> _StubEngine:
    stub = _StubEngine(outcome)
    app.state.conflict_engine_factory = lambda **_kw: stub
    return stub


def test_list_returns_open_conflicts(client: TestClient, engine: Engine, owner_token: str) -> None:
    _seed_conflict(engine, status="open", cid="cr_open")
    _seed_conflict(engine, status="resolved", cid="cr_done")
    rows = client.get("/v1/conflicts", headers=_bearer(owner_token))
    assert rows.status_code == 200, rows.text
    ids = [r["id"] for r in rows.json()]
    assert ids == ["cr_open"]  # default status=open
    body = rows.json()[0]
    assert body["candidate_values"] == {"keep_a": "doing", "keep_b": "todo"}
    assert body["field"] == "status" and body["head_rev"] == 2


def test_list_resolved_filter(client: TestClient, engine: Engine, owner_token: str) -> None:
    _seed_conflict(engine, status="resolved", cid="cr_done")
    rows = client.get("/v1/conflicts?status=resolved", headers=_bearer(owner_token))
    assert [r["id"] for r in rows.json()] == ["cr_done"]


def test_resolve_keep_a_dispatches_to_engine(
    client: TestClient, engine: Engine, app: FastAPI, owner_token: str
) -> None:
    _seed_conflict(engine, cid="cr1")
    stub = _inject(app, ResolveResult("cr1", resolved=True, rebase_required=False))
    resp = client.post(
        "/v1/conflicts/cr1/resolve", json={"choice": "keep-A"}, headers=_bearer(owner_token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"conflict_id": "cr1", "resolved": True, "rebase_required": False}
    # The resolver is attributed, the choice is passed through.
    assert stub.calls[0][1] == "keep-A"
    assert stub.calls[0][3] is not None  # resolved_by = the caller's member id


def test_resolve_rebase_required_is_surfaced(
    client: TestClient, engine: Engine, app: FastAPI, owner_token: str
) -> None:
    _seed_conflict(engine, cid="cr1")
    _inject(app, ResolveResult("cr1", resolved=False, rebase_required=True))
    resp = client.post(
        "/v1/conflicts/cr1/resolve", json={"choice": "keep-B"}, headers=_bearer(owner_token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rebase_required"] is True
    assert resp.json()["resolved"] is False


def test_resolve_unknown_conflict_is_404(
    client: TestClient, app: FastAPI, owner_token: str
) -> None:
    _inject(app, ResolveResult("x", resolved=True, rebase_required=False))
    resp = client.post(
        "/v1/conflicts/nope/resolve", json={"choice": "keep-A"}, headers=_bearer(owner_token)
    )
    assert resp.status_code == 404


def test_resolve_bad_choice_is_422(
    client: TestClient, engine: Engine, app: FastAPI, owner_token: str
) -> None:
    _seed_conflict(engine, cid="cr1")
    _inject(app, ResolveResult("cr1", resolved=True, rebase_required=False))
    resp = client.post(
        "/v1/conflicts/cr1/resolve", json={"choice": "keep-Z"}, headers=_bearer(owner_token)
    )
    assert resp.status_code == 422


def test_resolve_without_backend_is_409(
    client: TestClient, engine: Engine, owner_token: str
) -> None:
    _seed_conflict(engine, cid="cr1")  # no factory injected, local hub_mode → no engine
    resp = client.post(
        "/v1/conflicts/cr1/resolve", json={"choice": "keep-A"}, headers=_bearer(owner_token)
    )
    assert resp.status_code == 409
    assert "sync" in resp.json()["detail"].lower()


def test_resolve_needs_tickets_write(
    client: TestClient, engine: Engine, app: FastAPI, viewer_token: str
) -> None:
    _seed_conflict(engine, cid="cr1")
    _inject(app, ResolveResult("cr1", resolved=True, rebase_required=False))
    resp = client.post(
        "/v1/conflicts/cr1/resolve", json={"choice": "keep-A"}, headers=_bearer(viewer_token)
    )
    assert resp.status_code == 403  # a Viewer (and an Agent) can never resolve


def test_list_needs_a_token(client: TestClient) -> None:
    assert client.get("/v1/conflicts").status_code == 401
