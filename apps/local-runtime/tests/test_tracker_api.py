"""Tracker API over HTTP (E12): round-trips, filters, authz, untrusted files.

Pins the role matrix at the tracker surface (Viewer reads but cannot write,
no token is 401) and the E12-T2 rule that attachment downloads come back as
opaque save-only files.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from kantaq_core.identity import IdentityService, Role, TokenVerifier
from kantaq_runtime.app import create_app
from kantaq_runtime.config import Settings
from kantaq_test_harness.clock import FakeClock


@pytest.fixture
def engine(temp_sqlite: Engine) -> Engine:
    SQLModel.metadata.create_all(temp_sqlite)
    return temp_sqlite


@pytest.fixture
def app(engine: Engine, tmp_path: Path) -> FastAPI:
    verifier = TokenVerifier(engine, now=FakeClock().monotonic)
    settings = Settings(local_db_path=str(tmp_path / "data" / "local.sqlite"))
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
        return (
            IdentityService(session).invite(email="viewer@example.com", role=Role.viewer).plaintext
        )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_project(client: TestClient, token: str, **overrides: Any) -> dict[str, Any]:
    payload = {"name": "Proj", **overrides}
    response = client.post("/v1/projects", json=payload, headers=_bearer(token))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _create_ticket(
    client: TestClient, token: str, project_id: str, **overrides: Any
) -> dict[str, Any]:
    payload = {"project_id": project_id, "title": "A ticket", **overrides}
    response = client.post("/v1/tickets", json=payload, headers=_bearer(token))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


# -------------------------------------------------------------------- authz


def test_tracker_routes_require_a_token(client: TestClient) -> None:
    assert client.get("/v1/tickets").status_code == 401
    assert client.post("/v1/projects", json={"name": "X"}).status_code == 401


def test_viewer_reads_but_cannot_write(
    client: TestClient, owner_token: str, viewer_token: str
) -> None:
    project = _create_project(client, owner_token)
    ticket = _create_ticket(client, owner_token, project["id"])

    listed = client.get("/v1/tickets", headers=_bearer(viewer_token))
    assert listed.status_code == 200
    assert [t["id"] for t in listed.json()] == [ticket["id"]]

    denied = client.post(
        "/v1/tickets",
        json={"project_id": project["id"], "title": "nope"},
        headers=_bearer(viewer_token),
    )
    assert denied.status_code == 403

    patched = client.patch(
        f"/v1/tickets/{ticket['id']}",
        json={"status": "doing"},
        headers=_bearer(viewer_token),
    )
    assert patched.status_code == 403


# -------------------------------------------------------------- round-trips


def test_project_round_trip_and_default_workspace(client: TestClient, owner_token: str) -> None:
    # bootstrap_owner created exactly one workspace, so workspace_id is optional
    project = _create_project(client, owner_token, goal="ship it")
    fetched = client.get(f"/v1/projects/{project['id']}", headers=_bearer(owner_token))
    assert fetched.status_code == 200
    assert fetched.json()["goal"] == "ship it"

    patched = client.patch(
        f"/v1/projects/{project['id']}",
        json={"status": "paused"},
        headers=_bearer(owner_token),
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "paused"


def test_ticket_crud_filters_and_activity(client: TestClient, owner_token: str) -> None:
    project = _create_project(client, owner_token)
    ticket = _create_ticket(
        client, owner_token, project["id"], labels=["bug"], lifecycle_stage="implementation"
    )
    _create_ticket(client, owner_token, project["id"], title="Other", status="done")

    patched = client.patch(
        f"/v1/tickets/{ticket['id']}",
        json={"status": "doing", "priority": "high"},
        headers=_bearer(owner_token),
    )
    assert patched.status_code == 200, patched.text

    by_filter = client.get(
        "/v1/tickets",
        params={
            "project": project["id"],
            "status": "doing",
            "label": "bug",
            "stage": "implementation",
        },
        headers=_bearer(owner_token),
    )
    assert [t["id"] for t in by_filter.json()] == [ticket["id"]]

    comment = client.post(
        f"/v1/tickets/{ticket['id']}/comments",
        json={"body": "looks good"},
        headers=_bearer(owner_token),
    )
    assert comment.status_code == 201
    comments = client.get(
        f"/v1/tickets/{ticket['id']}/comments", headers=_bearer(owner_token)
    ).json()
    assert [c["body"] for c in comments] == ["looks good"]

    activity = client.get(
        f"/v1/tickets/{ticket['id']}/activity", headers=_bearer(owner_token)
    ).json()
    assert [a["action"] for a in activity] == [
        "ticket.create",
        "ticket.update",
        "comment.create",
    ]
    assert activity[1]["before"]["status"] == "todo"
    assert activity[1]["after"]["status"] == "doing"


def test_validation_and_missing_map_to_422_and_404(client: TestClient, owner_token: str) -> None:
    project = _create_project(client, owner_token)
    bad_status = client.post(
        "/v1/tickets",
        json={"project_id": project["id"], "title": "T", "status": "blocked"},
        headers=_bearer(owner_token),
    )
    assert bad_status.status_code == 422

    missing = client.get("/v1/tickets/tkt_nope", headers=_bearer(owner_token))
    assert missing.status_code == 404

    orphan = client.post(
        "/v1/tickets",
        json={"project_id": "prj_nope", "title": "T"},
        headers=_bearer(owner_token),
    )
    assert orphan.status_code == 404


# --------------------------------------------------------- lifecycle (MOD-20)


def test_lifecycle_stages_endpoint_serves_the_locked_taxonomy(
    client: TestClient, owner_token: str, viewer_token: str
) -> None:
    assert client.get("/v1/lifecycle/stages").status_code == 401  # no token

    response = client.get("/v1/lifecycle/stages", headers=_bearer(viewer_token))
    assert response.status_code == 200, response.text
    stages = response.json()
    assert [s["slug"] for s in stages] == [
        "intake",
        "discovery",
        "planning",
        "design",
        "implementation",
        "review",
        "qa",
        "release",
        "learn",
    ]
    assert [s["order"] for s in stages] == list(range(9))
    qa = next(s for s in stages if s["slug"] == "qa")
    assert qa["title"] == "QA"
    assert qa["containers"] == ["browser-qa", "regression-testing", "bug-triage"]
    assert all(s["purpose"] for s in stages)


def test_stage_transition_round_trip_with_activity_and_validation(
    client: TestClient, owner_token: str
) -> None:
    project = _create_project(client, owner_token)
    ticket = _create_ticket(client, owner_token, project["id"])
    assert ticket["lifecycle_stage"] == "intake"
    assert ticket["recommended_next_stages"] == ["discovery"]

    moved = client.patch(
        f"/v1/tickets/{ticket['id']}",
        json={"lifecycle_stage": "review"},  # jumps are valid: order is advisory
        headers=_bearer(owner_token),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["lifecycle_stage"] == "review"
    assert moved.json()["recommended_next_stages"] == ["qa", "implementation"]

    bogus = client.patch(
        f"/v1/tickets/{ticket['id']}",
        json={"lifecycle_stage": "build"},
        headers=_bearer(owner_token),
    )
    assert bogus.status_code == 422
    assert "unknown lifecycle stage" in bogus.json()["detail"]

    activity = client.get(
        f"/v1/tickets/{ticket['id']}/activity", headers=_bearer(owner_token)
    ).json()
    transition = activity[-1]
    assert transition["action"] == "ticket.update"
    assert transition["before"]["lifecycle_stage"] == "intake"
    assert transition["after"]["lifecycle_stage"] == "review"


def test_recommended_next_gates_release_behind_open_subtickets(
    client: TestClient, owner_token: str
) -> None:
    project = _create_project(client, owner_token)
    parent = _create_ticket(client, owner_token, project["id"], lifecycle_stage="qa")
    child = _create_ticket(
        client, owner_token, project["id"], title="Child", parent_id=parent["id"]
    )

    listed = client.get(
        "/v1/tickets", params={"project": project["id"]}, headers=_bearer(owner_token)
    ).json()
    by_id = {t["id"]: t for t in listed}
    assert by_id[parent["id"]]["recommended_next_stages"] == ["implementation"]

    done = client.patch(
        f"/v1/tickets/{child['id']}", json={"status": "done"}, headers=_bearer(owner_token)
    )
    assert done.status_code == 200
    refreshed = client.get(f"/v1/tickets/{parent['id']}", headers=_bearer(owner_token)).json()
    assert refreshed["recommended_next_stages"] == ["release", "implementation"]


# ------------------------------------------------------------- attachments


def test_attachment_upload_and_untrusted_download(client: TestClient, owner_token: str) -> None:
    project = _create_project(client, owner_token)
    ticket = _create_ticket(client, owner_token, project["id"])

    uploaded = client.post(
        f"/v1/tickets/{ticket['id']}/attachments",
        files={"file": ("../evil<script>.html", b"<script>alert(1)</script>", "text/html")},
        headers=_bearer(owner_token),
    )
    assert uploaded.status_code == 201, uploaded.text
    refs = uploaded.json()["attachments"]
    assert len(refs) == 1
    ref = refs[0]
    assert "/" not in ref["filename"] and "<" not in ref["filename"]

    download = client.get(
        f"/v1/tickets/{ticket['id']}/attachments/{ref['blob_id']}",
        headers=_bearer(owner_token),
    )
    assert download.status_code == 200
    assert download.content == b"<script>alert(1)</script>"
    # Untrusted file (PRD §15): never rendered, never sniffed, always saved.
    assert download.headers["content-type"].startswith("application/octet-stream")
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.headers["x-content-type-options"] == "nosniff"


def test_attachment_is_scoped_to_its_ticket(client: TestClient, owner_token: str) -> None:
    project = _create_project(client, owner_token)
    with_file = _create_ticket(client, owner_token, project["id"])
    without_file = _create_ticket(client, owner_token, project["id"], title="bare")

    uploaded = client.post(
        f"/v1/tickets/{with_file['id']}/attachments",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        headers=_bearer(owner_token),
    )
    blob_id = uploaded.json()["attachments"][0]["blob_id"]

    cross = client.get(
        f"/v1/tickets/{without_file['id']}/attachments/{blob_id}",
        headers=_bearer(owner_token),
    )
    assert cross.status_code == 404  # no blind blob access via another ticket


def test_attachment_activity_and_dedup(client: TestClient, owner_token: str) -> None:
    project = _create_project(client, owner_token)
    ticket = _create_ticket(client, owner_token, project["id"])
    for _ in range(2):  # same bytes twice: one ref, one stored blob
        response = client.post(
            f"/v1/tickets/{ticket['id']}/attachments",
            files={"file": ("dup.bin", b"\x00\x01", "application/octet-stream")},
            headers=_bearer(owner_token),
        )
        assert response.status_code == 201
    assert len(response.json()["attachments"]) == 1

    activity = client.get(
        f"/v1/tickets/{ticket['id']}/activity", headers=_bearer(owner_token)
    ).json()
    assert [a["action"] for a in activity] == ["ticket.create", "ticket.attach"]


# -------------------------------------------------------------- relations (E12-T3)


def _relation(client: TestClient, token: str, ticket_id: str, to_id: str, rel_type: str) -> Any:
    return client.post(
        f"/v1/tickets/{ticket_id}/relations",
        json={"to_id": to_id, "type": rel_type},
        headers=_bearer(token),
    )


def test_relation_round_trip_and_direction(client: TestClient, owner_token: str) -> None:
    project = _create_project(client, owner_token)
    a = _create_ticket(client, owner_token, project["id"], title="A")
    b = _create_ticket(client, owner_token, project["id"], title="B")

    created = _relation(client, owner_token, a["id"], b["id"], "blocking")
    assert created.status_code == 201, created.text
    rel = created.json()
    assert rel["type"] == "blocking"
    assert rel["direction"] == "outgoing"  # A is the from-end

    # The same edge reads as incoming from B's side.
    from_b = client.get(f"/v1/tickets/{b['id']}/relations", headers=_bearer(owner_token)).json()
    assert [(r["type"], r["direction"]) for r in from_b] == [("blocking", "incoming")]

    # Delete it (scoped to the ticket) — then both ends are clean.
    deleted = client.delete(
        f"/v1/tickets/{a['id']}/relations/{rel['id']}", headers=_bearer(owner_token)
    )
    assert deleted.status_code == 204
    assert client.get(f"/v1/tickets/{a['id']}/relations", headers=_bearer(owner_token)).json() == []


def test_relation_integrity_maps_to_422(client: TestClient, owner_token: str) -> None:
    project = _create_project(client, owner_token)
    a = _create_ticket(client, owner_token, project["id"], title="A")
    b = _create_ticket(client, owner_token, project["id"], title="B")

    assert _relation(client, owner_token, a["id"], a["id"], "related").status_code == 422  # self
    assert _relation(client, owner_token, a["id"], b["id"], "nope").status_code == 422  # type
    assert _relation(client, owner_token, a["id"], b["id"], "blocking").status_code == 201
    # inverse spelling of the same dependency is a duplicate (422)
    assert _relation(client, owner_token, b["id"], a["id"], "blocked-by").status_code == 422
    # missing endpoint is a 404
    assert _relation(client, owner_token, a["id"], "tkt_ghost", "related").status_code == 404


def test_delete_relation_scoped_to_the_ticket(client: TestClient, owner_token: str) -> None:
    project = _create_project(client, owner_token)
    a = _create_ticket(client, owner_token, project["id"], title="A")
    b = _create_ticket(client, owner_token, project["id"], title="B")
    c = _create_ticket(client, owner_token, project["id"], title="C")
    rel = _relation(client, owner_token, a["id"], b["id"], "related").json()

    # The relationship exists but does not touch C → 404 under C's path.
    mismatched = client.delete(
        f"/v1/tickets/{c['id']}/relations/{rel['id']}", headers=_bearer(owner_token)
    )
    assert mismatched.status_code == 404


def test_viewer_cannot_write_relations(
    client: TestClient, owner_token: str, viewer_token: str
) -> None:
    project = _create_project(client, owner_token)
    a = _create_ticket(client, owner_token, project["id"], title="A")
    b = _create_ticket(client, owner_token, project["id"], title="B")
    rel = _relation(client, owner_token, a["id"], b["id"], "related").json()

    # Viewer reads relations…
    listed = client.get(f"/v1/tickets/{a['id']}/relations", headers=_bearer(viewer_token))
    assert listed.status_code == 200 and len(listed.json()) == 1
    # …but cannot create or delete them.
    assert _relation(client, viewer_token, a["id"], b["id"], "blocking").status_code == 403
    assert (
        client.delete(
            f"/v1/tickets/{a['id']}/relations/{rel['id']}", headers=_bearer(viewer_token)
        ).status_code
        == 403
    )


def test_list_badges_subtickets_relationships_blocked(client: TestClient, owner_token: str) -> None:
    project = _create_project(client, owner_token)
    parent = _create_ticket(client, owner_token, project["id"], title="Parent")
    child = _create_ticket(
        client, owner_token, project["id"], title="Child", parent_id=parent["id"]
    )
    blocker = _create_ticket(client, owner_token, project["id"], title="Blocker")
    # blocker blocks child → child is blocked (blocker is not done)
    assert _relation(client, owner_token, blocker["id"], child["id"], "blocking").status_code == 201

    by_id = {t["id"]: t for t in client.get("/v1/tickets", headers=_bearer(owner_token)).json()}
    assert by_id[parent["id"]]["subticket_count"] == 1
    assert by_id[child["id"]]["blocked"] is True
    assert by_id[child["id"]]["relationship_count"] == 1
    assert by_id[blocker["id"]]["blocked"] is False

    # Once the blocker is done, the child is no longer blocked.
    client.patch(
        f"/v1/tickets/{blocker['id']}", json={"status": "done"}, headers=_bearer(owner_token)
    )
    refreshed = client.get(f"/v1/tickets/{child['id']}", headers=_bearer(owner_token)).json()
    assert refreshed["blocked"] is False


def test_parent_filter_returns_subtickets(client: TestClient, owner_token: str) -> None:
    project = _create_project(client, owner_token)
    parent = _create_ticket(client, owner_token, project["id"], title="Parent")
    child = _create_ticket(
        client, owner_token, project["id"], title="Child", parent_id=parent["id"]
    )
    _create_ticket(client, owner_token, project["id"], title="Unrelated")

    children = client.get(
        "/v1/tickets", params={"parent": parent["id"]}, headers=_bearer(owner_token)
    ).json()
    assert [t["id"] for t in children] == [child["id"]]


# ------------------------------------------------ recommendations (E17-T1)


def test_recommendations_return_structured_contracts(client: TestClient, owner_token: str) -> None:
    """A review-stage ticket recommends roles with the full MOD-22 contract."""
    project = _create_project(client, owner_token)
    ticket = _create_ticket(
        client, owner_token, project["id"], lifecycle_stage="review", labels=["Security"]
    )
    response = client.get(
        f"/v1/tickets/{ticket['id']}/recommendations", headers=_bearer(owner_token)
    )
    assert response.status_code == 200, response.text
    recs = response.json()
    assert recs, "a review-stage ticket should recommend at least one role/skill"
    by_container = {r["skill_container"]: r for r in recs}
    # The review stage's canonical containers are present (MOD-20).
    assert {"code-review", "security-review", "architecture-review"} <= set(by_container)
    rec = by_container["security-review"]
    # Every contract field is populated and well-formed.
    assert rec["role"] == "code_agent"
    assert rec["risk_level"] in {"low", "medium", "high"}
    assert rec["confidence"] in {"rule_match_strong", "rule_match_partial", "heuristic_only"}
    assert rec["approval_rule"] in {"propose_first", "read_only"}
    assert 'role_context_get(ticket="' in rec["mcp_session_template"]
    assert rec["required_memory"]  # the role's policy scopes


def test_recommendations_report_missing_memory_from_the_resolver(
    client: TestClient, owner_token: str
) -> None:
    """A fresh ticket has no linked memory -> every required scope is missing."""
    project = _create_project(client, owner_token)
    ticket = _create_ticket(client, owner_token, project["id"], lifecycle_stage="implementation")
    recs = client.get(
        f"/v1/tickets/{ticket['id']}/recommendations", headers=_bearer(owner_token)
    ).json()
    code_rec = next(r for r in recs if r["role"] == "code_agent")
    # code_agent reads codebase/decision/ticket/project; none is present here.
    assert set(code_rec["missing_memory"]) == {"codebase", "decision", "ticket", "project"}
    assert set(code_rec["required_memory"]) == set(code_rec["missing_memory"])


def test_recommendations_are_readable_by_a_viewer_but_need_a_token(
    client: TestClient, owner_token: str, viewer_token: str
) -> None:
    project = _create_project(client, owner_token)
    ticket = _create_ticket(client, owner_token, project["id"])
    # Read-only surface: a Viewer may read recommendations.
    ok = client.get(f"/v1/tickets/{ticket['id']}/recommendations", headers=_bearer(viewer_token))
    assert ok.status_code == 200
    # No token is rejected.
    assert client.get(f"/v1/tickets/{ticket['id']}/recommendations").status_code == 401
    # An unknown ticket is a 404.
    assert (
        client.get("/v1/tickets/tkt_nope/recommendations", headers=_bearer(owner_token)).status_code
        == 404
    )
