"""Builders — one call to get a valid entity with sane defaults + overrides.

Every builder takes an optional ``SeededRandom`` (so ids are deterministic per
seed) and ``**overrides`` applied last. Example:

    rng = SeededRandom(1)
    ticket = build_ticket(rng, title="Fix login", status="in_progress")
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from kantaq_test_harness.models import (
    AgentProposal,
    AuditEvent,
    Comment,
    Event,
    Member,
    MemoryEntry,
    MemoryLink,
    Project,
    Ticket,
    Token,
    Workspace,
)
from kantaq_test_harness.random import SeededRandom

_DEFAULT_RNG = SeededRandom(0)


def _rng(rng: SeededRandom | None) -> SeededRandom:
    return rng if rng is not None else _DEFAULT_RNG


def build_workspace(rng: SeededRandom | None = None, **overrides: Any) -> Workspace:
    base = Workspace(id=_rng(rng).ident("ws"), name="Acme Workspace")
    return replace(base, **overrides)


def build_project(rng: SeededRandom | None = None, **overrides: Any) -> Project:
    r = _rng(rng)
    base = Project(id=r.ident("prj"), workspace_id=r.ident("ws"), name="AcmeApp v1")
    return replace(base, **overrides)


def build_ticket(rng: SeededRandom | None = None, **overrides: Any) -> Ticket:
    r = _rng(rng)
    base = Ticket(id=r.ident("tkt"), project_id=r.ident("prj"), title="A ticket")
    return replace(base, **overrides)


def build_comment(rng: SeededRandom | None = None, **overrides: Any) -> Comment:
    r = _rng(rng)
    base = Comment(
        id=r.ident("cmt"), ticket_id=r.ident("tkt"), author_id=r.ident("mbr"), body="A comment"
    )
    return replace(base, **overrides)


def build_member(rng: SeededRandom | None = None, **overrides: Any) -> Member:
    r = _rng(rng)
    base = Member(id=r.ident("mbr"), workspace_id=r.ident("ws"), email="dev@example.com")
    return replace(base, **overrides)


def build_token(rng: SeededRandom | None = None, **overrides: Any) -> Token:
    r = _rng(rng)
    base = Token(id=r.ident("tok"), member_id=r.ident("mbr"), hashed=r.token(32))
    return replace(base, **overrides)


def build_audit_event(rng: SeededRandom | None = None, **overrides: Any) -> AuditEvent:
    r = _rng(rng)
    base = AuditEvent(
        id=r.ident("aud"), actor_id=r.ident("mbr"), action="ticket.update", target=r.ident("tkt")
    )
    return replace(base, **overrides)


def build_agent_proposal(rng: SeededRandom | None = None, **overrides: Any) -> AgentProposal:
    r = _rng(rng)
    base = AgentProposal(id=r.ident("prop"), ticket_id=r.ident("tkt"), proposer_id=r.ident("agent"))
    return replace(base, **overrides)


def build_memory_entry(rng: SeededRandom | None = None, **overrides: Any) -> MemoryEntry:
    base = MemoryEntry(id=_rng(rng).ident("mem"), title="A memory entry")
    return replace(base, **overrides)


def build_memory_link(rng: SeededRandom | None = None, **overrides: Any) -> MemoryLink:
    r = _rng(rng)
    base = MemoryLink(id=r.ident("mlk"), ticket_id=r.ident("tkt"), memory_id=r.ident("mem"))
    return replace(base, **overrides)


def build_event(rng: SeededRandom | None = None, **overrides: Any) -> Event:
    r = _rng(rng)
    base = Event(
        event_id=r.sortable_id(),
        collection="tickets",
        entity_id=r.ident("tkt"),
        actor_id=r.ident("mbr"),
        actor_seq=1,
    )
    return replace(base, **overrides)
