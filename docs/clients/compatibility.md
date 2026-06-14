# Client compatibility

Which coding agents are verified to connect to kantaq's loopback MCP gateway and
drive the propose-first flow — **because the tests ran**, against a real agent,
not a scripted stand-in.

Two layers test the agent connection:

- **Server-side contract (CI, deterministic):** the hero-flow gate and the MCP
  gateway tests drive the gateway with the *real MCP SDK client* and assert
  kantaq's side — transport, bearer auth, the tool catalog, scopes, audit,
  signed events, approval, signed sync. This runs on every PR.
- **Real-agent connection (this matrix, opt-in):** an actual LLM-backed agent
  (`claude -p`, `codex exec`), running headless, connects to the gateway and is
  asked to read a ticket and propose a change. Driven by
  [`scripts/verify_agent.py`](../../scripts/verify_agent.py) (`make verify-agent`).
  A real agent needs auth + network and is non-deterministic, so it is **not** a
  blocking CI gate; you run it on a machine where the agent is signed in.

## Real-agent connection smoke (T1–T3 core)

| Client | Version | Transport | T1 connect | T2 read a ticket | T3 propose (+ human approve) | Last verified | How |
|---|---|---|---|---|---|---|---|
| **Codex CLI** | 0.130.0 | streamable HTTP + bearer | ✅ | ✅ | ✅ | 2026-06-14 | `make verify-agent` |
| **Claude Code** | 2.1.145 | HTTP + bearer | ⏳ harness-ready | ⏳ | ⏳ | — | `make verify-agent --agent claude` in a signed-in terminal |

**Codex** was verified end to end: it connected to the gateway with its member
token, read the ticket (`ticket_get`), created a proposal (`agent_action_propose`,
propose-only), and the Owner approved it — ~31 s, no human in the loop.

**Claude Code** runs through the identical harness path (only the CLI invocation
differs), but in the sandbox used for this run `claude -p` could not reach its
model-API credentials (it exited before contacting kantaq). Run
`make verify-agent` in a terminal where `claude` is signed in and it fills in.

## What this is and isn't

- This is the **connection core**: T1 (connect ≤ 5 s), T2 (read), T3 (propose +
  the human-approval invariant). It is the strongest signal that "my agent works
  with kantaq."
- It is **not** the full 8-test Tier-1 suite. T4 permission denial, T5 token
  rotation, T6 untrusted-content tagging, T7 session expiry, and T8 audit
  completeness — and the **published README badge** — are E11-T2 / E11-T3. The
  README advertises a tier only when **all** of its tests fully pass.

## Run it yourself

```bash
make verify-agent                                  # every installed agent
uv run python scripts/verify_agent.py --agent claude
uv run python scripts/verify_agent.py --agent codex
```

It boots a disposable runtime DB + the MCP gateway on loopback, seeds an Owner,
an Agent member (propose-first scopes), and a ticket per agent, then drives each
installed agent and asserts the outcome from the shared event log + audit the
gateway wrote. Tokens never touch argv or a committed file (Claude reads a 0600
`.mcp.json` in a temp dir; Codex reads a bearer-token env var); everything is
torn down on exit. The opt-in pytest wrapper
([`tests/agents/test_real_agent_compat.py`](../../tests/agents/test_real_agent_compat.py))
runs the same harness under `KANTAQ_VERIFY_AGENT=1` and is skipped in normal CI.
