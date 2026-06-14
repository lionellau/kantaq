"""kantaq shared test harness (MOD-30).

Builders, fakes, and fixtures that make the Test Harness Engineering Standard
(docs/test/test-harness-standard.md) cheap to follow. Import what you need:

    from kantaq_test_harness import FakeClock, SeededRandom, FakeBackend, build_ticket

This package is imported at pytest *plugin registration* (the ``pytest11``
entry point lives in ``fixtures``), which runs before pytest-cov starts
measuring. Names that reach the real packages (``FakeBackend`` and the model
builders now share the canonical MOD-04 ``Event``; ``replica`` composes the
tracker and sync engine) are therefore exposed lazily (PEP 562) so importing
the harness never silently drops kantaq_core out of coverage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.keychain import FakeKeychain
from kantaq_test_harness.random import SeededRandom

if TYPE_CHECKING:
    from kantaq_test_harness.audit import AuditCapture
    from kantaq_test_harness.backend import CommittedEvent, FakeBackend
    from kantaq_test_harness.builders import (
        build_agent_proposal,
        build_audit_event,
        build_comment,
        build_event,
        build_member,
        build_memory_entry,
        build_memory_link,
        build_project,
        build_ticket,
        build_ticket_relationship,
        build_token,
        build_workspace,
    )
    from kantaq_test_harness.compat import FakeAgent, ToolCall, is_untrusted_wrapped
    from kantaq_test_harness.hero_flow import (
        DEFAULT_BUDGET_SECONDS,
        HeroFlowTimer,
        HeroFlowTooSlow,
    )
    from kantaq_test_harness.injection import InjectionFixture, load_injection_corpus
    from kantaq_test_harness.mcp import FakeMCPClient
    from kantaq_test_harness.models import (
        AgentProposal,
        AuditEvent,
        Comment,
        Event,
        Member,
        MemoryEntry,
        MemoryLink,
        PrivacyClass,
        Project,
        Ticket,
        TicketRelationship,
        Token,
        Workspace,
    )
    from kantaq_test_harness.red_team import (
        ATTACK_CATALOG,
        Attack,
        AttackCategory,
        AttackOutcome,
        attempt,
        categories_covered,
    )

__version__ = "0.0.5"

# Lazy attribute → "module:name". Resolved on first access, cached in globals.
_LAZY: dict[str, str] = {
    "AuditCapture": "kantaq_test_harness.audit:AuditCapture",
    "CommittedEvent": "kantaq_test_harness.backend:CommittedEvent",
    "FakeBackend": "kantaq_test_harness.backend:FakeBackend",
    "DEFAULT_BUDGET_SECONDS": "kantaq_test_harness.hero_flow:DEFAULT_BUDGET_SECONDS",
    "HeroFlowTimer": "kantaq_test_harness.hero_flow:HeroFlowTimer",
    "HeroFlowTooSlow": "kantaq_test_harness.hero_flow:HeroFlowTooSlow",
    # FakeMCPClient pulls the MCP SDK + httpx; the corpus loader is stdlib but
    # rides the same lazy path so the plugin import stays lean (coverage rule).
    "FakeMCPClient": "kantaq_test_harness.mcp:FakeMCPClient",
    # FakeAgent (the Compatibility profile's scripted Tier-1 client, E11-T2)
    # wraps FakeMCPClient, so it rides the same lazy path (coverage rule).
    "FakeAgent": "kantaq_test_harness.compat:FakeAgent",
    "ToolCall": "kantaq_test_harness.compat:ToolCall",
    "is_untrusted_wrapped": "kantaq_test_harness.compat:is_untrusted_wrapped",
    "InjectionFixture": "kantaq_test_harness.injection:InjectionFixture",
    "load_injection_corpus": "kantaq_test_harness.injection:load_injection_corpus",
    # The red-team battery rides the lazy path too (it imports the gateway):
    # keep the pytest-plugin import lean so kantaq_mcp stays in coverage.
    "ATTACK_CATALOG": "kantaq_test_harness.red_team:ATTACK_CATALOG",
    "Attack": "kantaq_test_harness.red_team:Attack",
    "AttackCategory": "kantaq_test_harness.red_team:AttackCategory",
    "AttackOutcome": "kantaq_test_harness.red_team:AttackOutcome",
    "attempt": "kantaq_test_harness.red_team:attempt",
    "categories_covered": "kantaq_test_harness.red_team:categories_covered",
    "AgentProposal": "kantaq_test_harness.models:AgentProposal",
    "AuditEvent": "kantaq_test_harness.models:AuditEvent",
    "Comment": "kantaq_test_harness.models:Comment",
    "Event": "kantaq_test_harness.models:Event",
    "Member": "kantaq_test_harness.models:Member",
    "MemoryEntry": "kantaq_test_harness.models:MemoryEntry",
    "MemoryLink": "kantaq_test_harness.models:MemoryLink",
    "PrivacyClass": "kantaq_test_harness.models:PrivacyClass",
    "Project": "kantaq_test_harness.models:Project",
    "Ticket": "kantaq_test_harness.models:Ticket",
    "TicketRelationship": "kantaq_test_harness.models:TicketRelationship",
    "Token": "kantaq_test_harness.models:Token",
    "Workspace": "kantaq_test_harness.models:Workspace",
    "build_agent_proposal": "kantaq_test_harness.builders:build_agent_proposal",
    "build_audit_event": "kantaq_test_harness.builders:build_audit_event",
    "build_comment": "kantaq_test_harness.builders:build_comment",
    "build_event": "kantaq_test_harness.builders:build_event",
    "build_member": "kantaq_test_harness.builders:build_member",
    "build_memory_entry": "kantaq_test_harness.builders:build_memory_entry",
    "build_memory_link": "kantaq_test_harness.builders:build_memory_link",
    "build_project": "kantaq_test_harness.builders:build_project",
    "build_ticket": "kantaq_test_harness.builders:build_ticket",
    "build_ticket_relationship": "kantaq_test_harness.builders:build_ticket_relationship",
    "build_token": "kantaq_test_harness.builders:build_token",
    "build_workspace": "kantaq_test_harness.builders:build_workspace",
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module_name, _, attr = target.partition(":")
    value = getattr(import_module(module_name), attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


__all__ = [
    "ATTACK_CATALOG",
    "DEFAULT_BUDGET_SECONDS",
    "AgentProposal",
    "Attack",
    "AttackCategory",
    "AttackOutcome",
    "AuditCapture",
    "AuditEvent",
    "Comment",
    "CommittedEvent",
    "Event",
    "FakeAgent",
    "FakeBackend",
    "FakeClock",
    "FakeKeychain",
    "FakeMCPClient",
    "HeroFlowTimer",
    "HeroFlowTooSlow",
    "InjectionFixture",
    "Member",
    "MemoryEntry",
    "MemoryLink",
    "PrivacyClass",
    "Project",
    "SeededRandom",
    "Ticket",
    "TicketRelationship",
    "Token",
    "ToolCall",
    "Workspace",
    "attempt",
    "build_agent_proposal",
    "build_audit_event",
    "build_comment",
    "build_event",
    "build_member",
    "build_memory_entry",
    "build_memory_link",
    "build_project",
    "build_ticket",
    "build_ticket_relationship",
    "build_token",
    "build_workspace",
    "categories_covered",
    "is_untrusted_wrapped",
    "load_injection_corpus",
]
