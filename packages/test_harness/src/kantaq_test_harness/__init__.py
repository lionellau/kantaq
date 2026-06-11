"""kantaq shared test harness (MOD-30).

Builders, fakes, and fixtures that make the Test Harness Engineering Standard
(docs/test/test-harness-standard.md) cheap to follow. Import what you need:

    from kantaq_test_harness import FakeClock, SeededRandom, FakeBackend, build_ticket
"""

from __future__ import annotations

from kantaq_test_harness.audit import AuditCapture
from kantaq_test_harness.backend import CommittedEvent, FakeBackend
from kantaq_test_harness.builders import (
    build_agent_proposal,
    build_audit_event,
    build_comment,
    build_event,
    build_member,
    build_project,
    build_ticket,
    build_token,
    build_workspace,
)
from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.hero_flow import (
    DEFAULT_BUDGET_SECONDS,
    HeroFlowTimer,
    HeroFlowTooSlow,
)
from kantaq_test_harness.keychain import FakeKeychain
from kantaq_test_harness.models import (
    AgentProposal,
    AuditEvent,
    Comment,
    Event,
    Member,
    PrivacyClass,
    Project,
    Ticket,
    Token,
    Workspace,
)
from kantaq_test_harness.random import SeededRandom

__version__ = "0.0.5"

__all__ = [
    "DEFAULT_BUDGET_SECONDS",
    "AgentProposal",
    "AuditCapture",
    "AuditEvent",
    "Comment",
    "CommittedEvent",
    "Event",
    "FakeBackend",
    "FakeClock",
    "FakeKeychain",
    "HeroFlowTimer",
    "HeroFlowTooSlow",
    "Member",
    "PrivacyClass",
    "Project",
    "SeededRandom",
    "Ticket",
    "Token",
    "Workspace",
    "build_agent_proposal",
    "build_audit_event",
    "build_comment",
    "build_event",
    "build_member",
    "build_project",
    "build_ticket",
    "build_token",
    "build_workspace",
]
