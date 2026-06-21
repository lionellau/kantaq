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

from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlmodel import Session, col, select

from kantaq_core import audit, lifecycle
from kantaq_core.tracker.blobs import AttachmentRef
from kantaq_core.tracker.events import DomainEvent, EventSink, Op
from kantaq_db.models import (
    AuditEvent,
    Comment,
    FollowUp,
    Member,
    Milestone,
    Project,
    Ticket,
    TicketMilestone,
    TicketRelationship,
    Workspace,
)

# Ticket workflow status (architecture facts: todo/doing/done + lifecycle stage).
TICKET_STATUSES: tuple[str, ...] = ("todo", "doing", "done")
TICKET_PRIORITIES: tuple[str, ...] = ("low", "medium", "high", "urgent")
PROJECT_STATUSES: tuple[str, ...] = ("active", "paused", "done")
# Milestone lifecycle (MOD-20 v0.3 / FR-E14-3). Flat (not nestable); a milestone
# moves active → complete, and archived retires it from the default views.
MILESTONE_STATUSES: tuple[str, ...] = ("active", "complete", "archived")
# Follow-up lifecycle (MOD-29 v0.3 / FR-E15-1). A follow-up is queued ``open``;
# ``complete`` resolves it to ``done`` or ``dismissed`` (never re-opened — a new
# follow-up is cheaper than a status dance). v1 writes accept ``open`` only.
FOLLOW_UP_STATUSES: tuple[str, ...] = ("open", "done", "dismissed")
FOLLOW_UP_RESOLVED_STATUSES: tuple[str, ...] = ("done", "dismissed")

# Typed ticket relationships (MOD-03 v0.1 / FR-E12-3). Five types, two of them
# symmetric (``related``/``duplicate``: an edge means the same fact from either
# end) and two an inverse pair (``blocking`` ⇔ ``blocked-by``: "A blocking B" is
# the same fact as "B blocked-by A"). ``caused-by`` is directed with no spelled
# inverse in the set. Integrity is enforced over these semantics, not the raw
# (from, to, type) triple — see ``_relation_key`` / ``_relation_arc``.
RELATIONSHIP_TYPES: tuple[str, ...] = (
    "related",
    "blocked-by",
    "blocking",
    "duplicate",
    "caused-by",
)
_SYMMETRIC_TYPES: frozenset[str] = frozenset({"related", "duplicate"})
# The relation types contributing arcs to each directed dependency family. The
# cycle check loads only the family it is testing (FR-E15-2 will read the same
# graph for the dependency surface).
_FAMILY_TYPES: dict[str, tuple[str, ...]] = {
    "blocks": ("blocking", "blocked-by"),
    "causes": ("caused-by",),
}

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
_MILESTONE_PATCHABLE = frozenset({"name", "description", "target_date", "status"})
# Follow-up fields a human (or an approved proposal) may patch. ``status`` moves
# only through ``complete_follow_up`` (validated to a resolved value), not a raw
# patch, so it is deliberately absent here.
_FOLLOW_UP_PATCHABLE = frozenset({"title", "body", "due_at"})


# Rows _apply_patch can update in place (audit + emit share the same flow).
_TRow = TypeVar("_TRow", "Project", "Ticket", "Milestone", "FollowUp")


def _default_now() -> datetime:
    return datetime.now(UTC)


def _naive_utc(ts: datetime) -> datetime:
    """UTC wall time without tzinfo — the store's (and so the fold's) encoding."""
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(UTC).replace(tzinfo=None)


def _relation_key(from_id: str, to_id: str, rel_type: str) -> tuple[Any, ...]:
    """A canonical key for a relationship's *fact*, independent of spelling.

    Two requests with the same key assert the same thing, so the key is what
    the no-duplicate rule compares (a plain ``(from, to, type)`` triple would
    let ``A blocking B`` and ``B blocked-by A`` — the same dependency — both be
    stored). Symmetric types collapse the endpoint order; the inverse pair
    collapses onto one directed ``blocks`` arc; ``caused-by`` onto a ``causes``
    arc.
    """
    if rel_type in _SYMMETRIC_TYPES:
        return (rel_type, frozenset({from_id, to_id}))
    arc = _relation_arc(from_id, to_id, rel_type)
    assert arc is not None  # every non-symmetric type yields an arc
    return arc


def _relation_arc(from_id: str, to_id: str, rel_type: str) -> tuple[str, str, str] | None:
    """The directed arc a relationship contributes to its dependency graph.

    Returns ``(family, src, dst)`` meaning ``src`` precedes ``dst`` in that
    family — ``blocks`` (``src`` blocks ``dst``) or ``causes`` (``src`` causes
    ``dst``) — or ``None`` for the symmetric types, which form no order and so
    cannot create a cycle. The cycle check (``_would_cycle``) runs per family.
    """
    if rel_type == "blocking":  # from blocks to
        return ("blocks", from_id, to_id)
    if rel_type == "blocked-by":  # to blocks from
        return ("blocks", to_id, from_id)
    if rel_type == "caused-by":  # to caused (→ causes) from
        return ("causes", to_id, from_id)
    return None  # related / duplicate — symmetric, no direction


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
        parent: str | None = None,
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
        if parent is not None:
            # The sub-issue query (FR-E12-3): a parent's direct children.
            statement = statement.where(Ticket.parent_id == parent)
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

    # ------------------------------------------------------ relations (FR-E12-3)

    def add_relation(self, from_id: str, to_id: str, rel_type: str) -> TicketRelationship:
        """Create a typed edge between two tickets (FR-E12-3).

        Integrity, fail-closed before anything is written: the type is one of
        the five; no self-link; no duplicate (the symmetric or inverse spelling
        of an existing edge is the same fact); no cycle in the dependency
        families (``blocks`` from blocking/blocked-by, ``causes`` from
        caused-by). Both endpoints must be tickets in the same workspace — typed
        edges may cross projects (an app ticket blocked-by an infra ticket),
        unlike the same-project parent/sub containment. The activity row lands
        on the *from* ticket, so the edge shows in the feed of the ticket the
        user acted on.
        """
        if rel_type not in RELATIONSHIP_TYPES:
            raise TrackerValidationError(
                f"unknown relationship type {rel_type!r}; expected one of {RELATIONSHIP_TYPES}"
            )
        if from_id == to_id:
            raise TrackerValidationError("a ticket cannot relate to itself")
        source = self.get_ticket(from_id)
        target = self.get_ticket(to_id)
        self._require_same_workspace(source, target)

        key = _relation_key(from_id, to_id, rel_type)
        for existing in self._relations_touching((from_id, to_id)):
            if _relation_key(existing.from_id, existing.to_id, existing.type) == key:
                raise TrackerValidationError(
                    f"these tickets already have a {rel_type!r}-equivalent relationship"
                )

        arc = _relation_arc(from_id, to_id, rel_type)
        if arc is not None and self._would_cycle(*arc):
            family = arc[0]
            raise TrackerValidationError(
                f"a {rel_type!r} relationship here would create a {family} cycle"
            )

        ts = self._now()
        relationship = TicketRelationship(
            from_id=from_id,
            to_id=to_id,
            type=rel_type,
            created_by=self._actor_id,
            created_at=ts,
            updated_at=ts,
        )
        self._session.add(relationship)
        self._session.flush()
        audit.write(
            self._session,
            actor_id=self._actor_id,
            action="relation.create",
            source=self._source,
            object_ref=f"tickets/{from_id}",
            after=audit.snapshot(relationship),
            now=ts,
        )
        self._emit("ticket_relationships", relationship.id, "patch", audit.snapshot(relationship))
        self._session.commit()
        self._session.refresh(relationship)
        return relationship

    def remove_relation(self, relationship_id: str) -> None:
        """Delete a typed edge — the only mutation a relationship has."""
        relationship = self._session.get(TicketRelationship, relationship_id)
        if relationship is None:
            raise TrackerNotFoundError("ticket_relationships", relationship_id)
        ts = self._now()
        audit.write(
            self._session,
            actor_id=self._actor_id,
            action="relation.delete",
            source=self._source,
            object_ref=f"tickets/{relationship.from_id}",
            before=audit.snapshot(relationship),
            now=ts,
        )
        self._emit("ticket_relationships", relationship.id, "tombstone", {})
        self._session.delete(relationship)
        self._session.commit()

    def relations_for(self, ticket_id: str) -> list[TicketRelationship]:
        """Every typed relationship touching the ticket, from either end."""
        self.get_ticket(ticket_id)
        return sorted(self._relations_touching((ticket_id,)), key=lambda r: r.id)

    def _relations_touching(self, ticket_ids: tuple[str, ...]) -> list[TicketRelationship]:
        ids = set(ticket_ids)
        statement = select(TicketRelationship).where(
            col(TicketRelationship.from_id).in_(ids) | col(TicketRelationship.to_id).in_(ids)
        )
        return list(self._session.exec(statement).all())

    def _require_same_workspace(self, a: Ticket, b: Ticket) -> None:
        if self._ticket_workspace(a) != self._ticket_workspace(b):
            raise TrackerValidationError("related tickets must belong to the same workspace")

    def _ticket_workspace(self, ticket: Ticket) -> str:
        project = self._session.get(Project, ticket.project_id)
        # A ticket always has a project (FK); fall back defensively.
        return project.workspace_id if project is not None else ticket.project_id

    def _would_cycle(self, family: str, src: str, dst: str) -> bool:
        """Would adding the arc ``src → dst`` close a cycle in ``family``?

        A cycle forms iff ``dst`` already reaches ``src`` along existing arcs of
        the family (the new arc is not yet stored), so BFS the current graph
        from ``dst``. v0.1 relation counts are small; this stays in memory.
        """
        adjacency = self._family_adjacency(family)
        seen = {dst}
        queue = deque([dst])
        while queue:
            for nxt in adjacency.get(queue.popleft(), ()):
                if nxt == src:
                    return True
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return False

    def _family_adjacency(self, family: str) -> dict[str, list[str]]:
        """Adjacency list of one dependency family, from the stored edges."""
        statement = select(TicketRelationship).where(
            col(TicketRelationship.type).in_(_FAMILY_TYPES[family])
        )
        adjacency: dict[str, list[str]] = {}
        for relationship in self._session.exec(statement).all():
            arc = _relation_arc(relationship.from_id, relationship.to_id, relationship.type)
            if arc is None:  # pragma: no cover - the query restricts to arc types
                continue
            _, edge_src, edge_dst = arc
            adjacency.setdefault(edge_src, []).append(edge_dst)
        return adjacency

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

    # -------------------------------------------------------------- milestones

    def create_milestone(
        self,
        *,
        project_id: str,
        name: str,
        description: str = "",
        target_date: datetime | None = None,
        status: str = "active",
    ) -> Milestone:
        """Create a flat milestone in a project (FR-E14-3).

        Fail-closed before any write: a non-empty name within the title bound, a
        known status, and an existing project. Like a project create, the audit
        row + the lww event land together.
        """
        name = name.strip()
        if not name:
            raise TrackerValidationError("a milestone needs a non-empty name")
        if len(name) > _TITLE_MAX:
            raise TrackerValidationError(f"milestone name exceeds {_TITLE_MAX} characters")
        if status not in MILESTONE_STATUSES:
            raise TrackerValidationError(
                f"unknown milestone status {status!r}; expected one of {MILESTONE_STATUSES}"
            )
        if self._session.get(Project, project_id) is None:
            raise TrackerNotFoundError("projects", project_id)

        ts = self._now()
        milestone = Milestone(
            project_id=project_id,
            name=name,
            description=description,
            target_date=_naive_utc(target_date) if target_date is not None else None,
            status=status,
            created_by=self._actor_id,
            created_at=ts,
            updated_at=ts,
        )
        self._session.add(milestone)
        self._session.flush()
        audit.write(
            self._session,
            actor_id=self._actor_id,
            action="milestone.create",
            source=self._source,
            object_ref=f"milestones/{milestone.id}",
            after=audit.snapshot(milestone),
            now=ts,
        )
        self._emit("milestones", milestone.id, "patch", audit.snapshot(milestone))
        self._session.commit()
        self._session.refresh(milestone)
        return milestone

    def update_milestone(self, milestone_id: str, changes: dict[str, Any]) -> Milestone:
        milestone = self.get_milestone(milestone_id)
        unknown = set(changes) - _MILESTONE_PATCHABLE
        if unknown:
            raise TrackerValidationError(f"unknown milestone fields: {sorted(unknown)}")
        if "name" in changes:
            changes["name"] = str(changes["name"]).strip()
            if not changes["name"]:
                raise TrackerValidationError("a milestone needs a non-empty name")
            if len(changes["name"]) > _TITLE_MAX:
                raise TrackerValidationError(f"milestone name exceeds {_TITLE_MAX} characters")
        if "status" in changes and changes["status"] not in MILESTONE_STATUSES:
            raise TrackerValidationError(
                f"unknown milestone status {changes['status']!r}; "
                f"expected one of {MILESTONE_STATUSES}"
            )
        if changes.get("target_date") is not None:
            changes["target_date"] = _naive_utc(changes["target_date"])
        return self._apply_patch(
            milestone, changes, collection="milestones", action="milestone.update"
        )

    def get_milestone(self, milestone_id: str) -> Milestone:
        milestone = self._session.get(Milestone, milestone_id)
        if milestone is None:
            raise TrackerNotFoundError("milestones", milestone_id)
        return milestone

    def list_milestones(
        self, *, project_id: str | None = None, include_archived: bool = True
    ) -> list[Milestone]:
        """Milestones, dated ones first by target_date then undated, id-stable.

        The ordering rule (E14-T2 decision): a milestone with a ``target_date``
        sorts ahead of one without, earliest date first; ties + undated break by
        id so the order is deterministic across replicas.
        """
        statement = select(Milestone)
        if project_id is not None:
            statement = statement.where(Milestone.project_id == project_id)
        if not include_archived:
            statement = statement.where(Milestone.status != "archived")
        rows = self._session.exec(statement).all()
        return sorted(
            rows,
            key=lambda m: (m.target_date is None, m.target_date or datetime.max, m.id),
        )

    def delete_milestone(self, milestone_id: str) -> None:
        """Delete a milestone, tombstoning its ticket memberships first.

        A membership FK-references the milestone, so the junction rows are
        removed (each its own tombstone event) before the milestone itself — no
        orphaned membership, no dangling FK.
        """
        milestone = self.get_milestone(milestone_id)
        ts = self._now()
        for membership in self._memberships_for_milestone(milestone_id):
            self._tombstone_membership(membership, now=ts)
        audit.write(
            self._session,
            actor_id=self._actor_id,
            action="milestone.delete",
            source=self._source,
            object_ref=f"milestones/{milestone.id}",
            before=audit.snapshot(milestone),
            now=ts,
        )
        self._emit("milestones", milestone.id, "tombstone", {})
        self._session.delete(milestone)
        self._session.commit()

    def add_ticket_to_milestone(self, ticket_id: str, milestone_id: str) -> TicketMilestone:
        """Group a ticket under a milestone (FR-E14-3).

        Integrity, fail-closed: the ticket and milestone both exist, share a
        project (a ticket only joins a milestone of its own project), and the
        membership is not a duplicate. The activity row lands on the ticket.
        """
        ticket = self.get_ticket(ticket_id)
        milestone = self.get_milestone(milestone_id)
        if ticket.project_id != milestone.project_id:
            raise TrackerValidationError("a ticket can only join a milestone in its own project")
        existing = self._session.exec(
            select(TicketMilestone).where(
                TicketMilestone.ticket_id == ticket_id,
                TicketMilestone.milestone_id == milestone_id,
            )
        ).first()
        if existing is not None:
            raise TrackerValidationError("this ticket is already in that milestone")

        ts = self._now()
        membership = TicketMilestone(
            ticket_id=ticket_id,
            milestone_id=milestone_id,
            created_by=self._actor_id,
            created_at=ts,
            updated_at=ts,
        )
        self._session.add(membership)
        self._session.flush()
        audit.write(
            self._session,
            actor_id=self._actor_id,
            action="milestone.add_ticket",
            source=self._source,
            object_ref=f"tickets/{ticket_id}",
            after=audit.snapshot(membership),
            now=ts,
        )
        self._emit("ticket_milestones", membership.id, "patch", audit.snapshot(membership))
        self._session.commit()
        self._session.refresh(membership)
        return membership

    def remove_ticket_from_milestone(self, membership_id: str) -> None:
        """Remove a ticket↔milestone membership — its only mutation."""
        membership = self._session.get(TicketMilestone, membership_id)
        if membership is None:
            raise TrackerNotFoundError("ticket_milestones", membership_id)
        self._tombstone_membership(membership, now=self._now())
        self._session.commit()

    def remove_ticket_from_milestone_by_pair(self, ticket_id: str, milestone_id: str) -> None:
        """Remove a membership identified by its (ticket, milestone) pair.

        The RESTful surface deletes ``/milestones/{m}/tickets/{t}``; a missing
        pair is a 404, never a silent no-op.
        """
        membership = self._session.exec(
            select(TicketMilestone).where(
                TicketMilestone.ticket_id == ticket_id,
                TicketMilestone.milestone_id == milestone_id,
            )
        ).first()
        if membership is None:
            raise TrackerNotFoundError("ticket_milestones", f"{ticket_id}/{milestone_id}")
        self._tombstone_membership(membership, now=self._now())
        self._session.commit()

    def milestones_for_ticket(self, ticket_id: str) -> list[Milestone]:
        """The milestones a ticket belongs to, id-stable."""
        self.get_ticket(ticket_id)
        milestone_ids = [
            m.milestone_id
            for m in self._session.exec(
                select(TicketMilestone).where(TicketMilestone.ticket_id == ticket_id)
            ).all()
        ]
        if not milestone_ids:
            return []
        rows = self._session.exec(
            select(Milestone).where(col(Milestone.id).in_(milestone_ids))
        ).all()
        return sorted(rows, key=lambda m: m.id)

    def tickets_for_milestone(self, milestone_id: str) -> list[Ticket]:
        """The tickets grouped under a milestone, id-stable."""
        self.get_milestone(milestone_id)
        ticket_ids = [m.ticket_id for m in self._memberships_for_milestone(milestone_id)]
        if not ticket_ids:
            return []
        rows = self._session.exec(select(Ticket).where(col(Ticket.id).in_(ticket_ids))).all()
        return sorted(rows, key=lambda t: t.id)

    def _memberships_for_milestone(self, milestone_id: str) -> list[TicketMilestone]:
        return list(
            self._session.exec(
                select(TicketMilestone).where(TicketMilestone.milestone_id == milestone_id)
            ).all()
        )

    def _tombstone_membership(self, membership: TicketMilestone, *, now: datetime) -> None:
        """Audit + emit a membership removal and delete the row (no commit)."""
        audit.write(
            self._session,
            actor_id=self._actor_id,
            action="milestone.remove_ticket",
            source=self._source,
            object_ref=f"tickets/{membership.ticket_id}",
            before=audit.snapshot(membership),
            now=now,
        )
        self._emit("ticket_milestones", membership.id, "tombstone", {})
        self._session.delete(membership)

    # ------------------------------------------------------------- follow-ups

    def create_follow_up(
        self,
        *,
        ticket_id: str,
        title: str,
        body: str = "",
        due_at: datetime | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> FollowUp:
        """Queue a follow-up against a ticket (FR-E15-1).

        Fail-closed before any write: a non-empty title within the title bound
        and an existing ticket. The audit row + the lww event land together,
        exactly like a milestone create. Agent-created follow-ups reach here only
        after a human approves the proposal (propose-first, E08); ``provenance``
        then records the original agent proposer rather than the approver.
        """
        title = title.strip()
        if not title:
            raise TrackerValidationError("a follow-up needs a non-empty title")
        if len(title) > _TITLE_MAX:
            raise TrackerValidationError(f"follow-up title exceeds {_TITLE_MAX} characters")
        if self._session.get(Ticket, ticket_id) is None:
            raise TrackerNotFoundError("tickets", ticket_id)

        ts = self._now()
        follow_up = FollowUp(
            ticket_id=ticket_id,
            title=title,
            body=body,
            status="open",
            due_at=_naive_utc(due_at) if due_at is not None else None,
            created_by=self._actor_id,
            provenance=provenance
            or {"origin": "manual", "actor_id": self._actor_id, "captured_at": ts.isoformat()},
            created_at=ts,
            updated_at=ts,
        )
        self._session.add(follow_up)
        self._session.flush()
        audit.write(
            self._session,
            actor_id=self._actor_id,
            action="follow_up.create",
            source=self._source,
            object_ref=f"follow_ups/{follow_up.id}",
            after=audit.snapshot(follow_up),
            now=ts,
        )
        self._emit("follow_ups", follow_up.id, "patch", audit.snapshot(follow_up))
        self._session.commit()
        self._session.refresh(follow_up)
        return follow_up

    def update_follow_up(self, follow_up_id: str, changes: dict[str, Any]) -> FollowUp:
        """Patch a follow-up's title/body/due_at. ``status`` moves only through
        ``complete_follow_up`` (so it can never silently re-open)."""
        follow_up = self.get_follow_up(follow_up_id)
        unknown = set(changes) - _FOLLOW_UP_PATCHABLE
        if unknown:
            raise TrackerValidationError(f"unknown follow-up fields: {sorted(unknown)}")
        if "title" in changes:
            changes["title"] = str(changes["title"]).strip()
            if not changes["title"]:
                raise TrackerValidationError("a follow-up needs a non-empty title")
            if len(changes["title"]) > _TITLE_MAX:
                raise TrackerValidationError(f"follow-up title exceeds {_TITLE_MAX} characters")
        if changes.get("due_at") is not None:
            changes["due_at"] = _naive_utc(changes["due_at"])
        return self._apply_patch(
            follow_up, changes, collection="follow_ups", action="follow_up.update"
        )

    def complete_follow_up(self, follow_up_id: str, *, status: str = "done") -> FollowUp:
        """Resolve a follow-up to ``done`` or ``dismissed`` (FR-E15-1).

        Only an ``open`` follow-up can be completed — completing an
        already-resolved one fails closed (a fresh follow-up is cheaper than a
        re-open dance)."""
        if status not in FOLLOW_UP_RESOLVED_STATUSES:
            raise TrackerValidationError(
                f"a follow-up completes to one of {FOLLOW_UP_RESOLVED_STATUSES}, not {status!r}"
            )
        follow_up = self.get_follow_up(follow_up_id)
        if follow_up.status != "open":
            raise TrackerValidationError(
                f"follow-up is already {follow_up.status}; only an open one can be completed"
            )
        return self._apply_patch(
            follow_up, {"status": status}, collection="follow_ups", action="follow_up.complete"
        )

    def get_follow_up(self, follow_up_id: str) -> FollowUp:
        follow_up = self._session.get(FollowUp, follow_up_id)
        if follow_up is None:
            raise TrackerNotFoundError("follow_ups", follow_up_id)
        return follow_up

    def search_follow_ups(
        self,
        *,
        ticket_id: str | None = None,
        due_before: datetime | None = None,
        status: str | None = None,
    ) -> list[FollowUp]:
        """Follow-ups by ticket / due-before / status, due soonest first.

        Ordering (FR-E15-1, "what's due before X?"): dated follow-ups first by
        ``due_at`` ascending, undated last, id-stable on ties — the milestone
        target_date rule applied to due dates."""
        if status is not None and status not in FOLLOW_UP_STATUSES:
            raise TrackerValidationError(
                f"unknown follow-up status {status!r}; expected one of {FOLLOW_UP_STATUSES}"
            )
        statement = select(FollowUp)
        if ticket_id is not None:
            statement = statement.where(FollowUp.ticket_id == ticket_id)
        if status is not None:
            statement = statement.where(FollowUp.status == status)
        if due_before is not None:
            statement = statement.where(
                col(FollowUp.due_at).is_not(None),
                col(FollowUp.due_at) < _naive_utc(due_before),
            )
        rows = self._session.exec(statement).all()
        return sorted(rows, key=lambda f: (f.due_at is None, f.due_at or datetime.max, f.id))

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
