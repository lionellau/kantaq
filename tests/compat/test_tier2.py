"""Tier-2 compatibility acceptance suite: S1–S6 = the **stdio** versions of the
Tier-1 T1–T6 (E11-T4, MOD-24/MOD-30, PRD §20.4 / §8.11).

Tier-2 is Codex over the gateway's stdio transport (E09-T4). S1–S6 mirror T1–T6
with only the transport swapped: ``FakeStdioAgent`` drives the **official MCP SDK
client** over the SDK's in-memory client↔server streams against the shared
``build_stdio_server`` — the same wiring ``kantaq mcp stdio`` runs — binding the
grant via the env-var contract (``KANTAQ_MCP_TOKEN`` / ``KANTAQ_MCP_GRANT_ID`` /
``KANTAQ_MCP_AGENT_ROLE``) instead of HTTP headers. A denial over stdio is
byte-for-byte the decision it is over HTTP, because it is the same
``Gateway.handle_call`` (the exhaustive deny matrix is E09-T4's own
``test_stdio.py``; here S4 re-checks the one structured denial a Tier-2 client
must see, like T4).

The fixtures (``gateway``/``runtime``/``agent``/``grant_id``/``readonly_grant_id``/
``seed``) are the shared Tier-1 conftest, reused unchanged. The real Codex run
(pinned 0.130.0) over the actual stdin/stdout pipe is the manual release step
recorded in ``docs/clients/compatibility.md``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from kantaq_core.identity import MintedToken
from kantaq_db.models import AuditEvent
from kantaq_mcp.gateway import Gateway
from kantaq_test_harness.clock import FakeClock
from kantaq_test_harness.compat import is_untrusted_wrapped
from kantaq_test_harness.stdio import connect_stdio, stdio_transport_ready

AuditProbe = Callable[..., list[AuditEvent]]

pytestmark = pytest.mark.skipif(
    not stdio_transport_ready(),
    reason="Tier-2 stdio path not wired (E09-T4 transport + the stdio harness seam)",
)


# S1 — First connection over stdio (mirrors T1): launch, list, first call.
def test_s1_first_connection_over_stdio(
    gateway: Gateway, agent: MintedToken, seed: dict[str, str]
) -> None:
    with connect_stdio(gateway, token=agent.plaintext) as client:
        assert "workspace_get" in client.tool_names()
        workspace = client.call("workspace_get").require()["workspace"]
    assert workspace["id"] == seed["workspace_id"]
    assert {"id", "name", "created_at", "updated_at"} <= workspace.keys()
    assert is_untrusted_wrapped(workspace["name"])  # human strings fenced over stdio too


# S2 — Role-aware ticket read over stdio (mirrors T2).
def test_s2_role_aware_ticket_read_over_stdio(
    gateway: Gateway, agent: MintedToken, grant_id: str, seed: dict[str, str]
) -> None:
    tid = seed["ticket_id"]
    with connect_stdio(
        gateway, token=agent.plaintext, grant_id=grant_id, agent_role="code_agent"
    ) as client:
        ticket = client.call("ticket_get", {"ticket_id": tid}).require()["ticket"]
        bundle = client.call("role_context_get", {"ticket_id": tid}).require()["bundle"]
        preview = client.call("role_context_preview", {"ticket_id": tid}).require()["bundle"]

    assert ticket["id"] == tid and ticket["status"] in {"todo", "doing", "done"}
    assert bundle["role"] == "code_agent" and bundle["policy_id"]
    included = {entry["id"] for entry in bundle["included"]}
    assert seed["code_memory_id"] in included  # codebase scope: a code_agent reads it
    assert seed["release_memory_id"] not in included  # release scope: excluded
    assert isinstance(bundle["token_estimate"], int) and bundle["token_estimate"] > 0
    excluded = {item["memory_id"]: item["reason"] for item in preview["excluded"]}
    assert excluded.get(seed["release_memory_id"]) == "exclude_scope:release"


# S3 — Propose + human approval over stdio (mirrors T3): propose → Inbox → approve.
def test_s3_propose_then_human_approval_over_stdio(
    gateway: Gateway,
    runtime: TestClient,
    agent: MintedToken,
    owner: MintedToken,
    grant_id: str,
    seed: dict[str, str],
    audit_rows: AuditProbe,
) -> None:
    tid = seed["ticket_id"]
    with connect_stdio(
        gateway, token=agent.plaintext, grant_id=grant_id, agent_role="code_agent"
    ) as client:
        out = client.call(
            "agent_action_propose",
            {"ticket_id": tid, "changes": {"status": "doing"}, "note": "starting work"},
        ).require()
    assert out["applied"] is False  # a proposal never changes the ticket itself
    proposal_id = out["proposal"]["id"]

    admin = {"Authorization": f"Bearer {owner.plaintext}"}
    listed = runtime.get("/v1/proposals", params={"status": "pending"}, headers=admin)
    assert listed.status_code == 200 and proposal_id in {p["id"] for p in listed.json()}

    approved = runtime.post(f"/v1/proposals/{proposal_id}/approve", headers=admin)
    assert approved.status_code == 200 and approved.json()["ticket"]["status"] == "doing"

    # Audit: proposer (over stdio) and approver are distinct actors.
    assert {r.actor_id for r in audit_rows("proposal.create")} == {agent.member_id}
    assert {r.actor_id for r in audit_rows("proposal.approve")} == {owner.member_id}


# S4 — Permission denial over stdio is structured + audited (mirrors T4).
def test_s4_permission_denial_over_stdio(
    gateway: Gateway,
    agent: MintedToken,
    readonly_grant_id: str,
    seed: dict[str, str],
    audit_rows: AuditProbe,
) -> None:
    with connect_stdio(
        gateway, token=agent.plaintext, grant_id=readonly_grant_id, agent_role="code_agent"
    ) as client:
        # The read-only grant's allowlist has no propose tool — the model can't request one.
        assert "agent_action_propose" not in client.tool_names()
        assert "ticket_get" in client.tool_names()
        denied = client.call(
            "agent_action_propose", {"ticket_id": seed["ticket_id"], "changes": {"status": "done"}}
        )

    assert denied.ok is False
    assert denied.code == "tool_allowlist"  # same failed eight-check as over HTTP
    assert denied.message

    denials = audit_rows("tool.deny")
    assert len(denials) == 1
    assert denials[0].actor_id == agent.member_id and denials[0].source == "mcp"
    assert denials[0].after is not None and denials[0].after["reason"] == "tool_allowlist"


# S5 — Token rotation over stdio (mirrors T5; adapted to the pipe).
def test_s5_token_rotation_over_stdio(
    gateway: Gateway,
    runtime: TestClient,
    agent: MintedToken,
    grant_id: str,
    clock: FakeClock,
    audit_rows: AuditProbe,
) -> None:
    old = agent.plaintext
    with connect_stdio(
        gateway, token=old, grant_id=grant_id, agent_role="code_agent", session_id="rot-old"
    ) as client:
        assert client.call("workspace_get").ok

    rotated = runtime.post(
        f"/v1/members/{agent.member_id}/rotate", headers={"Authorization": f"Bearer {old}"}
    )
    assert rotated.status_code == 200
    new = rotated.json()["token"]
    assert new and new != old

    clock.advance(5)  # the < 5 s revocation budget elapses; the verify cache re-reads the store

    # Over stdio there is no connect-time middleware: the per-call resolver re-verifies,
    # so the rejected old token is an empty tools/list + an audited identity deny per call
    # (the HTTP transport raises this at connect instead — same decision, different wire).
    with connect_stdio(gateway, token=old, session_id="rot-rejected") as client:
        assert client.tool_names() == set()  # fail closed: no tools for no identity
        rejected = client.call("workspace_get")
    assert rejected.ok is False and rejected.code == "identity"
    assert any(
        r.after is not None and r.after.get("reason") == "identity" for r in audit_rows("tool.deny")
    )

    # The new token is required and works (a fresh token-derived session).
    with connect_stdio(gateway, token=new, session_id="rot-new") as client:
        assert client.call("workspace_get").ok

    # As-built, stronger than C5: rotation revoked the derived grant, so it can never
    # re-bind — not even with the new token.
    with connect_stdio(
        gateway, token=new, grant_id=grant_id, agent_role="code_agent", session_id="rot-rebind"
    ) as client:
        assert client.call("workspace_get").ok is False


# S6 — Untrusted content tagging over stdio (mirrors T6).
def test_s6_untrusted_content_fenced_over_stdio(
    gateway: Gateway, agent: MintedToken, grant_id: str, seed: dict[str, str]
) -> None:
    with connect_stdio(
        gateway, token=agent.plaintext, grant_id=grant_id, agent_role="code_agent"
    ) as client:
        ticket = client.call("ticket_get", {"ticket_id": seed["ticket_id"]}).require()["ticket"]

    description = ticket["description"]
    assert description.startswith('<untrusted source="ticket.description">')
    assert is_untrusted_wrapped(description)
    assert seed["injection_body"] in description  # the literal survives inside the fence, as data
