"""Tracker domain service (MOD-03 / E12): projects, tickets, comments, activity.

The one write path for tracker state. Every mutation here does four things in
one transaction, in this order:

1. validate against the domain rules (fail before anything is touched),
2. apply the optimistic local write (the local replica is the source of truth
   the user sees immediately — architecture §8),
3. write an audit row through ``kantaq_core.audit`` (MOD-07) attributed to the
   acting member — for ticket-scoped writes that row *is* the activity entry
   (``object_ref="tickets/<id>"``), so a status change writes an activity
   event by construction,
4. emit a ``DomainEvent`` to the sink (MOD-04's event log once E04 lands), the
   same payload a fold would need to reproduce the row.

Handlers never write tables directly; they call this service (MOD-03 rule).
Timestamps are injectable (``now=``) so tests drive them with FakeClock.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlmodel import Session, select

from kantaq_core import audit, lifecycle
from kantaq_core.tracker.blobs import AttachmentRef
from kantaq_core.tracker.events import DomainEvent, EventSink, Op
from kantaq_db.models import AuditEvent, Comment, Member, Project, Ticket, Workspace

# Ticket workflow status (architecture facts: todo/doing/done + lifecycle stage).
TICKET_STATUSES: tuple[str, ...] = ("todo", "doing", "done")
TICKET_PRIORITIES: tuple[str, ...] = ("low", "medium", "high", "urgent")
PROJECT_STATUSES: tuple[str, ...] = ("active", "paused", "done")

_TITLE_MAX = 500
_LABEL_MAX = 64
_BODY_MAX = 100_000

# Ticket fields a PATCH may change (everything else is set at create or by
# dedicated flows like attachments).
_TICKET_PATCHABLE = frozenset(
    {
        "title",
        "description",
        "status",
        "priority",
        "labels",
        "assignee",
        "due_date",
        "acceptance_criteria",
        "lifecycle_stage",
        "parent_id",
    }
)
_PROJECT_PATCHABLE = frozenset({"name", "goal", "scope", "owner", "target_date", "status"})


# Rows _apply_patch can update in place (audit + emit share the same flow).
_TRow = TypeVar("_TRow", "Project", "Ticket")


def _default_now() -> datetime:
    return datetime.now(UTC)


def _naive_utc(ts: datetime) -> datetime:
    """UTC wall time without tzinfo — the store's (and so the fold's) encoding."""
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(UTC).replace(tzinfo=None)


class TrackerError(Exception):
    """Base class for tracker domain errors."""


class TrackerValidationError(TrackerError):
    """The request was understood but violates a domain rule (HTTP 422)."""


class TrackerNotFoundError(TrackerError):
    def __init__(self, collection: str, entity_id: str) -> None:
        super().__init__(f"no such {collection.rstrip('s')}: {entity_id}")
        self.collection = collection
        self.entity_id = entity_id


class TrackerService:
    """Tracker CRUD bound to one acting member and one session."""

    def __init__(
        self,
        session: Session,
        *,
        actor_id: str,
        source: str = "app",
        sink: EventSink | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._actor_id = actor_id
        self._source = source
        self._sink = sink
        self._raw_now: Callable[[], datetime] = now or _default_now

    def _now(self) -> datetime:
        """Normalize to naive UTC — the encoding the store actually holds
        (SQLite drops tzinfo on write). One encoding everywhere means the
        event stream, audit rows, and row snapshots fold byte-identically."""
        return _naive_utc(self._raw_now())

    # ------------------------------------------------------------------ events

    def _emit(self, collection: str, entity_id: str, op: Op, payload: dict[str, Any]) -> None:
        """Hand a JSON-safe payload to the sink.

        Payloads are always built from ``audit.snapshot`` output so the event
        stream and the audit trail use one encoding — the fold of emitted
        events must reproduce a row snapshot byte-for-byte.
        """
        if self._sink is not None:
            self._sink.emit(
                DomainEvent(collection=collection, entity_id=entity_id, op=op, payload=payload)
            )

    # ---------------------------------------------------------------- projects

    def create_project(
        self,
        *,
        workspace_id: str,
        name: str,
        goal: str = "",
        scope: str = "",
        owner: str | None = None,
        target_date: datetime | None = None,
        status: str = "active",
    ) -> Project:
        name = name.strip()
        if not name:
            raise TrackerValidationError("a project needs a non-empty name")
        if len(name) > _TITLE_MAX:
            raise TrackerValidationError(f"project name exceeds {_TITLE_MAX} characters")
        if status not in PROJECT_STATUSES:
            raise TrackerValidationError(f"unknown project status {status!r}")
        if self._session.get(Workspace, workspace_id) is None:
            raise TrackerNotFoundError("workspaces", workspace_id)
        if owner is not None:
            self._require_member(owner)

        ts = self._now()
        project = Project(
            workspace_id=workspace_id,
            name=name,
            goal=goal,
            scope=scope,
            owner=owner,
            target_date=target_date,
            status=status,
            created_at=ts,
            updated_at=ts,
        )
        self._session.add(project)
        self._session.flush()
        audit.write(
            self._session,
            actor_id=self._actor_id,
            action="project.create",
            source=self._source,
            object_ref=f"projects/{project.id}",
            after=audit.snapshot(project),
            now=ts,
        )
        self._emit("projects", project.id, "patch", audit.snapshot(project))
        self._session.commit()
        self._session.refresh(project)
        return project

    def update_project(self, project_id: str, changes: dict[str, Any]) -> Project:
        project = self.get_project(project_id)
        unknown = set(changes) - _PROJECT_PATCHABLE
        if unknown:
            raise TrackerValidationError(f"unknown project fields: {sorted(unknown)}")
        if "name" in changes:
            changes["name"] = str(changes["name"]).strip()
            if not changes["name"]:
                raise TrackerValidationError("a project needs a non-empty name")
        if "status" in changes and changes["status"] not in PROJECT_STATUSES:
            raise TrackerValidationError(f"unknown project status {changes['status']!r}")
        if changes.get("owner") is not None:
            self._require_member(changes["owner"])
        return self._apply_patch(project, changes, collection="projects", action="project.update")

    def get_project(self, project_id: str) -> Project:
        project = self._session.get(Project, project_id)
        if project is None:
            raise TrackerNotFoundError("projects", project_id)
        return project

    def list_projects(self, *, workspace_id: str | None = None) -> list[Project]:
        statement = select(Project)
        if workspace_id is not None:
            statement = statement.where(Project.workspace_id == workspace_id)
        rows = self._session.exec(statement).all()
        return sorted(rows, key=lambda p: p.id, reverse=True)

    # ----------------------------------------------------------------- tickets

    def create_ticket(
        self,
        *,
        project_id: str,
        title: str,
        description: str = "",
        status: str = "todo",
        priority: str = "medium",
        labels: list[str] | None = None,
        assignee: str | None = None,
        due_date: datetime | None = None,
        acceptance_criteria: str = "",
        lifecycle_stage: str = "intake",
        parent_id: str | None = None,
    ) -> Ticket:
        self.get_project(project_id)  # 404 before validation: the path is wrong
        fields = self._validated_ticket_fields(
            {
                "title": title,
                "description": description,
                "status": status,
                "priority": priority,
                "labels": list(labels) if labels is not None else [],
                "assignee": assignee,
                "due_date": due_date,
                "acceptance_criteria": acceptance_criteria,
                "lifecycle_stage": lifecycle_stage,
                "parent_id": parent_id,
            },
            project_id=project_id,
            ticket_id=None,
        )

        ts = self._now()
        ticket = Ticket(
            project_id=project_id,
            created_by=self._actor_id,
            created_at=ts,
            updated_at=ts,
            **fields,
        )
        self._session.add(ticket)
        self._session.flush()
        audit.write(
            self._session,
            actor_id=self._actor_id,
            action="ticket.create",
            source=self._source,
            object_ref=f"tickets/{ticket.id}",
            after=audit.snapshot(ticket),
            now=ts,
        )
        self._emit("tickets", ticket.id, "patch", audit.snapshot(ticket))
        self._session.commit()
        self._session.refresh(ticket)
        return ticket

    def update_ticket(self, ticket_id: str, changes: dict[str, Any]) -> Ticket:
        ticket = self.get_ticket(ticket_id)
        unknown = set(changes) - _TICKET_PATCHABLE
        if unknown:
            raise TrackerValidationError(f"unknown ticket fields: {sorted(unknown)}")
        validated = self._validated_ticket_fields(
            changes, project_id=ticket.project_id, ticket_id=ticket.id
        )
        return self._apply_patch(ticket, validated, collection="tickets", action="ticket.update")

    def get_ticket(self, ticket_id: str) -> Ticket:
        ticket = self._session.get(Ticket, ticket_id)
        if ticket is None:
            raise TrackerNotFoundError("tickets", ticket_id)
        return ticket

    def list_tickets(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        assignee: str | None = None,
        label: str | None = None,
        stage: str | None = None,
    ) -> list[Ticket]:
        statement = select(Ticket)
        if project_id is not None:
            statement = statement.where(Ticket.project_id == project_id)
        if status is not None:
            statement = statement.where(Ticket.status == status)
        if assignee is not None:
            statement = statement.where(Ticket.assignee == assignee)
        if stage is not None:
            statement = statement.where(Ticket.lifecycle_stage == stage)
        rows = list(self._session.exec(statement).all())
        if label is not None:
            # Generic JSON columns have no portable "list contains" in SQL;
            # filter the (already project/status-narrowed) rows in Python.
            rows = [t for t in rows if label in t.labels]
        return sorted(rows, key=lambda t: t.id, reverse=True)

    # ----------------------------------------------------------- lifecycle (MOD-20)

    def recommended_next(self, ticket: Ticket) -> list[str]:
        """The recommended next stages for the ticket (FR-E14-2).

        The rule itself is ``kantaq_core.lifecycle.recommend_next``; this glue
        supplies the relations input (open sub-tickets, v0.1's parent/sub
        relation). A legacy row whose stage predates the locked taxonomy
        recommends nothing rather than erroring a list endpoint — the strict
        check lives at the write path.
        """
        if not lifecycle.is_stage(ticket.lifecycle_stage):
            return []
        return list(
            lifecycle.recommend_next(
                ticket.lifecycle_stage,
                has_open_subtickets=self._has_open_subtickets(ticket.id),
            )
        )

    def _has_open_subtickets(self, ticket_id: str) -> bool:
        statement = select(Ticket).where(Ticket.parent_id == ticket_id, Ticket.status != "done")
        return self._session.exec(statement).first() is not None

    # ---------------------------------------------------------------- comments

    def add_comment(self, ticket_id: str, body: str) -> Comment:
        ticket = self.get_ticket(ticket_id)
        body = body.strip()
        if not body:
            raise TrackerValidationError("a comment needs a non-empty body")
        if len(body) > _BODY_MAX:
            raise TrackerValidationError(f"comment body exceeds {_BODY_MAX} characters")

        ts = self._now()
        comment = Comment(
            ticket_id=ticket.id,
            author_actor_id=self._actor_id,
            body=body,
            created_at=ts,
            updated_at=ts,
        )
        self._session.add(comment)
        self._session.flush()
        # The activity entry lands on the *ticket* so one query feeds the feed;
        # the comment row itself is identified inside ``after``.
        audit.write(
            self._session,
            actor_id=self._actor_id,
            action="comment.create",
            source=self._source,
            object_ref=f"tickets/{ticket.id}",
            after=audit.snapshot(comment),
            now=ts,
        )
        # Comments are append-only (merge_policy append_only): emitted as one
        # "append" creating the row, never patched afterwards.
        self._emit("comments", comment.id, "append", audit.snapshot(comment))
        self._session.commit()
        self._session.refresh(comment)
        return comment

    def list_comments(self, ticket_id: str) -> list[Comment]:
        self.get_ticket(ticket_id)
        statement = select(Comment).where(Comment.ticket_id == ticket_id)
        return sorted(self._session.exec(statement).all(), key=lambda c: c.id)

    # ---------------------------------------------------------------- activity

    def activity(self, ticket_id: str) -> list[AuditEvent]:
        """The ticket's activity feed: its audit rows, oldest first (FR-E12-2)."""
        self.get_ticket(ticket_id)
        statement = select(AuditEvent).where(AuditEvent.object_ref == f"tickets/{ticket_id}")
        return sorted(self._session.exec(statement).all(), key=lambda a: (a.created_at, a.id))

    # ------------------------------------------------------------- attachments

    def add_attachment(self, ticket_id: str, ref: AttachmentRef) -> Ticket:
        """Record an already-stored blob on the ticket (E12-T2).

        The bytes are in the blob store (untrusted, opaque); the ticket only
        carries the ref. Re-attaching the same blob is a no-op rather than a
        duplicate entry.
        """
        ticket = self.get_ticket(ticket_id)
        if any(a.get("blob_id") == ref.blob_id for a in ticket.attachments):
            return ticket
        attachments = [*ticket.attachments, ref.to_json()]
        return self._apply_patch(
            ticket,
            {"attachments": attachments},
            collection="tickets",
            action="ticket.attach",
        )

    # ----------------------------------------------------------------- helpers

    def _apply_patch(
        self,
        row: _TRow,
        changes: dict[str, Any],
        *,
        collection: str,
        action: str,
    ) -> _TRow:
        """Validated field changes → row update + audit + event, one commit."""
        before = audit.snapshot(row)
        ts = self._now()
        for fieldname, value in changes.items():
            setattr(row, fieldname, value)
        row.updated_at = ts
        self._session.add(row)
        self._session.flush()
        after = audit.snapshot(row)
        audit.write(
            self._session,
            actor_id=self._actor_id,
            action=action,
            source=self._source,
            object_ref=f"{collection}/{row.id}",
            before=before,
            after=after,
            now=ts,
        )
        # The event carries exactly the changed fields, in snapshot encoding.
        patch_payload = {key: after[key] for key in changes}
        patch_payload["updated_at"] = after["updated_at"]
        self._emit(collection, row.id, "patch", patch_payload)
        self._session.commit()
        self._session.refresh(row)
        return row

    def _require_member(self, member_id: str) -> Member:
        member = self._session.get(Member, member_id)
        if member is None:
            raise TrackerNotFoundError("members", member_id)
        return member

    def _validated_ticket_fields(
        self,
        fields: dict[str, Any],
        *,
        project_id: str,
        ticket_id: str | None,
    ) -> dict[str, Any]:
        out = dict(fields)
        if "title" in out:
            out["title"] = str(out["title"]).strip()
            if not out["title"]:
                raise TrackerValidationError("a ticket needs a non-empty title")
            if len(out["title"]) > _TITLE_MAX:
                raise TrackerValidationError(f"ticket title exceeds {_TITLE_MAX} characters")
        if "status" in out and out["status"] not in TICKET_STATUSES:
            raise TrackerValidationError(
                f"unknown status {out['status']!r}; expected one of {TICKET_STATUSES}"
            )
        if "priority" in out and out["priority"] not in TICKET_PRIORITIES:
            raise TrackerValidationError(
                f"unknown priority {out['priority']!r}; expected one of {TICKET_PRIORITIES}"
            )
        if "labels" in out:
            labels = out["labels"]
            if not isinstance(labels, list) or any(not isinstance(item, str) for item in labels):
                raise TrackerValidationError("labels must be a list of strings")
            cleaned: list[str] = []
            for raw in labels:
                label = raw.strip()
                if not label:
                    raise TrackerValidationError("labels must be non-empty strings")
                if len(label) > _LABEL_MAX:
                    raise TrackerValidationError(f"label {label[:20]!r}… exceeds {_LABEL_MAX}")
                if label not in cleaned:
                    cleaned.append(label)
            out["labels"] = cleaned
        if "description" in out and len(str(out["description"])) > _BODY_MAX:
            raise TrackerValidationError(f"description exceeds {_BODY_MAX} characters")
        if "lifecycle_stage" in out and not lifecycle.is_stage(str(out["lifecycle_stage"])):
            # MOD-20 locks the taxonomy in v0.1 (E14); migration 0008
            # normalized pre-taxonomy rows, so only the 9 slugs are written.
            raise TrackerValidationError(
                f"unknown lifecycle stage {out['lifecycle_stage']!r}; "
                f"expected one of {lifecycle.STAGE_SLUGS}"
            )
        if out.get("assignee") is not None:
            self._require_member(out["assignee"])
        if out.get("parent_id") is not None:
            self._validate_parent(out["parent_id"], project_id=project_id, ticket_id=ticket_id)
        return out

    def _validate_parent(self, parent_id: str, *, project_id: str, ticket_id: str | None) -> None:
        """Parent integrity: exists, same project, no self-link, no cycle."""
        if ticket_id is not None and parent_id == ticket_id:
            raise TrackerValidationError("a ticket cannot be its own parent")
        parent = self.get_ticket(parent_id)
        if parent.project_id != project_id:
            raise TrackerValidationError("parent ticket must belong to the same project")
        if ticket_id is not None:
            seen = {ticket_id}
            current: Ticket | None = parent
            while current is not None:
                if current.id in seen:
                    raise TrackerValidationError("parent link would create a cycle")
                seen.add(current.id)
                current = (
                    self._session.get(Ticket, current.parent_id)
                    if current.parent_id is not None
                    else None
                )
