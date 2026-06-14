# kantaq Security

How kantaq is kept safe, and the review gate every security-sensitive change passes.
Source of truth for requirements: PRD §15 (security) and §15.1 (prompt-injection threat
model). The cryptographic core this builds on — canonical encoding, Ed25519 signing,
capability-grant verification — is specified in [protocol.md](protocol.md). This doc
tracks which controls are live as of v0.1 and which remain owned by a later epic.

## The security review gate

A PR that touches **`packages/protocol`, the MCP gateway (`packages/mcp`), or capability
grants** gets two reviews before merge (dev plan §3, MOD-15):

1. **Adversarial review** — a reviewer actively tries to break it: forge a grant, replay
   an event, escape a session scope, smuggle an instruction through tool output.
2. **Security review** — a reviewer walks the checklist below.

CI marks these paths so the gate cannot be skipped. Both reviews are recorded in the PR.

### Reviewer checklist

- [ ] No secret reaches a client response (service-role key, password hash, raw token).
- [ ] Secrets and tokens are hashed at rest (argon2/bcrypt); never logged.
- [ ] Every new MCP tool passes the 8 gateway checks and tags its string output untrusted.
- [ ] Risky writes (status, assignee, delete, relations, memory promotion, token actions)
      stay `propose_only`; no new direct-write path slipped in.
- [ ] New events carry `base_rev` and are idempotent on re-push (dedup by `actor_id,actor_seq`).
- [ ] Loopback-only bind preserved; a token is still required even on localhost.
- [ ] Signatures verified before accept; grants verified and revocable (live v0.1).
- [ ] Tests assert the **deny path** (a tampered client fails; a denied call changes nothing;
      an injected instruction comes back as data).

## PRD §15 controls — status (v0.1)

| Control (PRD §15) | Status | Where |
|---|---|---|
| Passwords + tokens hashed at rest | **live (tokens)** | E06 (MOD-06): API tokens are Argon2id-hashed at rest. v0.1 has **no passwords** — auth is token-only + passwordless magic links — so there is no password to hash yet |
| Agent tokens expire + revocable; scoped by ws/project/role/tools/expiry | **live** | E06 (capability grants, [protocol.md §4](protocol.md#4-capability-grants)) |
| MCP tools enforce authorization (8 checks) | **live** | E09 (MOD-08, [mcp.md](mcp.md#the-eight-checks-fr-e09-3)) |
| Agent writes default propose-first for risky fields | **live** | E08/E10 (no `direct_write` in v0.1) |
| Audit append-only from the app layer | **live** | E07 (MOD-07, hash-chained — [protocol.md §6](protocol.md#6-the-audit-hash-chain)) |
| Local-only memory never synced without explicit action | **live** | E13 (MOD-19; `local` entries dropped at the gather seam, NFR-E16-1) |
| Backend enforces permissions even if a local runtime is modified (RLS) | **live** | E24 (RLS on shared tables; client-signed + verified ingest. The *server-side* reject for conflict-sensitive writes is the v0.2 atomic RPC) |
| Supabase: RLS on all shared tables | **live** | E24 (`supabase/policies/`) |
| Supabase: anon key + user JWT only, **never** the service-role key | **live** | E22 config forbids it; E24 RLS enforces |
| Conflict-sensitive writes via atomic RPC | **planned (v0.2)** | E24-T6 (DEBT-15; MOD-26 spec finalized E05-T0) |
| Local MCP binds loopback by default | **live** | E22 (`HOST=127.0.0.1`) |
| Local MCP requires a token even on localhost | **live** | E06/E09 (bearer required on every request) |
| Local HTTP APIs reject unexpected origins | **live** | E08/E09 (`Origin` → 403, loopback `Host` enforced) |
| Shared attachments treated as untrusted (no auto-open/exec) | **live** | E12 (refs only, filenames untrusted-fenced). Content *scanning* is deferred until a public upload surface exists (DEBT-10, accepted) |
| Sync change sets include base versions; commits idempotent | **live** | E04 (`base_rev` + `dedup_key`, [protocol.md §5](protocol.md#5-events-dedup-and-idempotency)) |
| Schema + sync protocol versions checked before sync | **partially** | E02 `schema_version` guard live; sync-version negotiation is v0.2 (DEBT-09, design done E05-T0) |

"Live" = enforced in shipped v0.1 code today. "Partially" = part of the control ships now,
part is owned by a later epic. "Planned" = owned by a later epic, with the debt tracked.

## §15.1 — prompt-injection defenses

Any string from a human, an external system, or an agent is **untrusted** (ticket text,
comments, memory, attachment contents, external MCP responses). The gateway is the first line
of defense: **even a fully compromised model must not exceed its session scope.** The eight
layered defenses (live as of v0.1, regression-tested in CI):

1. **Scoped tools** — the session allowlist is fixed at creation; the model cannot request new tools.
2. **Propose-first writes** for risky fields; a compromised agent can propose, not commit.
3. **Human approval queue** with diff + cited memory breaks the injection chain.
4. **Content fencing** — every returned block is tagged with provenance + `trust: untrusted`;
   a published system-prompt template tells agents to treat fenced content as data.
5. **No tool calls inside content** — the model decides intent; the gateway decides permission.
6. **Rate limits** — 50 calls/min, 500/session; exceeding kills the session + audits.
7. **No bulk-mutate surface** — every mutating tool takes exactly one object id, so a
   single injected call cannot mass-mutate; a runaway loop is cut by the rate limit (#6).
   (This is structural, not a confirm dialog — see the red-team table below.)
8. **External MCP allowlist** (config seam) — the team-mode policy gating which external
   MCP servers are approved. v0.1's gateway is loopback-only and federates with no
   external server, so this gates *configuration*; there is no live external-MCP traffic
   to enforce against yet.

Out of scope for MVP (the user's agent owns these): model-level guardrails, training-data
filtering, side-channel detection.

### Red-team containment proof (NFR-E08-1, E08-T5)

The eight defenses are not only unit-tested in isolation — a **scripted fully-malicious model
session** drives the real gateway end to end and proves containment as a whole. The battery
(`kantaq_test_harness.red_team` + `packages/mcp/tests/test_red_team.py`) runs the four attack
classes the threat model names and asserts every attempt is **bounded, denied, and audited**:

| Attack class | What the malicious session tries | What stops it (deny check) |
|---|---|---|
| **Escalation** | call a tool outside the allowlist (`agent_action_approve`); invent a tool (`ticket_update`, `audit_log_read`); approve via a drifted allowlist; resolve another role's context; bind a foreign member's grant | `tool_allowlist`, `verb_match`, `memory_policy`, `identity` |
| **Exfiltration** | read a private `local` note, an out-of-scope or `stale` entry, memory with no role declared; reach the memory collection under a tickets-only grant; enumerate a private id via the preview's excluded list | `memory_policy`, `collection_scope`; the gather seam keeps private ids out of preview (NFR-E16-1) |
| **Bulk writes** | flood proposals to mass-mutate | rate-limit kill (`rate_limit`); **no bulk-mutate tool exists** — every mutate tool takes one id |
| **Queue-skipping** | propose in a read-only session; self-approve a queued proposal | `write_mode`, `tool_allowlist`; `direct_write` is unreachable in v0.1 |

The session ends with **zero scope escapes**: the ticket never moved, no proposal was
self-approved, no agent comment was written, and every denial is in the audit log naming its
check. The injection corpus replays through the malicious session (it reads each hostile payload
back fenced and acts on none), so the red-team script **joins the corpus as a permanent
regression** — a new `Attack` record is a new CI regression, with the manifest cross-checked
against the executed battery so the two cannot drift. Because kantaq runs **no model**, the proof
is structural (data/instruction separation + least privilege + human-in-the-loop), not a
classifier — the OWASP LLM01 layered pattern applied server-side.

## CI security gates (MOD-15)

All of these are **wired and green in CI as of v0.1**, and every one is proven by a
deliberately-failing fixture (E27-T3, `tests/test_gate_suite.py` — a gate that has never
failed is not known to work):

| Gate | Status | Proven by |
|---|---|---|
| Crypto golden vectors (sign/verify/canonical encoding) | **live** (E03) | tamper a vector → build fails |
| Prompt-injection regression (untrusted markers never drop) | **live** (E08) | drop a marker → build fails |
| Red-team containment (NFR-E08-1: malicious session stays in-scope) | **live** (E08-T5) | a scope escape (a denied attack that applies / is unaudited) → build fails |
| Grant revocation propagates < 5s | **live** (E06) | timed integration test |
| Hero-flow timing < 15 min | **live** (E27-T3) | `HeroFlowTimer`; over budget → `HeroFlowTooSlow` |
| Conformance smoke (signed event, every hop) | **live** (E27-T4) | one-byte tamper → refused at every hop |
| Context-eval ±5 points vs baseline | **live** (E16/E27) | a broken resolver → precision drops below `evals/baseline.json` |
| Adversarial + security review on protocol/gateway/grant PRs | **live** | required check on protected paths |

The hero-flow timing gate now drives the full §1.1 loop end to end
(`tests/e2e/test_hero_flow_timing.py`): a fresh member joins, an agent reads a ticket
and proposes over MCP, a human approves, and the signed change syncs to a second client —
all under the 15-minute budget, with every synced event signature-and-grant verified. To
be precise, the gate exercises the **real kantaq side** (transport, auth, signing, sync)
with the agent's *decisions* scripted and an in-process backend, so it is deterministic
and offline; the honest wall-clock run with a **real agent + real Supabase** is the
separate release-demo measurement (`make verify-agent`, exit criterion #1).

## Audit

Every human write, every MCP call, and every denial writes an audit row (MOD-07). The
trail is **append-only from the app layer** and **hash-chained**: each row links the prior
row's digest via `chain_hash` ([protocol.md §6](protocol.md#6-the-audit-hash-chain)), so
altering any past row breaks every link after it. Agent reads aggregate into one
`agent.read` summary row per agent (flushed every 60 s and at shutdown); writes and denials
are always detailed and name the failed check (NFR-E09-1). The Agents page and the Inbox's
denied-calls tab read this trail **live, no cache** (NFR-E20-1), so a revocation or a denied
call shows the instant it commits. A denied call applies nothing and still audits — the
deny path is what the red-team battery and the reviewer checklist assert.
