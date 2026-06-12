"""Supabase sync endpoints: the MOD-04 backend port over PostgREST (E24-T4).

The backend does the minimum (architecture §2): validate, store, assign a
commit order. In v0.0.5 that needs no server code at all — the endpoints are
Supabase's own PostgREST data API over the ``sync_events`` table
(``supabase/migrations/0002_sync_events.sql``), whose identity column assigns
the strictly monotonic revision that last-writer-wins folds by (D-05).

``SupabaseSyncBackend`` implements ``kantaq_sync_engine.events.BackendPort``
— the same contract MOD-30's FakeBackend pins — using PostgREST's documented
dialect (the golden-rule standard here; see ``docs/stack.md``):

- **push** — bulk insert with ``?on_conflict=actor_id,actor_seq`` and
  ``Prefer: resolution=ignore-duplicates, return=representation``: Postgres
  runs ``INSERT .. ON CONFLICT DO NOTHING RETURNING ..``, so duplicates are
  silently dropped (idempotent re-push, NFR-E04-2) and exactly the newly
  committed rows come back, in submission order, carrying their revisions.
- **pull** — ``?revision=gt.{cursor}&order=revision.asc``, paged by
  ``limit`` so Supabase's max-rows cap can never silently truncate a batch.
- **snapshot** — the engine's own ``fold_events`` over a full pull (one fold,
  one truth with FakeBackend).

Security posture (SEC task, mirrors ``auth.py``):

- constructed with the **anon key only** — a service-role key refuses at
  construction (``assert_client_safe_key``, NFR-E24-1);
- the member's user JWT comes from an injected callable (the session is owned
  by the caller); one 401 triggers one ``refresh`` callback and one retry;
- no exception or repr ever carries the api key or a token — errors surface
  only the backend's own message text (test-pinned);
- what a member can read or write is decided by RLS on ``sync_events``
  (``supabase/policies/0002_sync_rls.sql``), not by this client.

Known v0.0.5 limit (accepted; closes with the v0.2 atomic RPC, D-09): identity
revisions are assigned at INSERT but become visible at COMMIT, so under
concurrent pushes a reader can observe revision N+1 before an in-flight N.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from kantaq_backend_supabase.keys import assert_client_safe_key
from kantaq_sync_engine.events import CommittedEvent, Event, Op, fold_events

_TIMEOUT = 10.0

# One page of events per request: well under Supabase's default max-rows
# (1000), so a pull batch is never silently truncated mid-page.
PAGE_SIZE = 500

SYNC_TABLE = "sync_events"

# The wire-object columns push writes; revision/committed_at are the backend's.
_EVENT_COLUMNS = (
    "event_id",
    "collection",
    "entity_id",
    "actor_id",
    "actor_seq",
    "op",
    "base_rev",
    "policy_ref",
    "payload",
    "sig",
    "workspace_id",
)


class SyncBackendError(Exception):
    """A sync endpoint call failed; carries the backend's message and status."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _error_message(response: httpx.Response) -> str:
    """The backend's error text, without echoing anything secret."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        for key in ("message", "msg", "hint", "details", "code"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return f"HTTP {response.status_code}"


@dataclass(frozen=True)
class SyncMember:
    """A members-mirror row, as the sync CLI resolves the acting member."""

    id: str
    workspace_id: str
    email: str
    role: str
    status: str


def lookup_active_members(
    url: str,
    anon_key: str,
    access_token: str,
    *,
    client: httpx.Client | None = None,
) -> list[SyncMember]:
    """The active members the signed-in user can see (their own workspaces).

    RLS does the scoping: ``members_select`` only returns rows of workspaces
    the JWT's email belongs to, so this is how a runtime discovers its member
    id and workspace id from nothing but a session (the v0.0.5 team manifest
    lives in the members mirror; E21's onboarding UI rides the same lookup).
    """
    active = client or httpx.Client(timeout=_TIMEOUT)
    response = active.get(
        f"{url.rstrip('/')}/rest/v1/members",
        params={"select": "id,workspace_id,email,role,status", "status": "eq.active"},
        headers={
            "apikey": assert_client_safe_key(anon_key),
            "Authorization": f"Bearer {access_token}",
        },
    )
    if response.status_code >= 400:
        raise SyncBackendError(_error_message(response), response.status_code)
    return [
        SyncMember(
            id=row["id"],
            workspace_id=row["workspace_id"],
            email=row["email"],
            role=row["role"],
            status=row["status"],
        )
        for row in response.json()
    ]


class SupabaseSyncBackend:
    """The backend port over one Supabase project, scoped to one workspace.

    ``access_token`` is called per request (the caller owns the session);
    ``refresh`` (optional) is called once on a 401 and must return a fresh
    access token — the request is retried exactly once with it.
    """

    def __init__(
        self,
        url: str,
        anon_key: str,
        *,
        workspace_id: str,
        access_token: Callable[[], str],
        refresh: Callable[[], str] | None = None,
        client: httpx.Client | None = None,
        page_size: int = PAGE_SIZE,
    ) -> None:
        self._base = url.rstrip("/") + "/rest/v1"
        self._anon_key = assert_client_safe_key(anon_key)
        self._workspace_id = workspace_id
        self._access_token = access_token
        self._refresh = refresh
        self._client = client or httpx.Client(timeout=_TIMEOUT)
        self._page_size = page_size

    # --------------------------------------------------------------- the port

    def push(self, events: Iterable[Event]) -> list[CommittedEvent]:
        """Commit new events in submission order; duplicates drop silently."""
        committed: list[CommittedEvent] = []
        batch = [self._event_to_row(event) for event in events]
        for start in range(0, len(batch), self._page_size):
            chunk = batch[start : start + self._page_size]
            response = self._request(
                "POST",
                f"/{SYNC_TABLE}",
                params={"on_conflict": "actor_id,actor_seq", "columns": ",".join(_EVENT_COLUMNS)},
                headers={"Prefer": "return=representation, resolution=ignore-duplicates"},
                json=chunk,
            )
            committed.extend(self._row_to_committed(row) for row in response.json())
        return committed

    def pull(self, collection: str | None = None, since: int = 0) -> list[CommittedEvent]:
        """Committed events with revision > ``since``, in commit order."""
        entries: list[CommittedEvent] = []
        cursor = since
        while True:
            params: dict[str, str] = {
                "select": "*",
                "workspace_id": f"eq.{self._workspace_id}",
                "revision": f"gt.{cursor}",
                "order": "revision.asc",
                "limit": str(self._page_size),
            }
            if collection is not None:
                params["collection"] = f"eq.{collection}"
            response = self._request("GET", f"/{SYNC_TABLE}", params=params)
            page = [self._row_to_committed(row) for row in response.json()]
            entries.extend(page)
            if len(page) < self._page_size:
                return entries
            cursor = page[-1].revision

    def snapshot(self, collection: str) -> dict[str, dict[str, Any]]:
        """The backend's fold of a collection (LWW by commit order)."""
        return fold_events(entry.event for entry in self.pull(collection))

    # --------------------------------------------------------------- plumbing

    def _headers(self, bearer: str) -> dict[str, str]:
        # The anon key authenticates the *project*; the user JWT authenticates
        # the member — RLS judges the JWT, never this client's say-so.
        return {"apikey": self._anon_key, "Authorization": f"Bearer {bearer}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        json: list[dict[str, Any]] | None = None,
    ) -> httpx.Response:
        def send(bearer: str) -> httpx.Response:
            return self._client.request(
                method,
                f"{self._base}{path}",
                params=params,
                headers={**self._headers(bearer), **(headers or {})},
                json=json,
            )

        response = send(self._access_token())
        if response.status_code == 401 and self._refresh is not None:
            response = send(self._refresh())
        if response.status_code >= 400:
            raise SyncBackendError(_error_message(response), response.status_code)
        return response

    def _event_to_row(self, event: Event) -> dict[str, Any]:
        return {
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
            "workspace_id": self._workspace_id,
        }

    @staticmethod
    def _row_to_committed(row: dict[str, Any]) -> CommittedEvent:
        op: Op = row["op"]
        return CommittedEvent(
            revision=int(row["revision"]),
            event=Event(
                event_id=row["event_id"],
                collection=row["collection"],
                entity_id=row["entity_id"],
                actor_id=row["actor_id"],
                actor_seq=int(row["actor_seq"]),
                op=op,
                base_rev=row.get("base_rev"),
                policy_ref=row.get("policy_ref"),
                payload=dict(row.get("payload") or {}),
                sig=row.get("sig"),
            ),
        )
