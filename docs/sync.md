# Sync, conflicts, and retention

kantaq is local-first: every member runs a local replica, works offline, and
syncs through a shared backend when connected. This document is how that stays
correct (no lost edits), honest (conflicts are visible, not silently merged),
and affordable (the logs do not grow without bound). It builds on the signed
event protocol in [protocol.md](protocol.md).

## Offline, then reconnect

Your writes land in a durable local **outbox** (the append-only event log). When
you reconnect, the sync engine drains the outbox through the backend's atomic
commit RPC, reconciles any dropped acknowledgement exactly once, and pulls
everyone else's committed events into your replica. A crash mid-sync is safe: an
event is either committed (it has a backend revision) or still pending (it
re-drains) — never half-applied.

## Conflicts are first-class, not silently merged

Different fields edited concurrently auto-merge. The **same scalar field** edited
to two different values by two members does not silently pick a winner behind
your back. Last-writer-wins converges the field to one value so work never
blocks (ride-flagged) — and the losing value is preserved in a **`conflict_record`**.

A maintainer resolves it from the **Inbox → Sync conflicts** tab: the record
shows the field, both candidate values, who wrote the losing one, and the
revisions that collided. Pick a side (or type a new value) and the resolution is
committed as a **compare-and-swap**: if the field has moved again since the
record was minted, nothing is applied and the record re-surfaces for you to
re-decide against the current value. The resolution is a new, audited event
attributed to you — never an in-place edit (the proposer/approver-as-distinct-
actors invariant carries to conflict resolution).

An agent never silently resolves a human's conflict: resolving needs
`tickets.write`, and an agent's scope is propose-only.

## Retention keeps the logs bounded

Two tables dominate the footprint, and both are pruned (see
[what a 4-person team actually pays](blog/what-a-4-person-team-actually-pays.md)):

- **Audit detail** (`source="mcp"`) older than 30 days summarizes into retained
  `agent.read`-style rows. The summarize is gated on a Merkle anchor over the
  pre-retention range, so the original detail stays provable; until that anchor
  exists the prune **refuses** rather than produce an unprovable summary.
- **`sync_events`** older than 30 days compact below a safe watermark — the
  minimum revision every live replica has already acknowledged. A replica that
  has fallen behind is re-snapshotted, never stranded behind a pruned cursor.

The **Settings → Sync** dashboard surfaces what is prunable, the replica size by
project, and a non-dollar capacity gauge against the Supabase Free ceiling — so
you see the tier "about to bite" before it pauses you.
