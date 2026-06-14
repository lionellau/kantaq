# We stopped paying for Linear

We are a small team. For a while we paid per seat for a hosted issue tracker —
the usual one, fast and well-designed. This is not a complaint about that
product. It is about what changed underneath us, and why a per-seat hosted
tracker stopped being the right shape for how we actually work.

## The bill was not the problem

The bill was annoying, not fatal. Five seats is not a lot of money. If cost were
the whole story we would have switched to a cheaper tracker and moved on.

The problem was that **our AI agents had become real contributors, and the
tracker had no honest place for them.**

We run Claude Code, Cursor, and Codex every day. They read the codebase, they
understand a ticket, they do the work. But in a hosted tracker an agent is a
second-class citizen wearing a human's badge: it authenticates as *you*, over a
broad API token, and anything it writes is indistinguishable from something you
wrote. So you do one of two things. You give the agent your credentials and hope
it never does anything you would not have — or you keep it out, and copy-paste
context in and decisions out by hand. We did both, on different days, and neither
felt right.

What we wanted was an agent that could **propose** — read a ticket with the right
context, suggest a status change or a comment or a new sub-task — and a human who
**approves** from a diff. Attributed. Audited. Reversible. The hosted tracker
could not give us that, because the trust model it was built on predates agents.

## The second thing: it was not ours

The other quiet problem: our project lived on someone else's server. Export was a
CSV dump, not a guarantee. If we wanted to leave, we left most of the structure
behind. For a tool that holds the spine of how a team works, "trust us, it's in
the cloud" is a weak answer.

We wanted the project to live **on our machines**, in a format we could read,
verify, and carry somewhere else without losing a byte.

## So we built kantaq

[kantaq](https://github.com/lionellau/kantaq) is a local-first, agent-native
issue tracker for small teams who already run a local AI agent. The shape is
different on purpose:

- **It runs on your machine.** Solo, with zero backend. As a team, every member
  runs their own copy and points it at one shared sync backend (Supabase today,
  self-hosted Postgres later) that only validates and stores committed events.
  There is no shared application instance for anyone to operate, monetize, or
  lose.

- **Agents are first-class — and propose-first.** Your agent connects to a
  loopback gateway on your own machine, never to the backend and never to a
  teammate's machine. It reads tickets with role-aware context and *proposes*
  changes. A human approves them from an Inbox that shows a field-level diff and
  the memory the agent cited. A compromised agent can propose; it cannot commit.
  Every call — and every denial — lands in an audit log.

- **Every change is signed.** Each synced event is Ed25519-signed by the device
  that made it and grant-verified before it is accepted. A modified client cannot
  forge a teammate's change. The whole thing is one small, documented protocol
  ([read it](../protocol.md)) — not a black box.

- **You can leave.** The workspace exports to a single deterministic file and
  re-imports losslessly — byte-identical event logs, identical snapshots,
  verified blob hashes. Portability is a protocol primitive, not a support
  ticket.

We did not set out to undercut anyone on price. We set out to fix the trust model
for a world where half the team is an agent. Not paying per seat is a side
effect.

## What it is not

Honesty matters more than the pitch, so: kantaq is for **small teams (2–10)**, it
is **v0.1**, and it assumes you already run a local agent. It is not a hosted
SaaS, not an enterprise rollout, not a Jira replacement for a 200-person org. It
will not import your existing Linear project yet. Telemetry is opt-in and
local-only. If you want a polished cloud product with a support contract, the one
we used to pay for is genuinely good — go use it.

But if you are a small team who would rather own your tracker, let your agents
work without handing them your identity, and verify every change yourself —
clone it, connect your agent, and tell us what breaks.

→ [Quickstart](../../QUICKSTART.md) · [Protocol](../protocol.md) · [Security](../security.md) · [Compatibility](../clients/compatibility.md)
