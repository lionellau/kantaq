#!/usr/bin/env python
"""Agent-role context UAT — different agent user types over the MCP surface (MOD-21).

`scripts/uat_roles.py` varies the *human* role on the web/API; this varies the
*agent context role* on the **MCP gateway**. The four locked agent roles
(code_agent / qa_agent / design_agent / product_agent) each get a *different*
memory-context bundle from the SAME ticket — the role-aware resolver (FR-E16-1..4)
is the heart of the "agent-native" claim, so the UAT proves it through the real
gateway, not a unit call.

It seeds one team memory entry in every one of the seven memory spaces, links
them all to one ticket, then for each agent role derives a real gateway session
(role declared) and calls `role_context_preview` through `Gateway.handle_call` —
the same path Claude Code drives. A space appears in the bundle **iff** it is in
that role's `include_scopes`, asserted against `kantaq_core.memory_policy.policy_for`
(the source of truth the gateway itself consults — drift-proof, like uat_roles
derives from `can`). Prints a role × space matrix; exits non-zero on any mismatch.

    uv run python scripts/uat_agent_roles.py                 # the matrix
    uv run python scripts/uat_agent_roles.py --report out.md # + markdown

CI gate: the same property is enforced by
``packages/mcp/tests/test_agent_role_context.py`` (run by the ``py`` workflow's
pytest). This script is the human-readable *reporter* over it (the
``compat_check.py`` ↔ ``tests/compat`` idiom).

Hermetic: throwaway DB, no Supabase, no real workspace.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The seven memory spaces (FR-E13-4). include ∪ exclude partitions these for every
# role, so one entry per space is the complete probe board.
SPACES: tuple[str, ...] = (
    "codebase",
    "decision",
    "ticket",
    "project",
    "release",
    "workspace",
    "agent_run",
)
ROLES: tuple[str, ...] = ("code_agent", "qa_agent", "design_agent", "product_agent")


def _build_arena():  # noqa: ANN202 — internal
    """Boot a gateway over a throwaway DB; seed a ticket + one team note per space.

    Returns (gateway, agent_actor, session_factory, ticket_id, mem_by_space).
    """
    data_dir = Path(tempfile.mkdtemp(prefix="kantaq-agentrole-uat-"))
    os.environ["LOCAL_DB_PATH"] = str(data_dir / "local.sqlite")
    os.environ["HUB_MODE"] = "local"
    os.environ.pop("KANTAQ_DB_URL", None)

    from sqlmodel import Session, select

    from kantaq.cli import main as kantaq_cli
    from kantaq_core.identity import IdentityService, Role
    from kantaq_core.memory.service import MemoryService
    from kantaq_core.tracker.service import TrackerService
    from kantaq_db.models import Workspace
    from kantaq_db.session import get_engine, sqlite_url
    from kantaq_mcp.catalog import CATALOG
    from kantaq_mcp.gateway import Gateway
    from kantaq_mcp.session import (
        AUDIT_POLICY_STANDARD,
        COLLECTION_SCOPE_ALL,
        WRITE_MODE_PROPOSE_ONLY,
        GatewaySession,
    )
    from kantaq_runtime.auth import ensure_local_identity, keychain_for
    from kantaq_runtime.config import get_settings

    if kantaq_cli(["db", "migrate"]) != 0:
        raise SystemExit("agent-role uat: migrations failed")

    settings = get_settings()
    engine = get_engine(sqlite_url(settings.local_db_path))
    if ensure_local_identity(engine, keychain_for(settings)) is None:
        raise SystemExit("agent-role uat: expected a fresh database")

    def _now() -> _dt.datetime:
        return _dt.datetime.now(_dt.UTC).replace(tzinfo=None)

    with Session(engine) as session:
        identity = IdentityService(session)
        owner_id = identity.list_members()[0].id
        # An agent member to authenticate as; the session's verbs/role are set
        # explicitly below (memory.read unlocks role_context_preview).
        agent = identity.invite(
            email="context-agent@uat.local",
            role=Role.agent,
            scopes=["tickets.read", "memory.read", "proposals.write"],
        )
        workspace_id = session.exec(select(Workspace)).one().id
        tracker = TrackerService(session, actor_id=owner_id, source="app", now=_now)
        project = tracker.create_project(workspace_id=workspace_id, name="Agent-role UAT")
        ticket = tracker.create_ticket(project_id=project.id, title="Context bundle UAT")
        ticket_id = ticket.id
        mem = MemoryService(session, actor_id=owner_id, source="app", now=_now)
        mem_by_space: dict[str, str] = {}
        for space in SPACES:
            entry = mem.create_entry(
                title=f"{space} note", body=f"a {space} note", space=space, visibility="team"
            )
            mem.link(entry.id, ticket_id, reason="uat")
            mem_by_space[entry.id] = space
        session.commit()

    gateway = Gateway(engine)
    agent_actor = gateway.authenticate(agent.plaintext)
    if agent_actor is None:
        raise SystemExit("agent-role uat: agent token failed to authenticate")

    granted = ("tickets.read", "memory.read", "proposals.write")
    allowed_tools = tuple(s.name for s in CATALOG if s.required_action in set(granted))

    def session_for(role: str) -> GatewaySession:
        now = _now()
        return GatewaySession(
            session_id=f"s-agentrole-{role}",
            member_id=agent.member_id,
            role=Role.agent.value,
            token_id="tok-agentrole",
            scopes=granted,
            allowed_tools=allowed_tools,
            write_mode=WRITE_MODE_PROPOSE_ONLY,
            created_at=now,
            expires_at=now.replace(year=2030),
            collection_scope=(COLLECTION_SCOPE_ALL,),
            granted_verbs=granted,
            agent_role=role,
            memory_policy_id=None,
            audit_policy=AUDIT_POLICY_STANDARD,
            grant_id=None,
        )

    return gateway, agent_actor, session_for, ticket_id, mem_by_space


def _included_spaces(gateway, agent_actor, session, ticket_id, mem_by_space) -> set[str]:  # noqa: ANN001
    """Drive role_context_preview through the gateway; return the spaces it returned."""
    result = gateway.handle_call(
        actor=agent_actor,
        session=session,
        tool_name="role_context_preview",
        args={"ticket_id": ticket_id},
    )
    bundle = result["bundle"]
    included_ids = {e["id"] for e in bundle["included"]}
    return {mem_by_space[i] for i in included_ids if i in mem_by_space}


def _render(date: str, matrix: dict[str, set[str]], oracle: dict[str, set[str]]) -> str:
    from kantaq_core.memory_policy import policy_for

    lines = [f"# Agent-role context UAT — different agent user types vs the gateway · {date}", ""]
    lines.append(
        "Each of the four locked agent roles calls `role_context_preview` over the "
        "**real MCP gateway** on the SAME ticket (one team note linked in every "
        "memory space). A space is ✓ iff it appears in that role's bundle — which "
        "must equal the role's `include_scopes` (`kantaq_core.memory_policy`, the "
        "filter the gateway itself runs). Generated by `scripts/uat_agent_roles.py`."
    )
    lines.append("")
    lines.append("| memory space | " + " | ".join(ROLES) + " |")
    lines.append("|" + "---|" * (len(ROLES) + 1))
    for space in SPACES:
        cells = []
        for role in ROLES:
            got = space in matrix[role]
            want = space in oracle[role]
            mark = "✓" if got else "·"
            cells.append(mark if got == want else f"✗({mark})")
        lines.append(f"| {space} | " + " | ".join(cells) + " |")
    lines.append("")
    for role in ROLES:
        lines.append(f"- **{role}** — {policy_for(role).rationale}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uat_agent_roles", description=__doc__)
    parser.add_argument("--date", default=_dt.date.today().isoformat())
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)

    gateway, agent_actor, session_for, ticket_id, mem_by_space = _build_arena()

    from kantaq_core.memory_policy import policy_for

    matrix: dict[str, set[str]] = {}
    oracle: dict[str, set[str]] = {}
    for role in ROLES:
        matrix[role] = _included_spaces(
            gateway, agent_actor, session_for(role), ticket_id, mem_by_space
        )
        oracle[role] = set(policy_for(role).include_scopes) & set(SPACES)

    print(f"\nAgent-role context UAT — agent user types vs the gateway (hermetic) · {args.date}")
    print("  (✓ included in the role's bundle · = withheld; a space is ✓ iff in include_scopes)\n")
    print(f"  {'memory space':14}" + "".join(f"{r:>14}" for r in ROLES))
    total = passed = 0
    for space in SPACES:
        line = f"  {space:14}"
        for role in ROLES:
            got = space in matrix[role]
            want = space in oracle[role]
            ok = got == want
            total += 1
            passed += ok
            cell = ("✓" if got else "·") + ("" if ok else "!")
            line += f"{cell:>14}"
        print(line)

    verdict = "PASS" if passed == total else "FAIL"
    print(f"\nAgent-role context UAT: {passed} / {total} cells {verdict}  ·  {args.date}")
    print("  Each role gets a DISTINCT bundle from the same ticket — the role-aware resolver.")
    if passed != total:
        print(
            "\nA bundle did not match the role's include_scopes — a resolver/policy drift. Fix it."
        )

    if args.report is not None:
        args.report.write_text(_render(args.date, matrix, oracle), encoding="utf-8")
        print(f"\nWrote the agent-role matrix to {args.report}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
