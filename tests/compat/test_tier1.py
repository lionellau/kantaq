"""Tier-1 compatibility acceptance suite: T1–T8 (E11-T2, MOD-24, PRD §20.4).

Each test is one of the eight Tier-1 acceptance criteria (PRD §20.4 calls them
C1–C8; the sprint calls them T1–T8), driven by ``FakeAgent`` — the official MCP
SDK client over the real gateway ASGI app, the same client library a Tier-1
agent (Claude Code, Cursor) embeds. A client earns the Tier-1 badge only when
all eight pass; this module is the **scripted-client subset that runs in CI**,
and its green is the matrix's ``Pass rate`` for the scripted client. The real
Claude Code / Cursor runs against pinned versions are recorded out of CI
(``scripts/compat_check.py`` → ``docs/clients/compatibility.md``).

As-built notes where PRD §20.4 wording and the shipped vocabulary differ — the
test asserts the real security property and the matrix documents the mapping:
* C2's ``included_memory`` / ``excluded_memory`` / ``missing_memory`` lists are
  split across ``role_context_get`` (the agent bundle: included + token
  estimate) and ``role_context_preview`` (the inspect view: excluded-with-reason
  + missing), per MOD-09/E16 as-built;
* C4's ``policy_denied`` is realized as the specific failed eight-check reason
  (here ``tool_allowlist`` — a read-only grant's allowlist has no propose tool),
  returned as a structured ``{code, message}`` error and audited as ``tool.deny``;
* C5's ``unauthenticated`` is the gateway's 401 at the door (an audited identity
  denial); the rotated old token is rejected within the < 5 s revocation budget;
* C7's ``session_expired`` is the gateway deny reason ``expiry``.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from kantaq_core.identity import MintedToken
from kantaq_db.models import AuditEvent
from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.compat import FakeAgent, is_untrusted_wrapped

AppFactory = Callable[[], Starlette]
AuditProbe = Callable[..., list[AuditEvent]]


# T1 — First connection (C1): paste the snippet, restart, first call within 5 s.
def test_t1_first_connection_under_five_seconds(
    gateway_app: AppFactory, agent: MintedToken, seed: dict[str, str]
) -> None:
    start = time.perf_counter()
    with FakeAgent(gateway_app(), token=agent.plaintext) as client:
        result = client.call("workspace_get")
        workspace = result.require()["workspace"]
    elapsed = time.perf_counter() - start

    assert workspace["id"] == seed["workspace_id"]
    # Valid workspace JSON: every required field present, the name fenced.
    assert {"id", "name", "created_at", "updated_at"} <= workspace.keys()
    assert is_untrusted_wrapped(workspace["name"])
    assert elapsed < 5.0, f"first connection + first call took {elapsed:.2f}s (budget 5 s)"


# T2 — Role-aware ticket read (C2): full ticket, then the role bundle.
def test_t2_role_aware_ticket_read(
    gateway_app: AppFactory, agent: MintedToken, grant_id: str, seed: dict[str, str]
) -> None:
    tid = seed["ticket_id"]
    with FakeAgent(
        gateway_app(), token=agent.plaintext, grant_id=grant_id, agent_role="code_agent"
    ) as client:
        ticket = client.call("ticket_get", {"ticket_id": tid}).require()["ticket"]
        bundle = client.call("role_context_get", {"ticket_id": tid}).require()["bundle"]
        preview = client.call("role_context_preview", {"ticket_id": tid}).require()["bundle"]

    # Full ticket fields, human strings fenced.
    assert ticket["id"] == tid
    assert is_untrusted_wrapped(ticket["title"])
    assert ticket["status"] in {"todo", "doing", "done"}

    # The role bundle: included memory (policy-filtered) + a token estimate (C2).
    assert bundle["role"] == "code_agent"
    assert bundle["policy_id"]
    included = {entry["id"] for entry in bundle["included"]}
    assert seed["code_memory_id"] in included  # codebase scope: a code_agent reads it
    assert seed["release_memory_id"] not in included  # release scope: excluded
    assert isinstance(bundle["token_estimate"], int) and bundle["token_estimate"] > 0

    # The excluded-with-reason + missing lists (C2), via the inspect view.
    excluded = {item["memory_id"]: item["reason"] for item in preview["excluded"]}
    assert excluded.get(seed["release_memory_id"]) == "exclude_scope:release"
    assert isinstance(preview["missing"], list)


# T3 — Propose + human approval (C3): propose → Inbox → Approve → ticket reflects.
def test_t3_propose_then_human_approval(
    gateway_app: AppFactory,
    runtime: TestClient,
    agent: MintedToken,
    owner: MintedToken,
    grant_id: str,
    seed: dict[str, str],
    audit_rows: AuditProbe,
) -> None:
    tid = seed["ticket_id"]
    with FakeAgent(
        gateway_app(), token=agent.plaintext, grant_id=grant_id, agent_role="code_agent"
    ) as client:
        out = client.call(
            "agent_action_propose",
            {"ticket_id": tid, "changes": {"status": "doing"}, "note": "starting work"},
        ).require()
    assert out["applied"] is False  # a proposal never changes the ticket itself
    proposal_id = out["proposal"]["id"]

    # The proposal is visible in the human's Inbox queue (the runtime API).
    admin = {"Authorization": f"Bearer {owner.plaintext}"}
    listed = runtime.get("/v1/proposals", params={"status": "pending"}, headers=admin)
    assert listed.status_code == 200
    assert proposal_id in {p["id"] for p in listed.json()}

    # The human clicks Approve; the ticket reflects the change.
    approved = runtime.post(f"/v1/proposals/{proposal_id}/approve", headers=admin)
    assert approved.status_code == 200
    assert approved.json()["ticket"]["status"] == "doing"

    # Audit: proposer and approver are distinct actors (the sprint-2 invariant).
    assert {r.actor_id for r in audit_rows("proposal.create")} == {agent.member_id}
    assert {r.actor_id for r in audit_rows("proposal.approve")} == {owner.member_id}


# T4 — Permission denial (C4): a read-only grant cannot propose; the deny is audited.
def test_t4_permission_denial_is_structured_and_audited(
    gateway_app: AppFactory,
    agent: MintedToken,
    readonly_grant_id: str,
    seed: dict[str, str],
    audit_rows: AuditProbe,
) -> None:
    tid = seed["ticket_id"]
    with FakeAgent(gateway_app(), token=agent.plaintext, grant_id=readonly_grant_id) as client:
        # The grant's allowlist has no propose tool — the model cannot request one.
        assert "agent_action_propose" not in client.tool_names()
        assert "ticket_get" in client.tool_names()
        denied = client.call(
            "agent_action_propose", {"ticket_id": tid, "changes": {"status": "done"}}
        )

    assert denied.ok is False
    assert denied.code == "tool_allowlist"  # the failed eight-check (PRD C4 policy_denied)
    assert denied.message  # the structured error names the tool

    denials = audit_rows("tool.deny")
    assert len(denials) == 1
    assert denials[0].actor_id == agent.member_id
    assert denials[0].source == "mcp"
    assert denials[0].after is not None and denials[0].after["reason"] == "tool_allowlist"


# T5 — Token rotation (C5): rotate; old token rejected; the new token is required.
def test_t5_token_rotation(
    gateway_app: AppFactory,
    runtime: TestClient,
    agent: MintedToken,
    grant_id: str,
    clock: FakeClock,
    audit_rows: AuditProbe,
) -> None:
    old = agent.plaintext
    # The agent works with its current token + grant.
    with FakeAgent(gateway_app(), token=old, grant_id=grant_id, agent_role="code_agent") as client:
        assert client.call("workspace_get").ok

    # The member rotates their own token from Settings (self-rotate needs no admin).
    rotated = runtime.post(
        f"/v1/members/{agent.member_id}/rotate", headers={"Authorization": f"Bearer {old}"}
    )
    assert rotated.status_code == 200
    new = rotated.json()["token"]
    assert new and new != old

    # The revocation/propagation budget elapses (< 5 s): the gateway's verify cache
    # of the old token expires, so the next presentation re-reads the store.
    clock.advance(5)

    # The old token is rejected at the door (unauthenticated → an audited identity deny).
    # The SDK raises the 401 on connect, so the FakeAgent context never opens.
    with pytest.raises(BaseException), FakeAgent(gateway_app(), token=old):  # noqa: B017
        pass
    assert any(
        r.after is not None and r.after.get("reason") == "identity" for r in audit_rows("tool.deny")
    ), "the rejected old token is audited as an identity denial"

    # The new token is required, and it works (a fresh token-derived session).
    with FakeAgent(gateway_app(), token=new) as client:
        assert client.call("workspace_get").ok

    # As-built, *stronger* than PRD §20.4 C5: rotation also revokes the member's
    # derived grants, so the rotated-away grant can never be re-bound — not even
    # with the new token. A leaked old token can never be paired with a live grant.
    with FakeAgent(gateway_app(), token=new, grant_id=grant_id, agent_role="code_agent") as client:
        assert client.call("workspace_get").ok is False


# T6 — Untrusted content tagging (C6): a hostile ticket body is fenced as data.
def test_t6_untrusted_content_is_fenced(
    gateway_app: AppFactory, agent: MintedToken, grant_id: str, seed: dict[str, str]
) -> None:
    with FakeAgent(
        gateway_app(), token=agent.plaintext, grant_id=grant_id, agent_role="code_agent"
    ) as client:
        ticket = client.call("ticket_get", {"ticket_id": seed["ticket_id"]}).require()["ticket"]

    description = ticket["description"]
    assert description.startswith('<untrusted source="ticket.description">')
    assert is_untrusted_wrapped(description)
    # The injection literal survives inside the fence — present, but as data.
    assert seed["injection_body"] in description


# T7 — Session expiry (C7): an expired session denies; re-init restores service.
def test_t7_session_expiry_then_reinitialize(
    gateway_app: AppFactory, agent: MintedToken, clock: FakeClock, seed: dict[str, str]
) -> None:
    tid = seed["ticket_id"]
    # The minimal token-derived session carries the v0.1 default 60-min TTL.
    with FakeAgent(gateway_app(), token=agent.plaintext) as client:
        assert client.call("ticket_get", {"ticket_id": tid}).ok
        clock.advance(3601)  # past the 60-minute session TTL
        expired = client.call("ticket_get", {"ticket_id": tid})
        assert expired.ok is False
        assert expired.code == "expiry"  # PRD C7 session_expired

    # Re-initialize: a fresh transport session via the same valid credential works.
    with FakeAgent(gateway_app(), token=agent.plaintext) as client:
        assert client.call("ticket_get", {"ticket_id": tid}).ok


# T8 — Audit completeness (C8): a scripted session leaves no gaps.
def test_t8_audit_completeness(
    gateway_app: AppFactory,
    agent: MintedToken,
    grant_id: str,
    seed: dict[str, str],
    audit_rows: AuditProbe,
) -> None:
    tid = seed["ticket_id"]
    # A representative C1–C7 sequence: reads, a write, and a denial in one session.
    with FakeAgent(
        gateway_app(), token=agent.plaintext, grant_id=grant_id, agent_role="code_agent"
    ) as client:
        client.call("workspace_get")
        client.call("ticket_get", {"ticket_id": tid})
        client.call("role_context_get", {"ticket_id": tid})
        client.call(
            "agent_action_propose",
            {"ticket_id": tid, "changes": {"status": "doing"}, "note": "go"},
        )
        # A read the code_agent policy withholds — a memory-policy denial.
        client.call("memory_get", {"memory_id": seed["release_memory_id"]})
    # Leaving the context tears down the lifespan, which flushes aggregated reads.

    rows = audit_rows()
    # Every row carries the C8 completeness fields: actor, action, source, timestamp.
    for row in rows:
        assert row.actor_id and row.action and row.source and row.created_at is not None
    mcp_rows = [row for row in rows if row.source == "mcp"]
    assert mcp_rows, "the scripted session left an audit trail"
    # No gaps: every mcp row is attributed to the acting agent, no one else.
    assert {row.actor_id for row in mcp_rows} == {agent.member_id}

    # Reads aggregate to one summary with object refs (MOD-07 §8.6 policy).
    reads = audit_rows("agent.read")
    assert len(reads) == 1
    assert reads[0].after is not None
    assert reads[0].after["reads"] >= 3
    assert reads[0].after["objects"]

    # The write is detailed, with its object ref.
    proposals = audit_rows("proposal.create")
    assert len(proposals) == 1
    assert proposals[0].object_ref is not None and proposals[0].object_ref.startswith(
        "agent_proposals/"
    )

    # The denial is detailed: the failed check + the grant-derived session reference.
    denials = audit_rows("tool.deny")
    assert len(denials) == 1
    assert denials[0].after is not None
    assert denials[0].after["reason"] == "memory_policy"
    assert denials[0].after["session_id"]
    assert denials[0].object_ref == "tools/memory_get"
