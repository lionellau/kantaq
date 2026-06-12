"""SupabaseSyncBackend unit tests — hermetic, httpx MockTransport (E24-T4).

These pin the PostgREST dialect the adapter speaks (paths, params, Prefer
headers, paging) and the SEC posture (anon key + user JWT only, service-role
refused, no secret in any error). The live half — the same calls answered by
real Postgres + RLS — is ``test_sync_live.py``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import jwt
import pytest

from kantaq_backend_supabase.keys import ServiceRoleKeyError
from kantaq_backend_supabase.sync import (
    SupabaseSyncBackend,
    SyncBackendError,
    lookup_active_members,
)
from kantaq_sync_engine.events import Event, fold_events

URL = "https://proj.supabase.co"
ANON_KEY = "anon-key-aaaa"
JWT = "user-jwt-bbbb"

# A positively identified service-role key (same convention as test_keys).
SERVICE_KEY = jwt.encode(
    {"role": "service_role", "iss": "supabase"},
    "test-only-signing-secret-0123456789abcdef",
    algorithm="HS256",
)


def _event(seq: int, *, actor: str = "mbr_a", entity: str = "tkt_1", **payload: Any) -> Event:
    return Event(
        event_id=f"evt{seq:023d}",
        collection="tickets",
        entity_id=entity,
        actor_id=actor,
        actor_seq=seq,
        payload=payload or {"title": f"v{seq}"},
    )


def _committed_row(revision: int, event: Event) -> dict[str, Any]:
    return {
        "revision": revision,
        "event_id": event.event_id,
        "collection": event.collection,
        "entity_id": event.entity_id,
        "actor_id": event.actor_id,
        "actor_seq": event.actor_seq,
        "op": event.op,
        "base_rev": event.base_rev,
        "policy_ref": event.policy_ref,
        "payload": dict(event.payload),
        "sig": event.sig,
        "workspace_id": "ws_a",
    }


def _backend(handler: Any, **kwargs: Any) -> SupabaseSyncBackend:
    return SupabaseSyncBackend(
        URL,
        ANON_KEY,
        workspace_id="ws_a",
        access_token=lambda: JWT,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


# ------------------------------------------------------------------- push


def test_push_speaks_the_postgrest_upsert_dialect() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        rows = json.loads(request.content)
        return httpx.Response(
            201, json=[_committed_row(i + 1, _event(r["actor_seq"])) for i, r in enumerate(rows)]
        )

    events = [_event(1), _event(2)]
    committed = _backend(handler).push(events)

    request = seen[0]
    assert request.url.path == "/rest/v1/sync_events"
    assert request.url.params["on_conflict"] == "actor_id,actor_seq"
    assert "resolution=ignore-duplicates" in request.headers["Prefer"]
    assert "return=representation" in request.headers["Prefer"]
    assert request.headers["apikey"] == ANON_KEY
    assert request.headers["Authorization"] == f"Bearer {JWT}"
    body = json.loads(request.content)
    assert [row["actor_seq"] for row in body] == [1, 2]
    assert all(row["workspace_id"] == "ws_a" for row in body)
    assert [entry.revision for entry in committed] == [1, 2]
    assert committed[0].event == events[0]


def test_push_chunks_by_page_size() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(len(json.loads(request.content)))
        return httpx.Response(201, json=[])

    _backend(handler, page_size=2).push([_event(i) for i in range(1, 6)])
    assert calls == [2, 2, 1]


def test_push_returns_only_what_the_backend_committed() -> None:
    """A duplicate batch comes back empty — idempotent re-push, NFR-E04-2."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=[])

    assert _backend(handler).push([_event(1)]) == []


# ------------------------------------------------------------------- pull


def test_pull_filters_by_cursor_collection_and_workspace() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    _backend(handler).pull(collection="tickets", since=42)

    params = seen[0].url.params
    assert params["revision"] == "gt.42"
    assert params["collection"] == "eq.tickets"
    assert params["workspace_id"] == "eq.ws_a"
    assert params["order"] == "revision.asc"


def test_pull_pages_until_a_short_page() -> None:
    pages = [
        [_committed_row(1, _event(1)), _committed_row(2, _event(2))],
        [_committed_row(3, _event(3))],
    ]
    cursors: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursors.append(request.url.params["revision"])
        return httpx.Response(200, json=pages[len(cursors) - 1])

    entries = _backend(handler, page_size=2).pull()

    assert cursors == ["gt.0", "gt.2"]  # the second page resumes past page one
    assert [entry.revision for entry in entries] == [1, 2, 3]


def test_snapshot_folds_with_the_engines_own_fold() -> None:
    """One fold, one truth: snapshot ≡ fold_events over the pulled stream."""
    events = [_event(1, title="old"), _event(2, title="new"), _event(3, entity="tkt_2", title="x")]
    rows = [_committed_row(i + 1, event) for i, event in enumerate(events)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)

    snapshot = _backend(handler).snapshot("tickets")

    assert snapshot == fold_events(events)
    assert snapshot["tkt_1"]["title"] == "new"  # LWW by commit order


# ----------------------------------------------------------------- session


def test_a_401_refreshes_once_and_retries() -> None:
    bearers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bearers.append(request.headers["Authorization"])
        if len(bearers) == 1:
            return httpx.Response(401, json={"message": "JWT expired"})
        return httpx.Response(200, json=[])

    backend = SupabaseSyncBackend(
        URL,
        ANON_KEY,
        workspace_id="ws_a",
        access_token=lambda: "stale-jwt",
        refresh=lambda: "fresh-jwt",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert backend.pull() == []
    assert bearers == ["Bearer stale-jwt", "Bearer fresh-jwt"]


def test_a_401_without_a_refresh_hook_surfaces() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "JWT expired"})

    with pytest.raises(SyncBackendError) as excinfo:
        _backend(handler).pull()
    assert excinfo.value.status_code == 401


# ---------------------------------------------------------------- security


def test_service_role_key_is_refused_at_construction() -> None:
    """NFR-E24-1, re-asserted at the sync endpoints."""
    with pytest.raises(ServiceRoleKeyError):
        SupabaseSyncBackend(URL, SERVICE_KEY, workspace_id="ws_a", access_token=lambda: JWT)


def test_errors_carry_the_backend_message_and_no_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": "42501", "message": "permission denied"})

    with pytest.raises(SyncBackendError) as excinfo:
        _backend(handler).push([_event(1)])

    assert excinfo.value.status_code == 403
    blob = repr(excinfo.value) + str(excinfo.value) + repr(excinfo.value.args)
    assert "permission denied" in blob
    assert ANON_KEY not in blob and JWT not in blob


def test_non_json_errors_fall_back_to_the_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(SyncBackendError) as excinfo:
        _backend(handler).pull()
    assert "502" in str(excinfo.value)


# ----------------------------------------------------------------- lookup


def test_lookup_active_members_pins_the_query_and_parses_rows() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "id": "mbr_a",
                    "workspace_id": "ws_a",
                    "email": "a@team.dev",
                    "role": "Owner",
                    "status": "active",
                }
            ],
        )

    members = lookup_active_members(
        URL, ANON_KEY, JWT, client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    request = seen[0]
    assert request.url.path == "/rest/v1/members"
    assert request.url.params["status"] == "eq.active"
    assert request.headers["apikey"] == ANON_KEY
    assert request.headers["Authorization"] == f"Bearer {JWT}"
    assert members[0].id == "mbr_a" and members[0].workspace_id == "ws_a"


def test_lookup_refuses_a_service_role_key_and_surfaces_errors() -> None:
    with pytest.raises(ServiceRoleKeyError):
        lookup_active_members(URL, SERVICE_KEY, JWT)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "bad jwt"})

    with pytest.raises(SyncBackendError):
        lookup_active_members(
            URL, ANON_KEY, JWT, client=httpx.Client(transport=httpx.MockTransport(handler))
        )
