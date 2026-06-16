# What a 4-person team actually pays

The first thing people ask about a self-hosted, local-first tracker is the
honest one: *what does this cost to run?* Not the sticker price — the real
monthly bill once your team has used it for half a year and the logs have grown.

Here is the answer for a 4-person team, with the numbers, not a vibe.

## The short version

| You run | DB / egress ceiling | Bill | When |
|---|---|---|---|
| **Supabase Free** | 500 MB / 5 GB · pauses after 7 idle days | **$0** | Start here. A 4-person team's 6-month footprint (~290 MB) fits under 500 MB. |
| **Supabase Pro** | 8 GB / 250 GB · no pause | **$25/mo flat** | When you outgrow Free or the idle-pause annoys you. A 4-person team sits far under the ceilings → $0 overage → a flat $25. |
| **Self-hosted Postgres (VPS)** | your disk · egress untracked | **$5–10/mo** | The only way under $10 — Supabase's Pro floor is $25. Trades managed Auth/Storage for a flat bill and your own ops. |

That is the whole cost story. **`< $25/mo` is met on Pro for a normal 4-person
team; the stretch `< $10` needs the VPS path** — and we say so instead of
rounding the claim down.

## Why it stays small: the footprint is bounded

The reason a real workspace fits under the Free tier is not luck — it is two
design choices that keep the two dominant tables from growing without bound.

- **Agent reads are aggregated, not logged per call.** The PRD once feared
  ~500,000 `mcp_tool_calls` rows (~150 MB) from agents reading context all day.
  As built, the gateway rolls reads up into ~2,000 `agent.read` summary rows.
  That single fact removes ~150 MB and ~498k rows from the model.
- **Retention is mandatory, not optional.** Detailed MCP-call audit rows
  summarize after 30 days; `sync_events` compacts after 30 days below a safe
  watermark (the minimum revision every live replica has already acked — never
  wall-clock alone, so a replica that was offline for a month is never stranded).

Seed the as-built 6-month 4-person profile (394,535 rows) into a real Postgres
and measure it: **~290 MB**, comfortably under the 500 MB Free ceiling. Our
estimator predicts that footprint within **1.8%** of the catalog ground truth —
because it reads the same `pg_total_relation_size` the database itself reports,
not a guess.

## What the dashboard shows — and what it doesn't

The dashboard in **Settings → Sync** is a **capacity gauge, not a dollar bill**.
It shows rows and bytes against the tier ceiling, a "the free tier is about to
bite" warning at 80%, an idle-pause warning, your agents' activity, and what
retention will prune. It does **not** project a monthly dollar figure.

That is deliberate. kantaq runs no billing system; the real invoice and your
egress live in the Supabase console, and a number we invent locally would be a
number we could get wrong. So the dashboard answers *"how full am I, and is the
free tier about to pause me?"* and links you to Supabase for the actual bill.

## The point

You can run kantaq for a 4-person team for **$0** until you have a reason not to,
then **$25/mo flat**, and **$5–10/mo** if you want to self-host. No per-seat
pricing, no surprise overage, and a gauge that warns you before a ceiling bites
instead of a bill that surprises you after.

See [the sync, conflict, and retention behavior](../sync.md) for how the logs
stay bounded, and [the portability doc](../portability.md) for taking your data
with you.
