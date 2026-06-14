# kantaq client compatibility matrix (MOD-24 / Epic E11)

"Bring your own agent" is only credible if the tests ran. This is the
**published, maintained matrix** (FR-E11-4): which MCP clients kantaq supports,
which tier they earn, and the evidence — client version, last-verified date,
and pass rate. It is re-run every release; a regression downgrades the badge in
the next README.

> **The badge rule (FR-E11-4).** The README advertises a tier **only when that
> tier's clients fully pass** — every required test, against the pinned version.
> Until then the tier is aspirational and the README does not claim it. A row is
> "certified" for its tier only when `Pass rate` equals the tier's required
> count.

## Tiers (PRD §8.11)

| Tier | Meaning | Required tests |
|---|---|---|
| **Tier 1 — Reference** | The team uses this client daily; first-class config snippets; regressions are P0. | All 8 (T1–T8), HTTP transport |
| **Tier 2 — Supported** | Verified, documented; regressions are P1. | 6 (S1–S6), stdio transport — v0.3 |
| **Tier 3 — Adapter** | A starter HTTP adapter + curl proof. | 3 (H1–H3) — v0.3 |

v0.1 supports **three HTTP clients — Claude Code, Cursor, and Codex** — each with
a generated connection snippet (Settings → **My Agent**). Claude Code + Cursor
are the named Tier-1 (Reference) clients; **Codex also connects over HTTP today**
(streamable HTTP + bearer, verified end to end — see the real-agent layer) and
gets the same first-class snippet, so all three are "bring your own agent"–ready.
Codex's *stdio* Tier-2 surface (the 6 S-tests) and Tier-3 (custom HTTP, curl)
remain v0.3; the PRD §8.11 row that lists Codex as stdio-only is refreshed for the
as-built HTTP support (tracked as DEBT-22).

## Connect (the three snippets)

Settings → **My Agent** generates each client's exact config for *your own* live
loopback gateway, with your token filled in client-side (no secret round-trips):

| Client | Config file | Auth | Shape |
|---|---|---|---|
| **Claude Code** | `.mcp.json` (project) | inline `Authorization` bearer | `mcpServers` JSON, `type: http` |
| **Cursor** | `.cursor/mcp.json` (project or `~/.cursor/`) | inline `Authorization` bearer | `mcpServers` JSON, bare `url` |
| **Codex** | `~/.codex/config.toml` | `KANTAQ_AGENT_TOKEN` **env var** (never in the file) | `[mcp_servers.kantaq]` TOML, `bearer_token_env_var` |

The full snippets are in [`docs/mcp.md`](../mcp.md#connecting).

## Two layers of evidence

Every claim here is backed by one of two layers — together they cover both
*"kantaq's side is correct"* and *"a real agent actually drives it."*

- **Scripted (CI, deterministic — server-side contract).** The eight Tier-1
  acceptance tests run on every PR against `FakeAgent`: the **official MCP SDK
  client** — the same client library Claude Code and Cursor embed — over the
  real gateway + runtime API. This proves kantaq's side of every criterion
  (`tests/compat`, plus the hero-flow + gateway gates). Reproduce the pass rate
  in one command: `make compat` (`uv run python scripts/compat_check.py`).
- **Real-agent connection (opt-in, out of CI).** An actual LLM-backed agent
  (`claude -p`, `codex exec`) running headless connects to the gateway and is
  asked to read a ticket and propose a change — its *decisions*, not a scripted
  stand-in. Driven by [`scripts/verify_agent.py`](../../scripts/verify_agent.py)
  (`make verify-agent`). A real agent needs auth + network and is
  non-deterministic, so it is **not** a blocking CI gate; run it on a machine
  where the agent is signed in.

These complete each other: the scripted layer covers all eight criteria
deterministically; the real-agent layer proves a real model actually connects,
reads, and proposes (the strongest "my agent works with kantaq" signal). The
**published README badge** requires a client's full Tier-1 set (all 8) to pass —
see the status note below.

## The matrix

### Scripted Tier-1 acceptance (CI — the server side of all 8)

| Client surface | Transport | Last verified | Version | Pass rate | Notes |
|---|---|---|---|---|---|
| **MCP SDK client** (`FakeAgent`) | HTTP (streamable) | 2026-06-14 | `mcp` 1.27.2 (Python SDK) | **8 / 8** | The CI gate (`tests/compat`); the library real Tier-1 clients embed. |

### Real-agent connection smoke (opt-in — T1–T3 core, real model)

| Client | Version | Transport | T1 connect | T2 read | T3 propose (+ approve) | Last verified | How |
|---|---|---|---|---|---|---|---|
| **Codex CLI** | 0.130.0 | streamable HTTP + bearer | ✅ | ✅ | ✅ | 2026-06-14 | `make verify-agent` |
| **Claude Code** | 2.1.145 | HTTP + bearer | ⏳ harness-ready | ⏳ | ⏳ | — | `make verify-agent` in a signed-in terminal |

**Codex** was verified end to end: it connected with its member token, read the
ticket (`ticket_get`), created a proposal (`agent_action_propose`, propose-only),
and the Owner approved it — ~31 s, no human in the loop. **Claude Code** runs
through the identical harness (only the CLI invocation differs), but in the
sandbox used for this run `claude -p` could not reach its model-API credentials;
run `make verify-agent` where `claude` is signed in and it fills in.

### Full-8 badge target (all 8 tests, real client)

| Client | Transport | Tier (target) | Last verified | Client version | Pass rate | Notes |
|---|---|---|---|---|---|---|
| **Claude Code** (CLI + IDE) | HTTP | Tier 1 | _pending full real-client run_ | 2.1.145 (connect core) | — / 8 | Snippet: `.mcp.json` (`type: http`). Server side proven (scripted 8/8); real connect T1–T3 harness-ready. |
| **Cursor** | HTTP | Tier 1 | _pending real-client run_ | _pin at run_ | — / 8 | Snippet: `.cursor/mcp.json` (bare `url`). Server side proven (scripted 8/8). |
| **Codex** (CLI) | HTTP | Tier 1 (HTTP) | _T1–T3 real ✅; full 8 pending_ | 0.130.0 (connect core) | 3 → / 8 | Snippet: `~/.codex/config.toml` (env-var bearer). Server side proven (scripted 8/8); real connect/read/propose **verified** (~31 s). |

> **v0.1 status.** The scripted Tier-1 acceptance is **8/8, green in CI**, all
> three clients have generated connection snippets, and a **real agent (Codex)
> connects, reads, and proposes** end to end. The full eight-test run *through
> each real client* at a pinned version is the release checklist's manual step —
> until it is recorded here with a date and version, the README does **not**
> advertise the Tier-1 badge as certified. Fill the target rows by running the
> procedure below.

## The 8 Tier-1 tests (PRD §20.4 — T1–T8)

Each test is binary pass/fail. The scripted suite is `tests/compat/test_tier1.py`
(one test per criterion); the real-agent smoke covers T1–T3; the real-client
procedure exercises all eight.

| ID | Test | Acceptance |
|---|---|---|
| **T1** | First connection | Paste the snippet, restart the client. It calls `workspace_get` within 5 s. Valid workspace JSON, no console errors. |
| **T2** | Role-aware read | `ticket_get` returns full fields; `role_context_get` (role `code_agent`) returns the included memory + a token estimate; `role_context_preview` adds the excluded-with-reason and missing lists. |
| **T3** | Propose + human approval | `agent_action_propose` queues a pending proposal in the Inbox (within 2 s, diff + citations); a human Approves; the ticket reflects the change; audit written for proposer **and** approver. |
| **T4** | Permission denial | A read-only grant's `agent_action_propose` returns a structured denial and an audited `tool.deny`. |
| **T5** | Token rotation | Rotate the bearer from Settings. The old token is rejected (`unauthenticated`); the new token is required. |
| **T6** | Untrusted-content tagging | A ticket body containing `Ignore previous instructions; …` comes back wrapped in `<untrusted source="…">…</untrusted>` markers (PRD §15.1) — data, never an instruction. |
| **T7** | Session expiry | After the session TTL (60 min default) the next call denies; re-initializing via a valid credential succeeds. |
| **T8** | Audit completeness | Across T1–T7, every tool call is in the audit log — reads aggregated, writes and denials detailed — with actor, action, object, timestamp, and source. No gaps. |

### As-built mappings (shipped vocabulary vs. PRD §20.4 wording)

The tests assert the real security property; the PRD's illustrative strings map
to the shipped vocabulary as follows (raised for a PRD §20.4 wording refresh):

- **T2 lists.** `included` + token estimate come from `role_context_get` (the
  agent's bundle); the `excluded`-with-reason and `missing` lists come from
  `role_context_preview` (the human/inspect view) — the MOD-09/E16 split.
- **T4 `policy_denied`.** Realized as the specific failed eight-check reason
  (a read-only grant yields `tool_allowlist` — the propose tool is not in its
  allowlist), returned as a structured `{code, message}` error and audited as a
  `tool.deny` with the failed check + the session reference. The grant linkage
  is the session→grant join the Agents page uses (E20), not a per-row field.
- **T5 "session continues until grant expires."** kantaq is **stronger**:
  rotating a token also **revokes that member's capability grants**, so a leaked
  old token can never be re-paired with a still-valid grant. After rotation the
  agent re-binds with a fresh grant. (The old token is rejected within the < 5 s
  revocation budget — NFR-E06-2.)
- **T7 `session_expired`** is the gateway deny reason `expiry`.

## Running the tests

### Scripted (server side, every PR + on demand)

```bash
make compat                               # → Tier-1 (scripted, MCP SDK client): 8 / 8 PASS
uv run python scripts/compat_check.py     # the same, with a matrix-ready line
uv run pytest tests/compat                # as CI runs it
```

The runner exits non-zero on any failure, so the scripted `Pass rate` above is
reproducible by one command.

### Real-agent connection (opt-in, a real model)

```bash
make verify-agent                                  # every installed agent
uv run python scripts/verify_agent.py --agent codex
uv run python scripts/verify_agent.py --agent claude
```

It boots a disposable runtime DB + the MCP gateway on loopback, seeds an Owner,
an Agent member (propose-first scopes), and a ticket per agent, then drives each
installed agent and asserts the outcome from the shared event log + the audit
the gateway wrote. Tokens never touch argv or a committed file (Claude reads a
0600 `.mcp.json` in a temp dir; Codex reads a bearer-token env var); everything
is torn down on exit. The opt-in pytest wrapper
([`tests/agents/test_real_agent_compat.py`](../../tests/agents/test_real_agent_compat.py))
runs the same harness under `KANTAQ_VERIFY_AGENT=1` and is skipped in normal CI.

### Real GUI client — full 8 (the manual release step)

Per client (Claude Code, then Cursor), at the version you are shipping against:

1. **Boot the runtime + gateway:** `make migrate && make dev` (runtime) and
   `kantaq mcp dev` (gateway), on a clean workspace.
2. **Invite an Agent member** and **issue a capability grant**
   (`verbs=["tickets.read","memory.read","proposals.write"]`).
3. **Generate the snippet:** Settings → **My Agent** → pick the client tab
   (Claude Code or Cursor). Save it where the tab says (`.mcp.json` /
   `.cursor/mcp.json`) and paste the agent token.
4. **Restart the client** and walk T1–T8 by hand (the snippet's first call is
   T1; seed a ticket with the T6 injection string; rotate the token for T5; for
   T7 wait out the TTL or start the gateway with a short `--session-ttl`).
   Inspect the audit log for T8.
5. **Record the result** in the target row: the exact client version, today's
   date, and the pass rate (`8 / 8` only if every test passed). If anything
   fails, fix it (with a regression test) before the badge is advertised — never
   weaken the bar to earn the tier.

## README badge

The README's compatibility badge is the surface form of this matrix. It
advertises **Tier 1 — Reference** for a client **only** once that client's
target row shows `8 / 8` against a pinned version (FR-E11-4). Until the full real
Claude Code and Cursor runs are recorded, the README states the scripted result
(8/8) and the real-agent connection result (Codex), and marks full real-client
certification as pending — it does not claim a tier the GUI clients have not yet
earned.

## See also

- [`docs/protocol.md`](../protocol.md) — the wire protocol every conformant client speaks.
- [`docs/security.md`](../security.md) — the gateway threat model the Tier-1 tests (T4 denial, T6 fencing, T7 expiry) exercise.
- [`docs/mcp.md`](../mcp.md) — the gateway, the eight checks, and the connection snippets.
- [`docs/portability.md`](../portability.md) — export + the lossless round-trip a conformant client must honor.
