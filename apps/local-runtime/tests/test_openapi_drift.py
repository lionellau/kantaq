"""Offline gate for the checked-in OpenAPI document (E18-T2, D-08).

Hermetic — regenerates the document from the app and compares byte-for-byte.
The TS half (schema.d.ts regenerated from this document) is gated in web CI.
"""

from __future__ import annotations

import json

from kantaq_runtime.openapi import read_artifact, render_spec

# The tracker + members + memory + lifecycle surface the typed client is
# generated for (MOD-03/06/19/20).
EXPECTED_PATHS = (
    "/healthz",
    "/v1/members",
    "/v1/members/invite",
    "/v1/projects",
    "/v1/projects/{project_id}",
    "/v1/lifecycle/stages",
    "/v1/tickets",
    "/v1/tickets/{ticket_id}",
    "/v1/tickets/{ticket_id}/comments",
    "/v1/tickets/{ticket_id}/activity",
    "/v1/tickets/{ticket_id}/attachments",
    "/v1/tickets/{ticket_id}/attachments/{blob_id}",
    "/v1/tickets/{ticket_id}/memory",
    "/v1/memory",
    "/v1/memory/{memory_id}",
    "/v1/memory/{memory_id}/link",
    "/v1/memory/{memory_id}/links",
    "/v1/telemetry",
    "/v1/grants",
    "/v1/grants/{grant_id}/revoke",
)


def test_checked_in_openapi_matches_the_app() -> None:
    """An API change cannot land without re-exporting the document."""
    assert read_artifact() == render_spec(), (
        "web/src/api/openapi.json is stale — regenerate with "
        "`uv run python -m kantaq_runtime.openapi`"
    )


def test_document_covers_the_v1_surface() -> None:
    paths = json.loads(read_artifact())["paths"]
    for path in EXPECTED_PATHS:
        assert path in paths, f"OpenAPI document is missing {path}"


def test_document_is_deterministically_rendered() -> None:
    """Sorted keys + stable indent: two exports are byte-identical."""
    assert render_spec() == render_spec()
    document = json.loads(render_spec())
    assert list(document) == sorted(document)
