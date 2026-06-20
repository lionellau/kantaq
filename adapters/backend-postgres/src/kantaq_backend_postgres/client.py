"""``SyncServerBackend`` — the runtime's ``BackendPort`` over the sync-server.

When ``HUB_MODE=postgres`` the local runtime talks to the self-hosted sync-server
over HTTP, exactly as it talks to Supabase via ``SupabaseSyncBackend`` — same
``BackendPort`` contract, so the sync engine, the ``VerifyingBackend`` wrapper,
and the offline outbox are all unchanged; only the wire target differs. A Bearer
**member token** authenticates the caller (the same token the gateway/CLI mints);
the server binds the acting member and runs the shared validators.

The wire shape mirrors the sync-server's endpoints (``app.py``):
``POST /v1/session`` · ``POST /v1/events`` · ``GET /v1/events`` ·
``GET /v1/snapshot`` · ``POST /v1/acks`` · ``GET /v1/acks/watermark``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

import httpx

from kantaq_backend_postgres.commit import to_commit_result
from kantaq_sync_engine.events import (
    CommitResult,
    CommittedEvent,
    Event,
    Op,
    RebaseRequired,
    SessionInit,
    fold_events,
)

_TIMEOUT = 10.0
PAGE_SIZE = 500


class SyncBackendError(Exception):
    """A sync-server call failed; carries the server's message and status."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("reason") or detail.get("code") or detail)
    if isinstance(detail, str):
        return detail
    return f"HTTP {response.status_code}"


class SyncServerBackend:
    """The MOD-04 backend port over one self-hosted sync-server, one workspace.

    The server derives the workspace from the authenticated member, so unlike
    ``SupabaseSyncBackend`` no ``workspace_id`` need be passed; events carry it
    for wire-compat but the server does not trust it.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        workspace_id: str | None = None,
        client: httpx.Client | None = None,
        page_size: int = PAGE_SIZE,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._workspace_id = workspace_id
        self._client = client or httpx.Client(timeout=_TIMEOUT)
        self._page_size = page_size

    # --------------------------------------------------------------- the port

    def session_init(self, *, sync_version: int, schema_version: int) -> SessionInit:
        body = self._post(
            "/v1/session", {"sync_version": sync_version, "schema_version": schema_version}
        )
        return SessionInit(int(body["sync_version"]), int(body["schema_version"]))

    def commit_events(
        self, events: Iterable[Event], *, require_signature: bool = True, cas: bool = False
    ) -> list[CommitResult]:
        batch = list(events)
        try:
            rows = self._post(
                "/v1/events",
                {
                    "events": [self._event_to_wire(e) for e in batch],
                    "require_signature": require_signature,
                    "cas": cas,
                },
            )
        except SyncBackendError as exc:
            if cas and exc.status_code == 409 and "rebase_required" in str(exc).lower():
                offending = next((e for e in batch if e.op == "patch"), batch[0])
                raise RebaseRequired(offending) from exc
            raise
        return [to_commit_result(row) for row in rows]

    def push(self, events: Iterable[Event]) -> list[CommittedEvent]:
        """Raw transport (pre-cutover / convergence): commit unsigned, no grant.

        Routed through the events endpoint with ``require_signature=False`` so the
        server tolerates unsigned history; committed rows map back to
        ``CommittedEvent`` (duplicates, which carry no new revision meaning, are
        skipped — the dedup floor held)."""
        batch = list(events)
        rows = self._post(
            "/v1/events",
            {"events": [self._event_to_wire(e) for e in batch], "require_signature": False},
        )
        by_id = {e.event_id: e for e in batch}
        return [
            CommittedEvent(revision=int(row["revision"]), event=by_id[row["event_id"]])
            for row in rows
            if row["status"] == "committed"
        ]

    def pull(self, collection: str | None = None, since: int = 0) -> list[CommittedEvent]:
        entries: list[CommittedEvent] = []
        cursor = since
        while True:
            params: dict[str, str] = {"since": str(cursor)}
            if collection is not None:
                params["collection"] = collection
            page_rows = self._get("/v1/events", params)
            page = [self._wire_to_committed(row) for row in page_rows]
            entries.extend(page)
            if len(page) < self._page_size:
                return entries
            cursor = page[-1].revision

    def snapshot(self, collection: str) -> dict[str, dict[str, Any]]:
        return fold_events(entry.event for entry in self.pull(collection))

    # ------------------------------------------------------------- ack watermark

    def update_ack_watermark(
        self, *, member_id: str, replica_id: str, acked_rev: int, now: datetime | None = None
    ) -> None:
        del now  # the server timestamps the upsert
        self._post(
            "/v1/acks",
            {"member_id": member_id, "replica_id": replica_id, "acked_rev": acked_rev},
        )

    def safe_watermark_rev(self, *, ttl_days: int = 30, now: datetime | None = None) -> int | None:
        del now
        body = self._get("/v1/acks/watermark", {"ttl_days": str(ttl_days)})
        value = body.get("safe_watermark_rev") if isinstance(body, dict) else None
        return int(value) if value is not None else None

    # --------------------------------------------------------------- plumbing

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _post(self, path: str, json: Any) -> Any:
        response = self._client.post(f"{self._base}{path}", headers=self._headers(), json=json)
        if response.status_code >= 400:
            raise SyncBackendError(_detail(response), response.status_code)
        return response.json()

    def _get(self, path: str, params: dict[str, str]) -> Any:
        response = self._client.get(f"{self._base}{path}", headers=self._headers(), params=params)
        if response.status_code >= 400:
            raise SyncBackendError(_detail(response), response.status_code)
        return response.json()

    def _event_to_wire(self, event: Event) -> dict[str, Any]:
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
    def _wire_to_committed(row: dict[str, Any]) -> CommittedEvent:
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
                base_rev=int(row["base_rev"]) if row.get("base_rev") is not None else None,
                policy_ref=row.get("policy_ref"),
                payload=dict(row.get("payload") or {}),
                sig=row.get("sig"),
            ),
        )
