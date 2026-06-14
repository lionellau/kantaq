# kantaq Security

How kantaq is kept safe, and the review gate every security-sensitive change passes.
Source of truth for requirements: PRD §15 (security) and §15.1 (prompt-injection threat
model). This doc tracks which controls are live and which land with their epic.

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
- [ ] (v0.1+) Signatures verified before accept; grants verified and revocable.
- [ ] Tests assert the **deny path** (a tampered client fails; a denied call changes nothing;
      an injected instruction comes back as data).

## PRD §15 controls — status

| Control (PRD §15) | Status | Where |
|---|---|---|
| Passwords + tokens hashed at rest | **planned** | E06 (MOD-06) |
| Agent tokens expire + revocable; scoped by ws/project/role/tools/expiry | planned | E06 |
| MCP tools enforce authorization (8 checks) | planned | E09 (MOD-08) |
| Agent writes default propose-first for risky fields | planned | E08/E10 |
| Audit append-only from the app layer | planned | E07 (MOD-07) |
| Local-only memory never synced without explicit action | planned | E13 |
| Backend enforces permissions even if a local runtime is modified (RLS) | planned | E24 (MOD-05) |
| Supabase: RLS on all shared tables | planned | E24 |
| Supabase: anon key + user JWT only, **never** the service-role key | **partially live** | E22 config forbids it; E24 enforces |
| Conflict-sensitive writes via atomic RPC | planned (v0.2) | E24 |
| Local MCP binds loopback by default | **live** | E22 (`HOST=127.0.0.1`) |
| Local MCP requires a token even on localhost | planned | E06/E09 |
| Local HTTP APIs reject unexpected origins | planned | E08 |
| Shared attachments treated as untrusted (no auto-open/exec) | planned | E12 (DEBT-10) |
| Sync change sets include base versions; commits idempotent | **modeled** | FakeBackend (MOD-30); E04 makes it real |
| Schema + sync protocol versions checked before sync | partially | E02 `schema_version` guard; sync version E05 |

"Live" = enforced in shipped code today. "Modeled" = the harness (`FakeBackend`) encodes the
contract so the real implementation has a target and a test. "Planned" = owned by a later epic.

## §15.1 — prompt-injection defenses

Any string from a human, an external system, or an agent is **untrusted** (ticket text,
comments, memory, attachment contents, external MCP responses). The gateway is the first line
of defense: **even a fully compromised model must not exceed its session scope.** The eight
layered defenses (land with E08/E09/E10, regression-tested in CI):

1. **Scoped tools** — the session allowlist is fixed at creation; the model cannot request new tools.
2. **Propose-first writes** for risky fields; a compromised agent can propose, not commit.
3. **Human approval queue** with diff + cited memory breaks the injection chain.
4. **Content fencing** — every returned block is tagged with provenance + `trust: untrusted`;
   a published system-prompt template tells agents to treat fenced content as data.
5. **No tool calls inside content** — the model decides intent; the gateway decides permission.
6. **Rate limits** — 50 calls/min, 500/session; exceeding kills the session + audits.
7. **Bulk-action confirm** — any call affecting > 1 object needs a second confirm.
8. **External MCP allowlist** — team mode approves external servers at the workspace level.

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

| Gate | Lands | Proven by |
|---|---|---|
| Crypto golden vectors (sign/verify/canonical encoding) | v0.1 (E03) | tamper a vector → build fails |
| Prompt-injection regression (untrusted markers never drop) | v0.1 (E08) | drop a marker → build fails |
| Red-team containment (NFR-E08-1: malicious session stays in-scope) | v0.1 (E08-T5) | a scope escape (a denied attack that applies / is unaudited) → build fails |
| Grant revocation propagates < 5s | v0.1 (E06) | timed integration test |
| Hero-flow timing < 15 min | now (stub) → v0.1 | `HeroFlowTimer`; over budget → fail (`test_hero_flow.py`) |
| Adversarial + security review on protocol/gateway/grant PRs | now | required check on protected paths |

The hero-flow timing gate is wired today as a stub (`tests/e2e/test_hero_flow_timing.py`) and
expands to the full §1.1 loop as MCP, tickets, and approval land.
