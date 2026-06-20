"""Tier-2 compatibility acceptance suite: S1–S6 = the **stdio** versions of the
Tier-1 T1–T6 (E11-T4, MOD-24/MOD-30, PRD §20.4).

Tier-2 is Codex over the gateway's stdio transport. S1–S6 mirror T1–T6 with only
the transport swapped — a denial over stdio must be byte-for-byte the decision it
is over HTTP. (The *exhaustive* deny-matrix + audit-completeness over stdio is
the gateway's own E09-T4 acceptance, not this client-compat subset; here S4
re-checks the one structured denial a Tier-2 client must see, like T4.)

**Sequenced after E09-T4 (the stdio MCP transport).** Until it lands, this suite
**skips**: the structure + the matrix row are prepped (E11-T4) so it flips on the
moment the transport and the stdio harness seam are wired — see
``kantaq_test_harness.stdio``. The real Codex run (pinned 0.130.0) is the manual
release step recorded in ``docs/clients/compatibility.md``, like Tier-1.

When wiring lands: implement ``connect_stdio`` (env-var grant binding over the
pipe), set ``_HARNESS_STDIO_WIRED = True``, and finalize each body against the
named T-analog — the fixtures (``seed``/``agent``/``grant_id``/``gateway_app``)
are the shared Tier-1 conftest, reused unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from starlette.applications import Starlette

from kantaq_core.identity import MintedToken
from kantaq_test_harness.stdio import connect_stdio, stdio_transport_ready

AppFactory = Callable[[], Starlette]

pytestmark = pytest.mark.skipif(
    not stdio_transport_ready(),
    reason="Tier-2 stdio transport (E09-T4) not landed — S1–S6 fixtures prepped, "
    "enable when the transport + the stdio harness seam are wired (E11-T4).",
)


@pytest.fixture
def stdio_agent(gateway_app: AppFactory) -> Iterator[Callable[..., object]]:
    """A real agent over the SDK stdio transport — the Tier-2 analog of the
    Tier-1 ``FakeAgent``. A factory so each S-test opens its own session with the
    grant/role it needs, exactly like the HTTP suite. Inert while the suite
    skips (``connect_stdio`` raises until E09-T4 is wired)."""

    def _open(token: str, **binding: object) -> object:
        return connect_stdio(gateway_app(), token=token, **binding)

    yield _open


# S1 — First connection over stdio (mirrors T1): launch, first call, fast.
def test_s1_first_connection_over_stdio(
    stdio_agent: Callable[..., object], agent: MintedToken, seed: dict[str, str]
) -> None:
    with stdio_agent(agent.plaintext) as client:  # type: ignore[attr-defined]
        workspace = client.call("workspace_get").require()["workspace"]
    assert workspace["id"] == seed["workspace_id"]


# S2 — Role-aware ticket read over stdio (mirrors T2).
def test_s2_role_aware_ticket_read_over_stdio(
    stdio_agent: Callable[..., object],
    agent: MintedToken,
    grant_id: str,
    seed: dict[str, str],
) -> None:
    with stdio_agent(agent.plaintext, grant_id=grant_id, agent_role="code_agent") as client:  # type: ignore[attr-defined]
        bundle = client.call("role_context_get", {"ticket_id": seed["ticket_id"]}).require()[
            "bundle"
        ]
    assert bundle["role"] == "code_agent"
    assert seed["code_memory_id"] in {entry["id"] for entry in bundle["included"]}


# S3 — Propose + human approval over stdio (mirrors T3): propose → Inbox → approve.
def test_s3_propose_then_human_approval_over_stdio() -> None:
    pytest.skip("finalize against E09-T4: mirror test_t3 with the stdio agent")


# S4 — Permission denial over stdio is structured + audited (mirrors T4).
def test_s4_permission_denial_over_stdio(
    stdio_agent: Callable[..., object], agent: MintedToken, readonly_grant_id: str
) -> None:
    with stdio_agent(
        agent.plaintext, grant_id=readonly_grant_id, agent_role="code_agent"
    ) as client:  # type: ignore[attr-defined]
        denied = client.call("agent_action_propose", {"ticket_id": "x", "changes": {}})
    assert denied.ok is False
    assert denied.code == "tool_allowlist"  # same reason as over HTTP (T4)


# S5 — Token rotation over stdio (mirrors T5).
def test_s5_token_rotation_over_stdio() -> None:
    pytest.skip("finalize against E09-T4: mirror test_t5 with the stdio agent")


# S6 — Untrusted content is fenced over stdio (mirrors T6).
def test_s6_untrusted_content_fenced_over_stdio(
    stdio_agent: Callable[..., object], agent: MintedToken, grant_id: str, seed: dict[str, str]
) -> None:
    from kantaq_test_harness.compat import is_untrusted_wrapped

    with stdio_agent(agent.plaintext, grant_id=grant_id, agent_role="code_agent") as client:  # type: ignore[attr-defined]
        ticket = client.call("ticket_get", {"ticket_id": seed["ticket_id"]}).require()["ticket"]
    assert is_untrusted_wrapped(ticket["description"])  # the injection body stays fenced
