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
